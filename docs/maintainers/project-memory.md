# 项目记忆

本文件记录 CapsWriter-Offline-with-AI 的仓专属事实，供以后在本仓工作的会话阅读。
内容于 2026-09-03 从 Claude Code 自动记忆迁出，技术细节原样保留，不替换现行设计文档。
条目可能已过期，阅读时请对照代码与提交日期。

## ASR 负载均衡代理架构

本条事实于 2026-06-30 得到（归档 frontmatter 无 `modified` 字段，取正文中的部署/修正日期）。

ASR 负载均衡代理 v3 已实现并部署（2026-06-30, PR#1 700e9b5 + PR#2 4be000f），路由公式于 2026-06-30 修正（d346b4c）。

关键架构决策：
- **Per-task 独立后端 WS 连接**：Server 的 `AudioCache`（`ws_recv.py:27`）是 per-websocket 的，不按 task_id 隔离。多个 task 共享一条后端连接会导致音频数据混合损坏。这是 Codex cross-model review 发现的致命缺陷，推翻了最初的"连接池"设计。
- **Least-connections 路由公式（2026-06-30 修正）**：`score = (active_tasks + 1) / weight + latency * 1e-6`。active_tasks 为主确保任务均匀分散到所有后端，latency 仅作同等负载时的 tiebreaker。**之前的公式** `(active_tasks + 1) * latency / weight` 因 latency 方差 10-300x 导致所有任务堆积到延迟最低的单台后端（Mac Studio），其他设备闲置——这是 tg-archiver 3 路并发测试发现的生产问题。
- **EWMA 截断重置（2026-06-30 修复）**：`record_processing_latency` 中 latency > 300 被丢弃时，同时重置 `avg_latency=0` 和 `latency_samples=0`。之前截断不重置导致僵尸 avg_latency 永久惩罚后端。
- **EWMA 样本过期**：`last_latency_time` 追踪最后一次样本时间，超过 `latency_ttl_seconds`（默认 300s）的旧样本视为过期，重置为冷启动状态（回退 peer median）。防止后端长时间空闲后旧延迟信号失真。
- **网络感知诊断**：记录任务端到端延迟（`monotonic()` 从 `_open_task_session` 入口到收到 `is_final`）和推理延迟双指标日志。端到端延迟仅日志不喂 EWMA。
- **Cooldown 恢复**：unhealthy 后端 cooldown_seconds（默认 60s）后自动重置 consecutive_failures=0 和 healthy=True。全部 unhealthy 时降级路由到 active_tasks 最少的后端。
- **不用 ping/pong 做健康检查**：Server 推理时可能阻塞 event loop，ping 超时会误杀正常工作的后端。
- **max_size=None**：与 Server 保持一致，不限制 WS 消息大小。
- **音频重发已 defer**：Codex 指出协议复杂度高（task_id 重写、部分结果去重、内存管理），待权重路由稳定后再做。详见 TODOS.md。

**Why:** 原 v3 公式虽然修复了零分陷阱，但 latency 方差（异构设备+异构音频时长）导致 latency 完全压制 active_tasks 的负载均衡效果。least-connections 是 nginx/HAProxy 的标准做法，对批量转录场景最优。

- **`/status` HTTP 端点（2026-06-30, 145d814）**：在 WebSocket 同端口通过 `websockets` v16 `process_request` 回调拦截 HTTP 请求。`GET /status` 返回 JSON，`GET /status?html` 返回格式化页面。显示后端原始字段（active_tasks、healthy、avg_latency、weight 等），不显示计算后 score（因 least-connections 下 score 不直观）。任务完成历史用 `collections.deque(maxlen=1000)`，记录成功和失败任务。deque 放在 `ProxyServer` 层（全局唯一），因 `TaskRouter` 是 per-client 的。
- **日志改进（2026-06-30, 71deb45）**：`datefmt` 加日期前缀；`TruncatingFileHandler` 替换为标准 `RotatingFileHandler(backupCount=5)`，旧类已删除。

**How to apply:** 路由公式现在以 active_tasks 为主，不再需要担心 latency 压制。EWMA 仍保留用于日志和 tiebreaker，截断时自动重置避免僵尸值。如需让快机器自动多分任务，用 config_proxy.py 中的 weight 配置。`curl http://proxy:6020/status` 查看集群状态。
