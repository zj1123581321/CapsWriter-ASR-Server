# E1 owner 进度

- **当前阶段**：implementing；E1 代码和隔离验收正在进行，HTTP 业务入口仍关闭。
- **本段结论**：`Task`/`Result` 已带默认 `owner_kind=ws`；稳定 key 为 `(owner_kind, derived_owner_id, task_id)`；HTTP 的空 `socket_id` 与稳定 job ID 可通过真实 multiprocessing Queue 进入 worker、生成 Result，并由父进程注入 sink 后释放活跃集合。WS 已接入共享有界 PCM 分段器，保留 CutFinder、切点吸附、偏移、重叠、段长预算、inflight slot 和 final 语义。
- **锁定与否决**：锁定不伪造 socket、不另存 owner_id、HTTP 活跃集合跨进程传播、父进程 sink 显式注入、HTTP 默认关闭；保留旧 WS 协议/health/端口/断线清理/背压/最终累计结果。继续否决独立网关、HTTP proxy、模型改造、自动 retry/重跑、持久化和 SDK 提前实现。
- **下一步**：
  1. 修完 owner/key 迁移后的旧 WS 错误、调度和协议回归。
  2. 连续运行背压/并发相关测试 5 次，核对无会话子进程的实际 argv/env。
  3. 提交并推送 E1 小步 commit，尝试创建 draft PR，记录远端状态和 CI 结论。
  4. 补齐 7 个路线目标文件、开发数据流文档和本卡完整报告；不把未完成目标标为 DONE。
