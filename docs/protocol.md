# WebSocket 语音识别协议

本文说明 CapsWriter ASR Server 与下游客户端之间的 WebSocket 和 HTTP `/health` 协议。新客户端应使用 v2；服务端保留 v1 上行兼容。Python 项目可优先使用 [SDK](../sdk/README.md)，其他语言可按本文直接接入。

## 连接与健康检查

- 默认 WebSocket 地址为 `ws://<host>:6016`；proxy 使用自身配置的地址。连上后直接发送 JSON 文本帧，不需要 subprotocol 或握手配置帧。
- 同端口提供 `GET /health`。就绪时返回 HTTP 200 和 JSON，包含 `status`、`protocol_version`、`role`、`encodings`；服务端还返回当前 `model`。未就绪时返回 HTTP 503。
- SDK 要求健康检查报告 `protocol_version >= 2`，且 `encodings` 包含所选编码。它不会回退到 v1。
- 一条连接同一时刻只处理一个活动任务。活动任务收到最终结果后可以继续复用连接；一个任务失败时服务端先发送 `error`，再关闭连接。

服务端 `/health` 的其余字段用于判断运行状态和版本来源：

```json
{"status":"ok","protocol_version":2,"role":"server","encodings":["f32le","s16le","flac","ogg_opus"],"model":"paraformer","git_sha":"abc1234","llama_build":null,"worker_alive":true,"aligner":"native","active_tasks":0,"queued_segments":0}
```

`git_sha` 是服务进程启动时取得的版本标识；更新工作树不会改变已运行进程报告的值。`worker_alive` 表示识别子进程已就绪且存活，`aligner` 表示当前时间对齐器状态；`active_tasks` 与 `queued_segments` 是当前负载计数。`llama_build` 仅在相关 GGUF 模型或已加载对齐器时提供，其他情况为 `null`。proxy 的 `/health` 同样返回状态、协议版本、角色和 `git_sha`；`encodings` 是健康 v2 后端编码的并集，`backends` 逐项报告后端 URL、健康状态、协议版本、模型、编码和版本标识。服务端与 proxy 都会在没有可服务的健康 worker/backend 时返回 HTTP 503。

## 上行音频帧

每条上行消息都是 JSON 文本帧。必填字段如下：

| 字段 | 类型 | 说明 |
|---|---|---|
| `task_id` | string | 任务标识；同一任务所有帧保持一致 |
| `source` | `file` 或 `mic` | 文件或麦克风音频 |
| `data` | string | 每帧音频字节的 Base64 编码 |
| `is_final` | boolean | 末帧为 `true`；末帧可以携带音频数据 |
| `time_start` | number | 音频起始 Unix 时间戳，秒 |

可选字段：`seg_duration`（默认 15 秒，最小 5 秒）、`seg_overlap`（默认 2 秒，必须非负且小于 `seg_duration / 2`）、`language`（默认 `auto`）、`context`（默认空字符串）和 `model`。`model` 有值时必须与当前服务端模型相同。一个任务的分段参数与编码不能在帧间改变。对有单段长度限制的引擎，服务端还会把切点搜索延长量和 overlap 计入最长段长；Qwen3 GGUF 与 MLX 的上限为 80 秒，超限时返回 `bad_request`。

### v1 与 v2 上行

服务端以 `encoding` 是否出现区分上行版本：

- **v1**：不带 `encoding` 和 `samples_total`，音频按 16 kHz、单声道、little-endian float32 PCM (`f32le`) 解释。v1 客户端可继续连接支持 v1 的服务端，也可连接本服务端。
- **v2**：带 `encoding`；末帧必须带非负整数 `samples_total`。`samples_total` 是整段解码后 16 kHz 单声道 PCM 的样本数，服务端允许最多 16,000 个样本（1 秒）的解码差异。编码在一个任务内固定。

v2 示例末帧：

```json
{"task_id":"bcb785be-9f09-46e4-ad05-f64482ae8dd7","source":"file","encoding":"s16le","data":"AAABAAIA","is_final":true,"time_start":1780000000.0,"samples_total":3}
```

`data` 中示例字节仅用于说明字段格式，不构成有效语音样本。对于多帧任务，末帧可包含最后一段数据；若没有剩余数据，`data` 可以为空字符串。

## 音频编码

| `encoding` | 帧内容 | 服务端要求 |
|---|---|---|
| `f32le` | 16 kHz 单声道 float32 little-endian PCM；每帧字节数为 4 的倍数 | 原始 PCM |
| `s16le` | 16 kHz 单声道 int16 little-endian PCM；每帧字节数为 2 的倍数 | 原始 PCM，服务端转为 float32 |
| `flac` | 单条 FLAC 音频流，可任意分帧 | 服务端 PATH 中需有 `ffmpeg` |
| `ogg_opus` | 单条 Ogg/Opus 音频流，可任意分帧 | 服务端 PATH 中需有 `ffmpeg` |

单帧 Base64 解码后的数据上限为 64 MiB；任务解码后时长默认最多 14,400 秒，可由服务端 `CW_MAX_TASK_SECONDS` 调整。v2 客户端应分块发送：原始 PCM 每帧最多 60 秒，压缩流每帧不超过 256 KiB。发送的 `data` 必须是指定编码的裸音频字节，不能把 WAV 文件头放进 PCM 帧。

服务端资源上限和超时默认值如下；服务端操作说明与变量定义见[部署文档](../deploy/README.md)和 `config_server.py`：

| 项目 | 默认上限 | 满载或超时时的处理 |
|---|---:|---|
| 单帧 Base64 解码后音频 | 64 MiB | 返回 `bad_request` |
| 单任务解码后时长 | `CW_MAX_TASK_SECONDS` = 14,400 秒 | 返回 `audio_too_long` |
| 每任务已提交未完成片段 | `CW_MAX_INFLIGHT_SEGMENTS` = 4 | 停止读取该连接，形成 TCP 背压 |
| 服务端全局活动任务 | `CW_MAX_TASKS` = 8 | 新任务返回 `overloaded` |
| Worker 每轮领取片段 | `CW_DRAIN_BATCH` = 16 | 后续片段留在队列中等待 |
| 每连接结果队列 | 256 条 | 队列满时返回 `slow_consumer` 并关闭连接 |
| proxy 每任务上行队列 | 8 帧 | 暂停读取客户端，向上传递背压 |
| 上传空闲 | `CW_UPLOAD_IDLE_SECONDS` = 300 秒 | 返回 `bad_request`；服务端背压等待期间暂停计时 |
| 单段推理看门狗 | `CW_SEGMENT_TIMEOUT` = 600 秒 | 返回 `inference_timeout`；推理进程卡住后主进程非零退出，由外部守护进程重启服务 |

proxy 每 30 秒探测后端健康状态；探测请求超时为 5 秒。压缩编码还要求服务端 PATH 有 `ffmpeg`，否则 `/health.encodings` 不含 `flac` 和 `ogg_opus`，收到相应请求会返回 `unsupported_encoding`。

## 下行结果与错误

成功消息为 JSON 对象，具有 `type: "result"`。`is_final: false` 表示中间进度，`is_final: true` 表示该任务的最终结果。

| 字段 | 说明 |
|---|---|
| `task_id` | 对应的任务标识 |
| `text` | 识别文本 |
| `text_accu` | 按时间信息合并的文本 |
| `tokens` | 服务端输出的 token 列表 |
| `timestamps` | 与服务端 token 输出关联的时间，单位秒 |
| `duration` | 已处理音频时长，单位秒 |
| `time_start`、`time_submit`、`time_complete` | 音频起始、片段提交、任务完成的 Unix 时间戳 |

不要假设不同模型输出相同粒度的 token；生成字幕时直接使用服务端给出的 token 与时间数组，不要从 `text` 反推索引。

### 4.2 error

任务失败消息的格式为：

```json
{"type":"error","task_id":"…","code":"inference_failed","message":"识别失败","retryable":true}
```

收到 `error` 即代表失败；客户端不能把随后连接关闭当作成功。`retryable` 是服务端给出的恢复建议，协议客户端不应无条件自动重试。

| code | 含义 |
|---|---|
| bad_request | JSON、必填字段或参数不合法，或上传空闲超时 |
| unsupported_encoding | 服务端不支持该编码 |
| decode_failed | 音频字节、样本数或解码器处理失败 |
| task_conflict | 同一连接已有另一个活动任务 |
| audio_too_long | 音频超过服务端任务时长上限 |
| inference_failed | 识别或时间对齐失败 |
| inference_timeout | 单段识别超时 |
| overloaded | 服务端活动任务达到上限 |
| slow_consumer | 客户端读取结果过慢 |
| no_backend | proxy 没有支持该请求的健康后端 |
| internal | 其他服务端内部错误 |

服务端把任务错误帧排入发送队列后会关闭对应连接。因此，客户端应先读取并处理错误帧；异常关闭且此前未收到最终结果时应报告连接失败。

## 兼容性与流控

| 客户端请求 | v1 服务端 | 本服务端 / v2 proxy |
|---|---|---|
| v1 上行（省略 `encoding`） | v1 行为 | 兼容处理 |
| v2 上行（带 `encoding`） | 不支持；客户端应先检查 `/health` | 按编码和 `samples_total` 校验 |

proxy 对带 `encoding` 的 v2 任务只选择协议版本不低于 2 且支持该编码的健康后端；找不到时返回 `no_backend`。不带 `encoding` 的 v1 请求保留旧路由行为。

服务端会在读取或推理背压时减慢上行读取，并限制任务数、待处理片段和每连接的结果队列。服务端可通过 `CW_UPLOAD_IDLE_SECONDS` 配置上传空闲超时，默认 300 秒；背压等待时间不计入该空闲时长。客户端应并发发送音频并接收结果，避免先发完整段再开始读取。

## Python SDK

SDK 默认编码为 `flac`，因此服务端也必须在 PATH 中安装 `ffmpeg` 且健康检查需列出 `flac`。首次部署可选 `s16le` 避免服务端压缩解码依赖；SDK 客户端本机始终需要 `ffmpeg` 和 `ffprobe`。SDK 每个任务前检查 `/health`，并发上传和接收；默认总体截止时间为 `max(120 秒, 音频时长 + 60 秒)`，`idle_timeout` 默认 300 秒。超过截止时间、上传发送时限或结果空闲时限时会抛出 `AsrError`。它不自动重试。完整安装与调用示例见 [SDK 文档](../sdk/README.md)。
