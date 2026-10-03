# M7 Linux HTTP 与 WS v2 首测证据

记录日期：2026-10-04（UTC+8）

## 运行环境与服务身份

- 基线工具提交：`d4eab579a93da326e79786002a7cf5ddf3e79a9a`；服务端 checkout 与运行 health 的版本：`820c3a2ee4fccc1b44187bd99c40cf99c16e1ca2`。
- 服务为隔离 Linux checkout 上的 Paraformer，绑定 `127.0.0.1` 随机高位端口；health 返回 `status=ok`、`worker_alive=true`、`protocol_version=2`、`model=paraformer`，短 SHA 与指定完整 SHA 前缀一致。服务进程 PID 与工作目录确认属于本次隔离启动。
- Python `3.12.3`，CapsWriter SDK `0.1.0`，`httpx 0.28.1`，uv 本次解析 `websockets 17.2`，FFmpeg `6.1.1`，`sherpa-onnx 1.13.8`，ONNX Runtime `1.30.0`（CPUExecutionProvider）。隔离环境由 `requirements-server-linux.txt` 安装并通过 `uv pip check`。
- 独立进程树监控记录峰值 RSS `914,108 KiB`（约 893 MiB），退出时累计 CPU 时间 `63` 秒；确认后向本次启动的监控器发 `TERM`，服务正常退出码 `0`，无本次服务进程残留。
- 另以 3 秒合成 WAV 各跑一次 WS v2 和 HTTP，状态均为 `ok`。输入没有语音，token 数为 0；此 smoke 只确认协议往返、health 检查与 DONE 路径，不用于识别质量判断。

## 素材关系与参考稿限制

匿名素材 `private-wav-01` 与 `private-mp4-01` 均为 `77.184` 秒。WAV 大小 `2,469,966` 字节，MP4 大小 `18,974,961` 字节；两者解码为 16 kHz 单声道 `f32le` 后均为 `4,939,776` 字节。独立本地解码 PCM 相关系数约 `0.999999969`，但逐样本不相等，因此仍按 WAV 与 MP4 两种输入形态分别记录。

字幕含 20 条，最后一条结束于 `77.040` 秒，位于素材时长内；没有可验证的人工标注或生成来源记录，故四次结果均记为 `reference_status=unverified`。下表 CER 只表示相对这份未核实参考稿的差异，不代表识别正确率或绝对质量。

## 四次串行实测

所有任务均使用相同服务、模型与 SDK 分段参数（`seg_duration=15`、`seg_overlap=2`），每次只运行一个 ASR 请求；每份结果均为 `status=ok`、最终 `DONE`，token 与 timestamp 数量相等，timestamp 单调且覆盖 token 并在 77.184 秒内。

| 匿名输入 | 协议 | 耗时（秒） | token / timestamp | CER（未核实参考） | 实际 producer payload |
| --- | --- | ---: | ---: | ---: | --- |
| `private-wav-01` | WS v2 | 4.446 | 107 / 107 | 0.6667 | 6 个文本帧，JSON UTF-8 累计 1,972,515 字节；其中音频 Base64 为 1,971,176 字节，解码 FLAC 为 1,478,372 字节；二进制帧 0 |
| `private-wav-01` | HTTP | 3.775 | 107 / 107 | 0.6667 | create 控制 JSON 200 字节；3 个 PATCH 累计上传 WAV 原文件 2,469,966 字节；HTTP 请求共 20 次，重复 body 0 字节 |
| `private-mp4-01` | WS v2 | 3.894 | 104 / 104 | 0.6491 | 11 个文本帧，JSON UTF-8 累计 3,619,954 字节；其中音频 Base64 为 3,617,520 字节，解码 FLAC 为 2,713,120 字节；二进制帧 0 |
| `private-mp4-01` | HTTP | 4.120 | 123 / 123 | 0.6667 | create 控制 JSON 201 字节；19 个 PATCH 累计上传 MP4 原文件 18,974,961 字节；HTTP 请求共 36 次，重复 body 0 字节 |

WS 的 Base64 字节是 JSON UTF-8 总量的子集，不能再次相加。HTTP 与 WS 计量均为应用层 producer payload，不包含 HTTP headers、TCP/TLS、WebSocket frame 或链路层开销。HTTP 请求总数含控制及状态请求。SDK transport retries 为 0，工具无自动恢复或重试；所有记录的重发字节为 0。

同一 WAV 的两个协议本次得到相同 token 数和参考差异指标；MP4 的 HTTP 与 WS 输出 token 数不同。此首测只记录观察结果：两条调用路径的媒体解码位置不同，现有证据不支持把该差异归因于单一环节，也不据此评判协议优劣。

## 执行命令形态

原始媒体、字幕路径和结果 JSON 只在派发私有目录使用；公开文档不记录路径、正文、哈希或 token 内容。实际命令使用以下形态，并分别串行运行 WAV 与 MP4；HTTP 命令额外提供同实例的 health URL：

```sh
uv run --no-project --python 3.12 --with numpy --with soundfile --with websockets --with httpx==0.28.1 \
  python scripts/_baseline_http_ws.py \
  --protocol ws-v2 --server ws://127.0.0.1:<WS_PORT> \
  --input <PRIVATE_INPUT> --fixture-id <ANONYMOUS_ID> \
  --expected-server-sha 820c3a2ee4fccc1b44187bd99c40cf99c16e1ca2 \
  --expected-model paraformer --private-dir <PRIVATE_RESULTS> \
  --reference <PRIVATE_SRT> --reference-status unverified --timeout 240
```

HTTP 模式将 `--protocol http --server http://127.0.0.1:<HTTP_PORT>` 替换 WS 参数，并加 `--health-url http://127.0.0.1:<WS_PORT>/health`。本次 uv 实际解析的 `websockets` 版本是 `17.2`；完整测试另按卡面分别使用固定 `15.0.1` 与未 pin 的最新 `17.2`。

## 回归与未覆盖项

- 全量 `tests/` 固定 websockets `15.0.1`：`452 passed, 3 skipped`。最新 `17.2`：`452 passed, 3 skipped`。两次的三个既有 skip 均为两项 ForceAligner 集成依赖缺失和一项 silero VAD/onnxruntime 集成依赖缺失，没有 HTTP decode skip。
- 本次只在 Linux CPU Paraformer、该固定服务 SHA、这一组匿名素材上验证；没有跨平台实测、生产服务启动、对外上传或部署。
- 字幕来源未核实；工具不测 CPU/RSS、TCP/TLS/WS frame/link payload，不测客户端网络重传，也不据 CER 宣称绝对准确率。
