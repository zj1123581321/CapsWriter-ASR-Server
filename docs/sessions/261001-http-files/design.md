# HTTP 文件任务：公开功能契约

## 范围状态

用户已在 2026-10-01 明确授权拆卡落地完整实现；本文只记录公开契约和本路线图，不表示 HTTP 文件任务已经可用。本次 E1 只交付任务归属、跨进程传播和共享有界 PCM 分段基础，HTTP listener、存储、runner、SDK 仍保持关闭并留给后续增量。独立规划终审历史没有完整覆盖，不能把规划记录当成实现或质量通过。先前“仅规划/未授权”是历史；实现授权是新近获得的，缺集中 gate 例外由主脑处理，本增量不能合并或部署。

## 用户可见目标

完整文件上传在未来应能在收到受理确认后脱离客户端连接，服务端继续识别；客户端之后用显式凭据查询并领取结果。上传支持显式续传，服务重启后保留已确认前缀和任务记录；识别中断必须可见，不自动重跑。实时 WebSocket 仍是默认入口，协议、端口、健康检查和断线清理不因 HTTP 改变。

## 任务与归属契约（R4）

- `Task` 和 `Result` 都带 `owner_kind`，默认值为 `ws`，因此旧构造仍表示 WebSocket 任务。
- WebSocket 任务使用 `socket_id` 作为派生 owner ID；HTTP 任务的 `socket_id` 必须为空，使用稳定 `task_id`（未来为稳定 `job_id`）作为派生 owner ID。
- 所有主进程、子进程、会话、背压和结果分发消费者使用同一个三元 key：`(owner_kind, derived_owner_id, task_id)`。不另存重复 `owner_id`，不伪造 WebSocket。
- 主进程通过受管理的 `active_http_jobs` 集合向 worker 传播 HTTP 活跃任务。HTTP 任务只有登记在该集合中才能入队或继续推理；集合中不存在的任务必须被丢弃，不能复活或污染 WS。
- 父进程结果分发为 HTTP 保留显式可注入 sink；没有注入真实持久消费者时 fail-fast，不把 HTTP 结果伪装成 WS 或静默丢弃。E3 才提供持久消费者。worker 超时或进程失败路径必须先完成持久 sink 提交，再释放 HTTP owner。
- 既有 `Task.data` 仍是有界 16 kHz 单声道 float32 PCM；原文件只由未来主进程读取解码，不把整文件或路径交给 worker。

## PCM 分段契约

`core/server/connection/segmenter.py` 的 `CutFinder` 仍负责切点吸附；`core/server/segmenter.py` 只提取有界 PCM 缓冲、切点搜索、偏移、重叠、段长预算和最终段语义。当前真实 caller 是 WS 接收层，后续 HTTP runner 复用同一输出。

每个段是有限大小的 little-endian、16 kHz、单声道 float32 PCM。切点 stride 与 overlap 都必须先按旧 WS producer 量化为采样点，再乘每采样 4 字节：`round(seconds * SAMPLE_RATE) * BYTES_PER_SAMPLE`。合法有限小数 `seg_duration`/`seg_overlap` 仍被协议接受，不得改成非法，也不得靠补 0 字节掩盖错切。offset 按不含 overlap 的量化 stride 累加，段数据包含 overlap；第一段 `Task.offset` 仍为切前缓冲偏移。中间段 `is_final=false`，末帧只产生一个剩余 PCM 的 `is_final=true` 段。切点吸附保持 VAD 优先、RMS 降级、搜索窗口、最大切点和引擎单段上限。WS 自己负责连接取消和 inflight semaphore，HTTP runner 自己负责持久确认；二者不共享连接取消或持久 ACK。

外部 PCM 意见保持 P2 分级：合法浮点参数若再偏离采样点量化，会使识别消费 `np.frombuffer` 因非 4 字节对齐失败；这是原重构冻结行为，不引入新对齐抽象、配置项、状态或参数限制来绕过。

## 领取凭据契约（R1）

创建上传前由客户端生成并可靠保存高熵随机令牌；上传、查询、写入和 Job 领取都通过请求头携带。服务端只存令牌指纹，任务编号仅用于查找和排错，不赋权。缺失或错误令牌拒绝访问。日志、URL 和默认终端不回显令牌。丢失令牌不能仅凭编号自行领取。不新增用户账号或认证平台。

## HTTP 依赖与启用契约（R2）

服务端直接依赖 aiohttp==3.14.3，SDK 直接依赖 HTTPX==0.28.1；两个部署边界分别声明，server 不导入 SDK，也不要求 SDK 安装 server 库。aiohttp 声明带 `python_version>='3.10'` marker，仅在显式启用 HTTP 时按需导入并要求该运行时；HTTP 未启用的旧服务不因新增依赖被迫升级解释器。启用但不满足依赖或运行时时明确启动失败，不 fallback。不抬高 SDK 既有最低版本。

SDK 客户端使用 `retries=0`、`follow_redirects=False`、`trust_env=False`；每个请求只发一次，3xx 为明确协议错误。无 WS fallback、无 HTTP proxy。`CW_HTTP_PORT` 默认禁用；启用必须同时给出显式端口和稳定绝对 `CW_HTTP_DATA_DIR`。HTTP 端口不得与旧 WS 相同，不自动推算邻近端口。显式启用后初始化错误 fatal，不自动退回 disabled。

## HTTP 接口契约（R3）

二进制请求体不是 JSON、Base64 或 multipart，不接受额外 Content-Encoding。令牌只入 `Authorization: Bearer`。任务参数 `model`/`language`/`context`/`seg_duration`/`seg_overlap` 使用旧校验语义，恢复时不可改。创建或最终提交丢失后由客户端显式恢复，网络库不得自动重发。

| 方法/route | 成功 | 失败 |
|---|---|---|
| `POST /v1/uploads` | 小 JSON：`size_bytes>0`、sha256（64 hex）、options；`Idempotency-Key` + Bearer。首次 201；相同 key+token+同身份参数返回 200 同一 `upload_id`、`UPLOADING`、`confirmed_offset`、`expires_at` | 400 参数、409 同 key 不同内容、429 并发/数量、413 文件限、507 容量；失败不产生 Job |
| `GET /v1/uploads/{id}` | 200：state/size/sha/可信 offset/expires_at/`job_id?`；COMMITTED 历史可找回同一 Job | 未知或错 token 同 404；token 正确但已过期 410；503 存储不可读，不默认 offset=0 |
| `PATCH /v1/uploads/{id}` | 原字节、确定 `Content-Length`、`Upload-Offset`；仅在 fsync 与 offset 事务成功后 204 + 新 `Upload-Offset` | 409 旧/超前 offset 或写竞争并带可信 offset；413 超块/文件长度；400/415 错 body/编码；断网无假 ACK |
| `POST /v1/uploads/{id}/commit` | offset=size 且实际长度/hash 相同；唯一 Job 事务后首次 202；重复 200 同一 `job_id`，不重新识别 | 422 完整性错、410 过期、429/507 准入满；保留未受理上传供显式继续，不投模型 |
| `GET /v1/jobs/{id}` | 200：`QUEUED`/`RUNNING`/`DONE`/`FAILED`、时间、`error_code`、`result_available`、`source_available` | 404 未知/无权；503 查询不可用 ≠ Job 失败 |
| `GET /v1/jobs/{id}/result` | DONE 时 200 完整识别结果：duration、全部文本、tokens、timestamps、最终时序 | 409 `result_not_ready` 或 `job_failed` 并带已存 `error_code`；源音频 7 天过期不影响领取 |

错误体含 `code`、不泄密的 `message`、`request_id`；offset 冲突才额外带 `confirmed_offset`。未来 SDK 显式导出提交/恢复/查询/领取入口，旧 WS API 与 CLI 默认不变；未匹配或旧服务不支持均明确失败，不自动换 WS。

## 持久提交与存储契约（R5）

SQLite：WAL、synchronous=FULL、foreign_keys ON、timeout=0。一个 server 对稳定数据目录持 OS 独占锁；不删 PID 锁猜活跃，不支持多实例共写或网络盘。独立单写、受监督的 I/O worker 与有界 mailbox；SQLite 连接只在该 worker 创建和使用。不在 WS 事件循环做 fsync，不存在自动 DB busy 重试。

可靠提交顺序：字节 → offset 记录 → ACK；完整文件身份核对后同一事务建立唯一 Job 并进入 COMMITTED；完整 result 与 DONE 必须同一次提交。取消 HTTP 请求不释放尚未完成的 I/O；已开始的写入必须结束后才能解锁。无自动重跑。重启只把旧 `QUEUED`/`RUNNING` 标为失败（`server_restarted`），partial 前缀和已完成结果保留。未登记残留文件不自动删除。

上传状态：`UPLOADING -> COMMITTED`，过期为 `EXPIRED`。任务状态：`QUEUED -> RUNNING -> DONE/FAILED`。终态不可反向转 RUNNING。

## 监督契约（R6）

HTTP listener、文件 runner、I/O operation 与结果泵必须进入真实监督链。关键任务异常使 store readiness 不可用，并以非零退出。正常信号关闭：停 listener、等待受限 I/O 结束、停 worker 后结束 loop。未知 RuntimeError 不得吞掉。旧 SIGTERM 零退出、端口释放和真实异常非零都要回归。

## 资源起点（R7）

下列数字是启用 HTTP 后的起始 guard，不是测得吞吐。HTTP 未启用时不改变旧 WS。额度按实际源文件、DB+WAL+SHM、accepted result reservation 和物理剩余核算；满时 429/507，仍能查询旧结果。真实三平台与 ASR 字节/质量基线尚未验证，实施阶段必须实测，不能凭理论百分比宣传省流量。

| 量 | 起始值 |
|---|---|
| 原文件 / PATCH 块 / 流读取 | 1 GiB / file；1 MiB / PATCH；64 KiB / read |
| 小 JSON / 并发 handler / 同时 body | 16 KiB；16 handler；同时 body 操作 2 |
| 未完成上传会话 | 32；已中断进度可保留，不等于只能保存 2 份上传 |
| HTTP Job 准入 / 运行 | 共享活动总量 = 内存 `state.tasks` 中的非终态记录（WS 与运行中 HTTP）加上 SQLite `jobs` 表中 `QUEUED`+`RUNNING` 的 HTTP Job，同一 Job 两处都在时按 job_id 只数一次；总量上限是现有 `max_tasks`（默认 8），其中固定预留 2 个名额只给 WS，HTTP 准入不得把总量推过 `max_tasks - 2`（默认 6）；WS 首帧与 HTTP commit 两条准入路径在同一把共享准入锁内完成「计数 → 判定 → 登记」，登记动作不得在锁外；停机时先在锁内把 DB 侧计数来源换成显式失败的再拆 worker/存储，事后准入不得退化为只数内存；幂等重放先于预算判定（重放拿回同一 Job，不新建）；超限时 HTTP commit 报 429 `too_many_jobs`、WS 首帧报 `overloaded`；HTTP 主动运行 1；mic 优先不变 |
| 段 / 解码器 | 每任务 4 个在途 PCM 段；HTTP 解码器 1；不落完整 PCM 文件 |
| 无输入空闲 | 300 s，与旧 WS 起点一致；当前 PATCH 失败不因此删除 upload 进度 |
| 未完成上传 TTL | 创建或最近成功确认 PATCH 后 7 天；GET 不延长；活跃 I/O 不误清；过期 410，元数据不自动删 |
| 源音频 / 元数据库 | 16 GiB source 声明长度总预留（含未确认尾和旧保留源）；2 GiB DB+WAL+SHM/结果整体 guard |
| 结果 / 安全余量 | 64 MiB 最终 JSON；每个待处理 Job 准入时预留 result 与 WAL 峰值；实际剩余文件系统保持 2 GiB 安全余量 |
| I/O mailbox | 32 有界操作 |

源音频在终态 7 天且无活跃引用后才允许清理；任务记录和结果不删除。容量不足、未知任务、旧 offset、错误凭据和持久化不可用都必须给出可见错误；不默认 offset=0，不静默 catch-all。解码按实际采样数强制检查，沿用现有 `CW_MAX_TASK_SECONDS`（默认 14400 s / 4 h）与引擎段预算；不采用 12 h 候选。

## 失败、保留与提交顺序

上传必须先校验身份、大小、指纹和容量，再有限写入；只有文件字节和任务记录可靠提交后才确认受理。受理后再解码为 PCM 段并走现有 worker。任何解码失败、中间段失败、结果提交失败都不能发布残缺成功。客户端在首个请求前保存高熵任务令牌。网络库不自动 retry、redirect 或 fallback WS，响应丢失由客户端显式查询/恢复。

## 保留与否决

保留既有 WS JSON、默认行为、health、端口、mic 优先/轮转/FIFO、背压、错误契约、最终累计文本/时间戳、断线清理和模型链。不实现独立文件网关、HTTP proxy、统一端口迁移、账号平台、模型改造、自动重试/识别重跑、假 socket、重复 owner_id 或新的识别流水线；不重开旧 PR30，也不把后续 HTTP 半成品对外开放。不采用已否决的重复 owner_id、ALLOCATING 状态、12 h 解码上限或自动清理未登记残留。

## 七项路线与依赖

1. **E1 owner**：共享 owner/key、活跃 HTTP 集合、真实 IPC 和共享 PCM 分段；当前增量。
2. **E2 upload**：持久存储、续传 offset、HTTP listener 和受理事务；依赖 E1。
3. **E3 runner**：原文件解码、HTTP job runner、异常监督和结果 sink；依赖 E2。
4. **E4 resources**：配额、源音频清理、重启收敛和活跃引用保护；依赖 E3。
5. **E5 SDK**：显式 HTTP 提交/恢复/查询/领取客户端；可与 E1/E2 并行，集成等待核心链。
6. **E6 QA**：跨进程、真实 producer、重启、并发和旧 WS 回归；依赖 E4 与 E5。
7. **E7 baseline**：部署/协议文档及真实字节、质量、资源基线；依赖 E6。
