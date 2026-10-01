# E1 owner 进度

- **当前阶段**：implementing；E1 本地实现和隔离验收完成，等待 draft PR 检查收敛，HTTP 业务入口仍关闭。
- **本段结论**：`Task`/`Result` 已带默认 `owner_kind=ws`；稳定 key 为 `(owner_kind, derived_owner_id, task_id)`；HTTP 的空 `socket_id` 与稳定 job ID 可通过真实 multiprocessing Queue 进入 worker、生成 Result，并由父进程注入 sink 后释放活跃集合。WS 已接入共享有界 PCM 分段器，保留 CutFinder、切点吸附、偏移、重叠、段长预算、inflight slot 和 final 语义。
- **锁定与否决**：锁定不伪造 socket、不另存 owner_id、HTTP 活跃集合跨进程传播、父进程 sink 显式注入、HTTP 默认关闭；保留旧 WS 协议/health/端口/断线清理/背压/最终累计结果。继续否决独立网关、HTTP proxy、模型改造、自动 retry/重跑、持久化和 SDK 提前实现。
- **验证结论**：完整 Verify-Command 为 236 passed、3 skipped、85 warnings、退出码 0；最终 owner/分段裸环境为 10 passed、4 warnings；背压/取消/协议组合连续 5 次均为 26 passed、59 warnings、退出码 0。skip 是现有 VAD 模型/依赖条件，不是 HTTP 业务 skip。
- **下一步**：
  1. 推送最终进度/兼容测试提交，核对远端分支 SHA 与工作树干净。
  2. 查看 draft PR 两个 WebSocket CI 矩阵的真实 conclusion，pending 不当作审查通过。
  3. 写入完整委派报告，分列继承红与新红、红验原输出、边界偏差和最贵步骤。
  4. 将 E1 交回主脑继续 E2；保持七个未完成目标为 active/pending，不把本卡写成 HTTP 已可用。

## 2026-10-01 续修：采样点量化与公开合同

- **当前阶段**：repairing；补齐原重构冻结的 float32 采样点量化，并把 R1–R7 机械合同写入公开设计/QA。HTTP 默认仍关闭，本卡不合并、不部署。
- **本段结论**：`_cut` 的 stride/overlap 恢复为先 `round(seconds * SAMPLE_RATE)` 再乘 4 字节。真实 `_validate_segmentation` + `_submit_segments` 下，固定切点 `5.00001/0.5` 产出 352000 字节，吸附切点 5.12 s 且 overlap `0.50001` 产出 359680 字节；`process_audio_task` 能消费完整样本。未新增对齐抽象、状态或参数限制。公开 `design.md` 补齐 R1–R7 必要 HTTP 接口、持久提交、监督和容量约束；`qa.md` 列出 12 组待测/已锁证明，不声称全部已实测。
- **锁定与否决**：Task/Result kind 与派生 key、旧 WS 协议/背压/时间戳/模型算法不变；合法小数分段参数仍合法。外部 PCM 意见保持 P2，不扩其它意见。继续否决网关、proxy、WS 库迁移、自动 retry/重跑、账号、模型改造、PR30、假 socket、重复 owner_id。
- **原范围偏差**：原卡 Scope-Globs 漏列 `core/server/worker/__init__.py`。该文件是 ProcessManager 向子进程传递 `active_http_jobs` 的必要 caller；主脑已复核并认可必须接线。这是拆卡方遗漏，不是本续修回退项，本轮未改该文件。
- **下一步**：
  1. 全量 Verify-Command 为 243 passed、3 skipped、85 warnings、退出码 0。
  2. 背压/错误/协议/分段组合连续 5 次均为 45 passed、61 warnings、退出码 0。
  3. 本增量保持 draft PR 37，不合并、不部署；交回主脑处理缺 gate 例外。
