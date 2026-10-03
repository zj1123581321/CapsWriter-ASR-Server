# HTTP 与 WebSocket v2 基线

`scripts/_baseline_http_ws.py` 使用仓库 SDK 的实际 producer，测量 HTTP 文件任务与默认 WebSocket v2 的本机应用层 payload、完成耗时、token/timestamp 完整性，并可与一份显式参考稿比较。它只连接 loopback 地址；服务必须在同一隔离主机运行。

## 测量口径

- HTTP 输入是原始文件。分别记录源文件字节、以本机 ffmpeg/soundfile 解码为 16 kHz 单声道 `f32le` 后的 PCM 字节、SDK 实际发出的 PATCH body 累计字节和 create 控制 JSON 字节。
- WebSocket 使用 SDK `transcribe_file` 的默认 v2 路径和 `flac` 编码；计实际 `send()` 的 JSON UTF-8 字节与二进制字节。JSON 中 Base64 音频字节另列，不用 float32 源 buffer 长度冒充线上 payload。
- HTTP 同一 `Upload-Offset` 再次发送相同 body 时，第二次及以后 body 计入 `http_retransmitted_bytes`。HTTP SDK transport retries 固定为 0，工具不自动恢复；如需 SDK 显式恢复，应单独记录恢复调用及当次实际请求。
- 以上是应用 payload，不含 HTTP headers、TCP、TLS、WebSocket frame 或链路层开销。传输层 TCP 重发对 SDK `send()` 不可见。
- 每个协议单独执行一个任务。MP4 与 WAV 用不同匿名 fixture ID、分开记录；比较时两协议必须使用同一源文件、同一服务模型、同一 `seg_duration` 和 `seg_overlap`。
- 工具校验 `/health` 的 `status`、`worker_alive`、模型、协议版本和运行 `git_sha`。健康端点目前返回短 SHA；要求它是指定完整 server SHA 的至少 7 位前缀。结果同时记 benchmark tool SHA 和 SDK 版本，不把工具提交 SHA 说成服务版本。
- 私有 JSON 保存源绝对路径和哈希、参考稿、完整 final 文本/token/timestamp、请求体摘要及模型与工具信息，要求仓库外目录权限为 `0700`，文件权限为 `0600`。stdout 仅含匿名 ID、字节数和指标；失败写 `BASELINE_FAILED` 到 stderr 并非零退出。

## Linux 隔离运行

1. 使用独立 Python 3.12 环境。不要激活或覆盖已有服务环境：

   ```sh
   PRIVATE=/absolute/private/capswriter-m7
   mkdir -m 700 -p "$PRIVATE"
   uv venv "$PRIVATE/venv" --python 3.12
   uv pip install --python "$PRIVATE/venv/bin/python" -r requirements-server-linux.txt
   ```

2. 准备 Paraformer 和 Punct-CT-Transformer 权重到私有缓存。服务配置将模型路径锚定在代码目录 `models/`；让对应的两个模型目录软链到私有缓存，软链和模型都不得进入 Git。Paraformer 走 ONNX CPU；不需要下载 GGUF、llama.cpp 或 GPU 包。缺失、空文件或路径不匹配时停止，不切假引擎。

3. 在隔离 checkout 中检出要测的**已合并 server SHA**，不从正在开发的 benchmark 分支启动服务。为 WebSocket 和 HTTP 各选一个随机高位端口，先用 `ss -Hln` 确认都未监听；配置不接受端口 `0`，冲突时停止并重新准备一组端口，不让服务自动换端口。数据目录使用稳定的私有绝对路径：

   ```sh
   WS_PORT=$(shuf -i 49152-65535 -n 1)
   HTTP_PORT=$(shuf -i 49152-65535 -n 1)
   test "$WS_PORT" != "$HTTP_PORT" || exit 1
   ss -Hln "sport = :$WS_PORT" | grep -q . && exit 1
   ss -Hln "sport = :$HTTP_PORT" | grep -q . && exit 1
   export CW_ADDR=127.0.0.1
   export CW_PORT="$WS_PORT"
   export CW_HTTP_PORT="$HTTP_PORT"
   export CW_HTTP_DATA_DIR="$PRIVATE/http-data"
   export CW_MODEL_TYPE=paraformer
   "$PRIVATE/venv/bin/python" start_server.py > "$PRIVATE/server.log" 2>&1 &
   server_pid=$!
   ```

   先确认 PID 属于这次启动，再由基线工具探测 `/health`。日志只重定向到私有文件；不要整段输出。结束时只向确认过的 `$server_pid` 发 `TERM` 并 `wait`，不使用 `pkill`。

4. 对同一 WAV 先后运行两种协议。MP4 再单独运行一对命令；不要并行提交任务：

   ```sh
   COMMON="--input /private/source/full.wav --fixture-id wav-private-01 --expected-server-sha <40位SHA> --expected-model paraformer --private-dir $PRIVATE/results --reference /private/source/000_video.srt --reference-status unverified"
   uv run --no-project --python 3.12 --with numpy --with soundfile --with websockets==15.0.1 --with httpx==0.28.1 python scripts/_baseline_http_ws.py --protocol ws-v2 --server "ws://127.0.0.1:$WS_PORT" $COMMON
   uv run --no-project --python 3.12 --with numpy --with soundfile --with websockets==15.0.1 --with httpx==0.28.1 python scripts/_baseline_http_ws.py --protocol http --server "http://127.0.0.1:$HTTP_PORT" --health-url "http://127.0.0.1:$WS_PORT/health" $COMMON
   ```

   `--fixture-id` 不得包含文件名、路径或语音正文。参考稿来源未核实时用 `unverified`；CER 只表示相对该参考稿的差异，不表示识别正确率。归一化规则固定为：剥离 SRT 独立编号和时间轴、拼接断行、删除空白与 Unicode 标点、对剩余字符做 `casefold()`。无参考稿可省略 `--reference`；归一化后为空会在发请求前失败。

5. 每协议完成后检查私有 JSON 的 `status=ok`、server/tool SHA、SDK 与 ffmpeg 版本、DONE/final 状态、token/timestamp 数量、单调性、覆盖范围、耗时和 producer payload 摘要。失败项和未量项如实保留；工具当前不测 CPU/RSS，也不推算 TCP/TLS 开销。

## 回归验证

卡面全套验证命令：

```sh
uv run --no-project --python 3.12 --with numpy --with rich --with websockets==15.0.1 --with colorama --with pytest==9.1.1 --with soundfile --with pytest-asyncio==1.4.0 --with aiohttp==3.14.3 --with httpx==0.28.1 python -m pytest tests/ -q -rs -p no:cacheprovider
```

`tests/fixtures/http_baseline_producer.json` 是 SDK 实际 HTTP 请求体和 WS v2 `send()` 帧的合成契约产物；它只验证 producer 序列化，不是模型质量数据。旧 `scripts/_baseline_asr.py` 仍是 WS v1 Base64 JSON，未改其默认行为，不能拿它的 float32 输入长度代替 v1 JSON payload、SDK v2 字节或 HTTP 原文件字节。
