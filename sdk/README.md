# CapsWriter ASR Python SDK

SDK 支持 Python 3.10 及以上版本。服务端推荐 Python 3.12。它将音频文件转为服务端可接收的格式，检查 `/health`，通过 WebSocket 上传并返回识别结果。

## 安装与系统依赖

SDK 依赖 `ffmpeg`，必须能从 `PATH` 找到，用于音频转码与压缩流的样本计数。请先按操作系统安装 FFmpeg，并确认 `ffmpeg -version` 能运行。SDK 不依赖 `ffprobe`：音频长度由 SDK 自己解码已发出的字节流得出，不读取源文件的容器元数据。

在仓库根目录安装本地 SDK：

```bash
python -m pip install ./sdk
```

也可以直接从 GitHub 安装：

```bash
python -m pip install "git+https://github.com/zj1123581321/CapsWriter-ASR-Server.git#subdirectory=sdk"
```

## 首次识别

先按[入门指南](../docs/guides/getting-started.md)启动服务，并确认服务端已下载所选模型。默认地址是 `ws://127.0.0.1:6016`。下面的代码转录本地音频文件：

```python
import asyncio
from capswriter_asr import AsrError, transcribe_file


async def main():
    try:
        result = await transcribe_file(
            "meeting.m4a", "ws://127.0.0.1:6016", encoding="s16le"
        )
    except AsrError as exc:
        print(f"识别失败 [{exc.code}]: {exc.message}")
        raise
    print(result.text)


asyncio.run(main())
```

上面的首次示例显式选用 `s16le`，服务端无需安装 FFmpeg 即可接收。SDK 默认编码是 `flac`；若使用默认值，服务端也必须在 `PATH` 中安装 FFmpeg，且 `/health` 的 `encodings` 必须包含 `flac`。无论客户端选择何种编码，SDK 都只需要客户端本机的 `ffmpeg`：压缩编码会先转码，再从已发出的压缩字节流解码累计出 `samples_total` 供服务端对账，不使用源文件的容器时长。服务端必须报告协议版本 2 和所选编码；SDK 不会降级到 v1，也不会自动重试。同步程序可调用 `transcribe_file_sync(path, url, ...)`。

转码时 SDK 对 ffmpeg 显式传 `-map 0:a:0`：**多音轨文件只转录第一条音轨**，视频轨、字幕轨和数据轨都不会被拉进 ffmpeg 的解码图。若文件没有任何音轨，ffmpeg 会非零退出，SDK 抛 `AsrError('decode_failed')`，不会静默产出空音频。

`transcribe_file` 和同步入口还接受 `encoding`（`flac`、`ogg_opus`、`f32le`、`s16le`）、`language`、`context`、`seg_duration`、`seg_overlap`、`deadline_total`、`idle_timeout`、`model` 与 `on_progress(result_dict)`。异步接口会并发上传和接收结果；失败时抛出 `AsrError`。识别结果的字段见[服务协议](../docs/reference/protocol.md)。

`deadline_total` 不传时是自动预算，分两段计时：本地准备阶段（`/health` 检查、转码、样本计数）受入口的 120 秒上限约束，转码耗时不算进转录预算；本地阶段结束后，远端转录的预算 `max(120 秒, 音频时长 + 60 秒)` 从那一刻起算。显式传入 `deadline_total` 则是整个调用的墙钟上限（含本地准备阶段），从进入函数起算，超时按同一条预算裁决。超时消息按路径如实点明被超过的预算（默认路径写「自动预算」，显式传参写 `deadline_total`），并写明卡在「本地准备」还是「远端转录」阶段。

SDK 也提供命令行字幕导出，默认写入 SRT：

```bash
python -m capswriter_asr meeting.m4a --url ws://127.0.0.1:6016 --encoding s16le --out-dir subtitles --format srt,txt,json
```

## HTTP 文件任务（显式提交与恢复）

HTTP 文件入口直接连接固定的 ASR HTTP 地址，上传的是原文件二进制，不会在客户端
额外转码或把文件改成有损格式。它使用 HTTPX 0.28.1，并关闭环境代理、重定向和库级
重试；网络失败后不会自动重发，也不会回退到 WebSocket。当前服务端 HTTP 文件任务由
后续增量交付，服务端 HTTP 监听默认关闭；本节的 SDK/CLI 不能单独让尚未实现该接口的
服务端可用。

提交前 SDK 会先把包含领取凭据的恢复文件原子写入指定路径并设为用户私有权限。该文件
是恢复上传、查询状态和领取结果的唯一凭据，应放在安全位置，不要提交到版本库、复制到
日志或发给他人。成功提交只表示文件已被服务端受理，不表示识别已经完成：

```bash
python -m capswriter_asr http submit meeting.m4a \
  --url http://127.0.0.1:6017 \
  --resume-file ~/.capswriter/meeting.resume.json

python -m capswriter_asr http resume meeting.m4a \
  --url http://127.0.0.1:6017 \
  --resume-file ~/.capswriter/meeting.resume.json

python -m capswriter_asr http status \
  --url http://127.0.0.1:6017 \
  --resume-file ~/.capswriter/meeting.resume.json

python -m capswriter_asr http result \
  --url http://127.0.0.1:6017 \
  --resume-file ~/.capswriter/meeting.resume.json \
  --out-dir subtitles --format srt,txt,json
```

`resume` 是用户在失败后明确执行的恢复动作：它会重新核对源文件大小和 SHA-256，
查询服务端确认的 offset，只上传未确认的原始字节，且不能替换原地址或提交参数。
`status` 只查一次，`result` 只领取 DONE 的完整结果，不会后台轮询。服务端源音频按
协议保留终端任务后的 7 天，任务记录和结果不因源文件清理而删除。

## 直接使用 WebSocket

非 Python 客户端可直接按[协议 v2](../docs/reference/protocol.md)接入。仓库提供了一个只接受 16 kHz、单声道、PCM16 WAV 短音频的[最小 Python 示例](../examples/websocket_transcribe.py)，展示末帧样本数、并发收发与服务端错误处理。
