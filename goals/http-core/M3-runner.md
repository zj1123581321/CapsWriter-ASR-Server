---
lane: http-core
id: M3
slug: runner
status: 进行中
owner: pi协调
order: 3
priority: 高
depends_on: [http-core/M2]
merged_pr: null
---

# 里程碑进度：http-core/M3：HTTP 文件 runner 与结果分派

- **预期产出**：原文件解码、Job runner、异常监督和持久 Result sink；受理后脱离连接继续识别。
- **当前范围**：复用 E1 PCM/worker 链，不传完整文件给 worker，不改模型算法或 WS 结果协议。
- **关键决策**：HTTP Task 为 owner_kind=http、socket_id 为空、task_id 为稳定 job_id；任一段失败即整 Job FAILED。
- **已知阻塞**：等待 E2 的真实存储/HTTP producer 契约。
- **推进前必须拿到的证据**：
  - [ ] 真实文件解码 argv、PCM producer payload 和 Queue/Result 跨进程契约；环境：本地隔离 tmp/PID，入口：HTTP commit 后 runner。
  - [ ] 关闭原客户端后由另一 HTTP 连接领取完整结果；环境：本地隔离服务，入口：HTTP status/result。
- **完成条件**：runner、结果持久化和未知异常监督均 fail-loud，旧 WS/health/信号回归通过。
