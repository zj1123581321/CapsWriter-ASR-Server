# HTTP 文件任务：公开功能契约

## 范围状态

用户已在 2026-10-01 明确授权拆卡落地完整实现；本文只记录公开契约和本路线图，不表示 HTTP 文件任务已经可用。本次 E1 只交付任务归属、跨进程传播和共享有界 PCM 分段基础，HTTP listener、存储、runner、SDK 仍保持关闭并留给后续增量。独立规划终审历史没有完整覆盖，不能把规划记录当成实现或质量通过。

## 用户可见目标

完整文件上传在未来应能在收到受理确认后脱离客户端连接，服务端继续识别；客户端之后用显式凭据查询并领取结果。上传支持显式续传，服务重启后保留已确认前缀和任务记录；识别中断必须可见，不自动重跑。实时 WebSocket 仍是默认入口，协议、端口、健康检查和断线清理不因 HTTP 改变。

## 任务与归属契约

- `Task` 和 `Result` 都带 `owner_kind`，默认值为 `ws`，因此旧构造仍表示 WebSocket 任务。
- WebSocket 任务使用 `socket_id` 作为派生 owner ID；HTTP 任务的 `socket_id` 必须为空，使用稳定 `task_id`（未来为稳定 `job_id`）作为派生 owner ID。
- 所有主进程、子进程、会话、背压和结果分发消费者使用同一个三元 key：`(owner_kind, derived_owner_id, task_id)`。不另存重复 `owner_id`，不伪造 WebSocket。
- 主进程通过受管理的 `active_http_jobs` 集合向 worker 传播 HTTP 活跃任务。HTTP 任务只有登记在该集合中才能入队或继续推理；集合中不存在的任务必须被丢弃，不能复活或污染 WS。
- 父进程结果分发为 HTTP 保留显式可注入 sink；没有注入真实持久消费者时 fail-fast，不把 HTTP 结果伪装成 WS 或静默丢弃。E3 才提供持久消费者。

## PCM 分段契约

`core/server/connection/segmenter.py` 的 `CutFinder` 仍负责切点吸附；`core/server/segmenter.py` 只提取有界 PCM 缓冲、切点搜索、偏移、重叠、段长预算和最终段语义。当前真实 caller 是 WS 接收层，后续 HTTP runner 复用同一输出。

每个段是有限大小的 little-endian、16 kHz、单声道 float32 PCM。切点吸附保持 VAD 优先、RMS 降级、搜索窗口、最大切点和引擎单段上限；offset 按不含 overlap 的 stride 累加，段数据包含 overlap。中间段 `is_final=false`，末帧只产生一个剩余 PCM 的 `is_final=true` 段。WS 自己负责连接取消和 inflight semaphore，HTTP runner 自己负责持久确认；二者不共享连接取消或持久 ACK。

## 未来 HTTP 状态机与提交顺序

上传状态为 `UPLOADING -> COMMITTED`，过期上传为 `EXPIRED`；任务状态为 `QUEUED -> RUNNING -> DONE/FAILED`。HTTP 默认不监听、不注册业务入口，不因缺少 HTTP 依赖回退到 WS。未来显式启用时，上传必须先校验身份、大小、指纹和容量，再有限写入；只有文件字节和任务记录可靠提交后才确认受理。受理后再解码为 PCM 段并走现有 worker；完整结果与 DONE 必须一起可靠提交。

未来接口使用二进制请求体而非 JSON/Base64 文件体，续传 offset 以服务端已确认字节为准。客户端在首个请求前保存高熵任务令牌，服务端只保存令牌指纹；任务编号用于查找，不单独赋权。网络库不自动 retry、redirect 或 fallback WS，响应丢失由客户端显式查询/恢复。任何解码失败、中间段失败、结果提交失败都不能发布残缺成功。

## 资源与失败边界

后续实现沿用工程定稿的保守起点：单文件 1 GiB、请求块 1 MiB、读取块 64 KiB、HTTP handler 16、同时 body 操作 2、未完成上传 32、HTTP 运行中任务 1、全局 WS/HTTP 活动任务 8、每任务 4 个在途 PCM 段、完成结果 JSON 64 MiB。未完成上传 TTL 为创建或最近成功确认后 7 天；源音频在终态 7 天且无活跃引用后才允许清理，任务记录和结果不删除。容量不足、未知任务、旧 offset、错误凭据和持久化不可用都必须给出可见错误；不默认 offset=0，不静默 catch-all。

## 保留与否决

保留既有 WS JSON、默认行为、health、端口、mic 优先/轮转/FIFO、背压、错误契约、最终累计文本/时间戳、断线清理和模型链。不实现独立文件网关、HTTP proxy、统一端口迁移、账号平台、模型改造、自动重试/识别重跑、假 socket、重复 owner_id 或新的识别流水线；不重开旧 PR30，也不把后续 HTTP 半成品对外开放。

## 七项路线与依赖

1. **E1 owner**：共享 owner/key、活跃 HTTP 集合、真实 IPC 和共享 PCM 分段；当前增量。
2. **E2 upload**：持久存储、续传 offset、HTTP listener 和受理事务；依赖 E1。
3. **E3 runner**：原文件解码、HTTP job runner、异常监督和结果 sink；依赖 E2。
4. **E4 resources**：配额、源音频清理、重启收敛和活跃引用保护；依赖 E3。
5. **E5 SDK**：显式 HTTP 提交/恢复/查询/领取客户端；可与 E1/E2 并行，集成等待核心链。
6. **E6 QA**：跨进程、真实 producer、重启、并发和旧 WS 回归；依赖 E4 与 E5。
7. **E7 baseline**：部署/协议文档及真实字节、质量、资源基线；依赖 E6。
