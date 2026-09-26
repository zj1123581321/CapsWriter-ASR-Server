# CapsWriter ASR Python SDK

SDK 支持 Python 3.10 及以上版本。服务端推荐 Python 3.12。它将音频文件转为服务端可接收的格式，检查 `/health`，通过 WebSocket 上传并返回识别结果。

## 安装与系统依赖

SDK 依赖 `ffmpeg` 和 `ffprobe`，两者都必须能从 `PATH` 找到：`ffmpeg` 负责音频转码，`ffprobe` 读取文件时长。请先按操作系统安装 FFmpeg，并确认 `ffmpeg -version` 与 `ffprobe -version` 都能运行。

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

上面的首次示例显式选用 `s16le`，服务端无需安装 FFmpeg 即可接收。SDK 默认编码是 `flac`；若使用默认值，服务端也必须在 `PATH` 中安装 FFmpeg，且 `/health` 的 `encodings` 必须包含 `flac`。无论客户端选择何种编码，SDK 都需要客户端本机的 `ffmpeg` 和 `ffprobe`：前者把输入文件转成所选编码，后者读取文件时长。服务端必须报告协议版本 2 和所选编码；SDK 不会降级到 v1，也不会自动重试。同步程序可调用 `transcribe_file_sync(path, url, ...)`。

`transcribe_file` 和同步入口还接受 `encoding`（`flac`、`ogg_opus`、`f32le`、`s16le`）、`language`、`context`、`seg_duration`、`seg_overlap`、`deadline_total`、`idle_timeout`、`model` 与 `on_progress(result_dict)`。异步接口会并发上传和接收结果；失败时抛出 `AsrError`。识别结果的字段见[服务协议](../docs/reference/protocol.md)。

SDK 也提供命令行字幕导出，默认写入 SRT：

```bash
python -m capswriter_asr meeting.m4a --url ws://127.0.0.1:6016 --encoding s16le --out-dir subtitles --format srt,txt,json
```

## 直接使用 WebSocket

非 Python 客户端可直接按[协议 v2](../docs/reference/protocol.md)接入。仓库提供了一个只接受 16 kHz、单声道、PCM16 WAV 短音频的[最小 Python 示例](../examples/websocket_transcribe.py)，展示末帧样本数、并发收发与服务端错误处理。
