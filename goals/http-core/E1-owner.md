---
lane: http-core
id: E1
slug: owner
status: 进行中
owner: pi协调
order: 1
priority: 高
depends_on: []
merged_pr: null
---

# 里程碑进度：<!-- http-core/E1：共享任务归属与 PCM 分段基础 -->

- **预期产出**：Task/Result owner_kind、稳定派生 key、跨进程 HTTP 活跃任务门控，以及 WS/未来 HTTP 共用的有界 PCM 分段核心；旧 WS 契约不变。
- **当前范围**：实现 E1 代码、真实 multiprocessing producer/consumer fixture、共享分段回归和公开契约文档；不实现 HTTP listener、store、runner、SDK 或生产部署。
- **关键决策**：默认 owner_kind=ws；HTTP 使用空 socket_id 和稳定 job_id；不保存重复 owner_id、不伪造 socket；HTTP 结果由父进程注入 sink 接收。
- **已知阻塞**：无行为阻塞；draft PR/远端 CI 需在首个非空提交后核实。
- **推进前必须拿到的证据**：
  - [ ] 实际 Task.data、offset、owner_kind、socket_id、is_final 经 multiprocessing Queue 到 worker 的断言；环境：本地隔离 Python 3.12，入口：tests/test_owner_ipc.py。
  - [ ] WebSocket producer 通过共享分段器发出有界 PCM Task，队列长度和旧背压回归通过；环境：本地隔离 port 0，入口：tests/test_shared_segmenter.py、tests/test_backpressure.py。
  - [ ] CI 标准全量 Verify-Command 通过；环境：CI 标准无会话消费者，入口：pytest tests/。
- **完成条件**：代码、测试、设计、QA、进度文件同一合并增量；所有旧 WS 回归和真实 IPC 断言通过，HTTP 仍默认关闭。
