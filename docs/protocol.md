# CapsWriter ASR 服务协议（v2）
状态：已评审（2026-09-26 CEO review + eng review 窄审），实现按任务卡逐步落地；本文件是服务端、proxy、SDK 的唯一契约来源。

适用：server（`core/server`）、proxy（`core/proxy`）、SDK（由 `scripts/transcribe_client.py` 升级）。
协议定义唯一来源：仓内包 `capswriter_asr/protocol.py`（可 `pip install git+…` 给下游）；`core/protocol.py` 改为从它 re-export，现有 import 不变。

## 0. 设计约束

- 外部下游用 `result.get("is_final", False)` 判结束（tg-archiver `asr/client.py:209`、VideoTranscriptAPI `capswriter_client.py:651`、WechatChatRoomSummary `capswriter.py:203`），三家都在 `ConnectionClosed` 时退出等待（tg-archiver 抛 `AsrConnectionError`；另两家返回 None）。**让老下游停止等待的信号只有 `is_final` 或连接关闭。** 服务端只保证「不挂死」；老下游是否把 None 正确当失败上报，由下游迁移 issue（T14）负责。
- 服务端与 proxy 均禁用 WS ping（`server_manager.py:73`、`proxy_server.py:73`、`router.py:237`，提交 f61a1a7）。存活判定只靠应用层计时与进度消息。
- 红线：无重试、无 fallback，错误 fail fast。

## 1. 连接、任务与唯一终态

- **一条连接同一时刻最多一个活动任务**。任务终结（final 或 error）后，同连接可开始下一个任务（兼容现麦克风客户端复用连接：`core/client/connection/websocket_manager.py:75`、`recorder.py:188`）。
- 活动任务未终结时出现不同 `task_id` → `error{code:"task_conflict"}`。
- 服务端内部一律以 `(socket_id, task_id)` 为任务键（修正 `state.py:70` 仅按 task_id 全局存储导致跨连接同 ID 串结果的问题）。

任务状态机（每个任务唯一一个，服务端主进程持有）：
```
 RECEIVING --上行 is_final--> DRAINING --最后一段结果到达且尾段已切完--> DONE (发 final)
     |                            |
     +--------任一失败-------------+------------> FAILED (发 error)
 DONE/FAILED 为终态；进入终态的转移只允许发生一次（原子检查），
 终态后：迟到的结果丢弃；已入推理队列未开始的段由 worker 按「已终止任务集合」跳过；
         正在推理的段不做跨进程中断（跑完后结果丢弃）；
         在途计数、proxy active_tasks 只释放一次。
 并发裁决：先到者胜。final 与 error 同时就绪时以先完成终态转移者为准。
 重复 is_final、终态后继续上传的帧：丢弃并记 warning，不回包（连接已关或已进入下一任务前）。
```
- error 的「发出」= 放入该连接出站队列；随后该连接出站队列冲刷（上限 5s），再以 close 4000 关闭。冲刷超时直接关闭。
- DONE 不关闭连接（由客户端关）；FAILED 必关连接（为了让老下游退出等待）。

```
客户端                                    服务端
  |-- GET /health -------------------------->|  SDK 校验版本
  |== WS ===================================|
  |-- audio{task_id,encoding,data,F} ------>|
  |-- audio{...}  (连续分块，≤60s/帧) ------->|  背压：在途段满时停止读取
  |<-- result{type:result,is_final=F} ------|  每段一条累计进度
  |-- audio{...,is_final=T} --------------->|  末帧可带数据；服务端继续切段
  |<-- result{type:result,is_final=T} ------|
 失败：|<-- error{...} ; close(4000) ---------|
```

## 2. 上行消息 audio（JSON 文本帧）

| 字段 | 类型 | v1 | v2 | 说明 |
|---|---|---|---|---|
| task_id | str | 有 | 有 | 非空 |
| source | "mic"\|"file" | 有 | 有 | 下游一律 "file" |
| data | str | 有 | 有 | base64，可空串 |
| is_final | bool | 有 | 有 | 末帧 true，**可携带数据** |
| time_start | float | 有 | 有 | 透传 |
| seg_duration | float | 有 | 有 | 默认 15 |
| seg_overlap | float | 有 | 有 | 默认 2 |
| context / language | str | 有 | 有 | |
| **encoding** | str | 无 | 新增 | 见 §3；**出现该字段即声明本任务为 v2 任务** |
| **samples_total** | int | 无 | 末帧必填 | 16kHz 单声道总样本数；非末帧省略。服务端容许解码帧填充误差 1 秒 |
| model | str | 无 | 可选 | 期望的后端模型（/health 的 model 值）；缺省 = 不限 |

校验（越界 → `bad_request`）：
- `5 ≤ seg_duration`，`0 ≤ seg_overlap < seg_duration / 2`，且 **`seg_duration + seg_overlap + 吸附最大延长 ≤ 引擎单段上限`**（引擎上限由 engine 暴露，如 MLX `chunk_size=80`）。服务端据此保证**每个实际提交段都不超过引擎上限**，引擎侧截断变为断言失败（`inference_failed`），绝不静默截断。
- 同一任务内 encoding 不可变。
- 缺 encoding = v1 任务，按 `f32le` 处理。
- 未知字段忽略。

**末帧切段规则（修复现存缺陷 `ws_recv.py:190`）**：收到 is_final 后，先把末帧数据并入缓冲，再按常规规则循环切段直到剩余 ≤ 单段上限，最后提交尾段。压缩编码则在解码器输出 EOF 之后执行同一流程。

## 3. encoding 与解码

| 值 | 内容 | 约束 | 服务端处理 |
|---|---|---|---|
| `f32le` | PCM float32 LE 16kHz 单声道 | 每块字节数 4 的倍数 | 直入缓冲 |
| `s16le` | PCM int16 LE 16kHz 单声道 | 每块字节数 2 的倍数 | 转 float32 |
| `flac` | 一条 FLAC 流，任意字节切分 | — | per-task ffmpeg |
| `ogg_opus` | 一条 Ogg/Opus 流，任意字节切分 | — | per-task ffmpeg |

- 单帧 base64 解码后 ≤ 64 MiB（约 17 分钟 f32le）；超限 `bad_request`。现 `transcribe_client.py:279` 整文件单帧发送的写法在 T12 改为分块，与本限制同批上线。
- 单任务解码后音频时长 ≤ `CW_MAX_TASK_SECONDS`（默认 14400）；按**解码后 PCM** 计，超限 `audio_too_long`。
- ffmpeg 生命周期（asyncio 子进程，写 stdin 与读 stdout 为两个并发协程，禁止同步串行写读）：
  1. 首帧创建：`ffmpeg -nostdin -f {flac|ogg} -i pipe:0 -ar 16000 -ac 1 -f f32le pipe:1`（显式 demuxer）。
  2. 数据帧写入 stdin（带背压 drain）。
  3. 上行 is_final：关闭 stdin → 读 stdout 至 EOF → `wait()` 取退出码 → 非零即 `decode_failed` → 否则执行末帧切段。
  4. 取消（error、断连）：关闭 stdin，5s 内未退出则 kill，再 wait 回收。任何路径都不得遗留子进程。
- 服务端 PATH 无 ffmpeg 时，/health 的 encodings 不含压缩格式；收到压缩任务 → `unsupported_encoding`。
- SDK 默认 `flac`；`ogg_opus` 需在固定语料 CER 不劣于 flac 后按消费方逐个开启。

## 4. 下行消息

### 4.1 result
v1 字段全部保留，新增 `type:"result"`（老下游 dict.get 读取不受影响）。
- 中间帧：每段一条，用作进度与存活信号。
- 最终帧：`is_final=true`；file 任务的 tokens/timestamps 必须来自真实对齐或引擎原生时间戳；不得缺段。

### 4.2 error（v2）
```json
{"type":"error","task_id":"…","code":"inference_failed","message":"含段序号等上下文","retryable":false}
```
| code | 触发 | retryable |
|---|---|---|
| bad_request | 坏 JSON、缺字段、越界、encoding 变化、帧超限、上传空闲超时 | false |
| unsupported_encoding | 取值不认识或本机不可用 | false |
| decode_failed | base64/字节对齐错误、ffmpeg 非零退出 | false |
| task_conflict | 活动任务未终结时出现新 task_id | false |
| audio_too_long | 超时长上限 | false |
| inference_failed | 段推理异常、对齐返回空、引擎断言失败 | true |
| inference_timeout | 单段推理超 `CW_SEGMENT_TIMEOUT` | true |
| overloaded | 全局活动任务超上限 | true |
| slow_consumer | 该连接出站队列满（客户端长期不读） | true |
| no_backend | （proxy）无可用 v2 后端 | true |
| internal | 其他（附异常类名） | true |

- 老 v1 客户端（仓内 `core/client`，将于 T9 删除）用 `RecognitionMessage.from_dict` 解析 error 会抛 KeyError；随后连接关闭，它会停止等待，但可能误报「完成」——已知且接受，不修（客户端将删除）。
- 无法解析出 task_id 的坏帧，task_id 填空串。

## 5. /health（HTTP GET，同端口）

server：
```json
{"status":"ok","protocol_version":2,"role":"server",
 "encodings":["f32le","s16le","flac","ogg_opus"],
 "model":"qwen_asr_mlx","git_sha":"abc1234","llama_build":"b10621",
 "worker_alive":true,"aligner":"ok","active_tasks":1,"queued_segments":3}
```
proxy：
```json
{"status":"ok","protocol_version":2,"role":"proxy","git_sha":"…",
 "encodings":["f32le","s16le","flac"],
 "backends":[{"url":"…","healthy":true,"protocol_version":2,"model":"…","encodings":[…],"git_sha":"…"}]}
```
- 不可服务（worker 死、未就绪、proxy 无健康 v2 后端）→ HTTP 503。
- proxy 的 `encodings` = 所有健康 v2 后端 encodings 的并集；proxy 每 30s 刷新后端 /health。
- 旧服务端无 /health：websockets 对普通 HTTP 请求返回非 200（通常 426），**SDK 把任何非 200 / 非 JSON 响应一律视为 v1**。
- 对齐器：服务启动时**立即加载**（改掉 `engines/manager.py:20` 的首次使用惰性加载）；需要外挂对齐器的引擎加载失败 → 启动失败。原生时间戳引擎（paraformer、sensevoice 等）`aligner:"native"`。空闲卸载机制保留，但再次加载失败 → 该任务 `inference_failed`，不伪造时间戳。

## 6. 版本规则

- 整数版本，只增不减；v1 = 现状，v2 = 本文件。v2 服务端兼容 v1 任务。
- `model` 为可选字段，不升版本。
- **SDK 永远发送 encoding 字段（含 f32le）**，因此 SDK 任务都是 v2 任务：
  - SDK 每任务前 GET /health；非 v2 → `ServerTooOld`；encodings 不含所选编码 → `UnsupportedEncoding`。不降级。
  - proxy 对 v2 任务只路由到健康的 v2 后端，且后端 encodings 含该编码；否则 `no_backend`。v2 任务不会落到 v1 后端，v2 保证（error 帧、终态规则）端到端成立。
  - 缺 encoding 的 v1 任务，proxy 可路由到任意健康后端（现状）。
- 破坏兼容须升 v3，/health 增加 `min_protocol_version`。

## 7. 背压与资源上限（全链路有界）

| 位置 | 上限 | 满时行为 |
|---|---|---|
| server 每任务在途段 | `CW_MAX_INFLIGHT_SEGMENTS`=4 | 提交前 await 信号量 → 停止读该连接 → TCP 流控 |
| server 全局活动任务 | `CW_MAX_TASKS`=8 | 新任务 `overloaded` |
| server 每连接出站队列 | 256 条 | 满 → `slow_consumer`，关该连接；分发器从不 await 满队列（put_nowait） |
| server worker 每轮取入 | `CW_DRAIN_BATCH`=16 | 取满即去推理 |
| proxy 每任务上行队列 | 8 帧（改 `router.py:203` 的无界 Queue） | 满 → 停止读客户端，背压透传 |
| proxy 每任务下行队列 | 256 条 | 同 server 出站规则 |
| ffmpeg 输出读取 | 受在途段信号量约束 | 同上 |

- 信号量在**结果回到主进程时**释放（不等送达客户端），慢客户端不会反向锁住推理。
- 结果分发：单一分发协程从 `queue_out` 取结果，按 `(socket_id, task_id)` `put_nowait` 到对应连接的出站队列；每连接一个发送协程。一个慢连接只影响自己。

计时规则：
- **上传空闲** `CW_UPLOAD_IDLE_SECONDS`=300：仅在服务端「愿意读」（未被背压挂起）时计时；背压挂起期间暂停计时。超时 → `bad_request`（message 注明 upload idle）。
- **单段推理看门狗** `CW_SEGMENT_TIMEOUT`=600：主进程记录每段提交时间，超时 → 该任务 `inference_timeout`；并判定推理子进程卡死 → 主进程非零退出交守护进程拉起（与 D3「子进程死亡即退出」同一原则）。上限按最慢机器（Mac mini CPU）单段耗时的 10 倍以上取值，部署时按 /health 实测复核。

## 8. SDK 行为

- `websockets.connect(..., ping_interval=None, max_size=None, max_queue=None)`。
- 上传与接收并发（两个协程）；禁止「先发完再收」。
- 分块：raw 编码每帧 ≤ 60 秒音频；压缩流每帧 256 KiB。
- 截止时间由独立计时器强制（修正 `transcribe_client.py:282`）：
  - `deadline_total = max(120s, 音频时长 × 1.0 + 60s)`；
  - `idle_timeout = 300s`：上传结束后无任何服务端消息，或 send 被阻塞超过此值 → 失败。
- 失败抛 `AsrError(code, message)`：code 取服务端 error 原值；另有 `timeout`、`connection_lost`（无 error 帧的异常关闭）、`server_too_old`、`unsupported_encoding`。SDK 不自动重试。

## 9. 兼容性矩阵

| 客户端 \ 服务端 | v1 server | v2 server | v2 proxy（混合后端） |
|---|---|---|---|
| 外部老下游（缺 encoding） | 现状 | 正常；失败时 error+关连接 → 退出等待 | 可落任意后端，现状语义 |
| 仓内 core/client 麦克风 | 现状 | 正常（顺序复用连接允许） | 同左 |
| 仓内 transcribe_client（整文件单帧） | 现状 | >17 分钟被 bad_request 拒 → T12 同批改分块 | 同左 |
| SDK v2（任意 encoding） | ServerTooOld | 正常 | 只落 v2 后端，否则 no_backend |

## 10. 本契约引出的现存缺陷（与 v2 无关，P0 修）
- 末帧整块提交可超引擎上限，MLX 静默截断尾部（`ws_recv.py:190` + `qwen_asr_mlx/asr_engine.py:108`）。
- 分段参数无校验，`seg_duration=0` 时 `ws_recv.py:74` 死循环冻结事件循环。
- 结果按 task_id 全局存储，跨连接同 ID 串结果（`state.py:70`）。
