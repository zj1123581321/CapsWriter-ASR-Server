---
lane: http-core
id: M3
slug: runner
status: 已完成
owner: pi协调
order: 3
priority: 高
depends_on: [http-core/M2]
merged_pr: 51
---

# 里程碑进度：http-core/M3：HTTP 文件 runner 与结果分派

- **预期产出**：原文件解码、Job runner、异常监督和持久 Result sink；受理后脱离连接继续识别。
- **当前范围**：复用 E1 PCM/worker 链，不传完整文件给 worker，不改模型算法或 WS 结果协议。
- **关键决策**：HTTP Task 为 owner_kind=http、socket_id 为空、task_id 为稳定 job_id；任一段失败即整 Job FAILED。
- **已知阻塞**：无；E3 的存储/HTTP producer 契约已合入并被 runner 复用，R7 共享上限统一计数也已合入。
- **推进前必须拿到的证据**：
  - [x] 真实文件解码 argv、PCM producer payload 和 Queue/Result 跨进程契约；环境：本地隔离 tmp/PID，入口：HTTP commit 后 runner。
    证据：tests/test_http_file_runner.py 用真实 ffmpeg 包装脚本记录 argv/env，四种真实容器（mp3/aac/m4a/opus）解码后段边界与样本数逐条核对，子进程记录真实收到的 Task 与真实发出的 Result。
  - [x] 关闭原客户端后由另一 HTTP 连接领取完整结果；环境：本地隔离服务，入口：HTTP status/result。
    证据：真实 SDK 提交（上传客户端随调用关闭）后，另一条 HTTP 连接读到 DONE 并领取与 worker 最终 Result 逐字段一致的持久结果。
- **完成条件**：runner、结果持久化和未知异常监督均 fail-loud，旧 WS/health/信号回归通过。
  - [x] 结果超限、worker 失败、ffmpeg 非零退出、SIGTERM 与崩溃窗口均有真实进程证据；未知后台异常让进程非零退出。
  - [x] 重启收敛为 FAILED[server_restarted] 且不自动重跑。
  - [x] 两个 websockets 版本全量绿。
  - [x] 冷审查四项修复：全局运行闸门（P1-2）、QUEUED→RUNNING 持久转移（P2-2）、
    `record_result` 校验 tokens 拼接与 text_accu 一致（P2-3）、推理段超时先落库再退出（P2-1）。
- **完成证据**：PR #51 已合并，合并提交 `67b8b65247c9f5310d5c8346cf579416e19851fc`（`git show 67b8b65 --stat` 可核对改动面：新增 `core/server/http_file_runner.py` 与 `tests/test_http_file_runner.py`，harness 增补真实子进程 Task/Result 记录）。本里程碑的验收记录来源为该 PR 的真实 ffmpeg 解码、真实 SDK 上传、真识别子进程与真 multiprocessing 队列证据；后续 R7「共享上限跨存储统一计数、WS 固定预留 2 个名额」随 PR #58 合并于 `e066930ef38aabe8e5051c9256463646f62a7186`，补上排队 HTTP Job 的容量口径，不改变本里程碑结论。

## 路线图审计（2026-10-03 / PR51 收口，master e066930）

- **里程碑真完成了吗？**：真完成。原容器流式解码、文件 runner、持久 Result sink 与结果分派均有真实进程证据（真实 ffmpeg argv/env、四种真实容器段边界与样本数、跨进程 Task/Result、另一连接领取完整结果）；缺依赖的旧 WS/health/信号回归未变。
- **下一个目标还是对的吗？**：对。下一个是 M4 资源边界与重启清理，直接消费已固定的共享上限原语（`count_active_tasks`、`WS_RESERVED_SLOTS`）与持久状态字段。
- **有没有漏掉的里程碑？**：没有。M4 资源/GC、M6 边界 QA、M7 部署与质量基线仍待；幂等状态码双路径等 P2 backlog 不在本里程碑范围。
- **新证据是否改变了工作顺序？**：没有。R7 把容量口径统一到「内存非终态 + SQLite QUEUED/RUNNING 去重」，M4 的配额/预留工作因此有单一原语可依。
- **done 的定义还成立吗？**：成立。整条路线（12 组真实边界 QA、同源字节/质量基线、平台部署）尚未完成，不能因 M3 完成就宣称 HTTP 整体可部署。
- **审计结论**：M3 关闭，按目标索引契约激活 M4；`GOALS.md` 派生表与本段手写审计由主脑统一更新（本卡不改 `GOALS.md`）。
