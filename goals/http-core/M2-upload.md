---
lane: http-core
id: M2
slug: upload
status: 进行中
owner: pi协调
order: 2
priority: 高
depends_on: [http-core/M1]
merged_pr: null
---

# 里程碑进度：http-core/M2：HTTP 上传与持久受理

- **预期产出**：显式 HTTP listener、受限落盘、可信续传 offset、上传幂等和唯一 Job 受理事务；默认仍关闭。
- **当前范围**：只在 E1 合并后实现 HTTP 上传边界，不改 WS listener、proxy 或 SDK 默认行为。
- **状态说明**：基础实现与续修已提交，仍待独立复核与验收；本项保持进行中，`merged_pr` 继续为 `null`。
- **关键决策**：二进制请求体、Bearer capability、无隐式 retry/redirect/fallback；确认晚于文件与任务记录可靠提交。
- **已知阻塞**：M1已合并，接口/依赖/资源契约已固定；本阶段开始受限上传与存储实现，真实模型runner属于M3。本条激活不代表HTTP已可用。
- **推进前必须拿到的证据**：
  - [x] 实际 HTTP producer body、headers、恢复文件字节和文件落盘字节一致；环境：本地隔离 port 0/tmp，入口：真实 SDK 请求（`tests/test_http_file_tasks.py::test_sdk_upload_reaches_disk_then_commit_is_explicitly_unavailable`）。
  - [x] 重开同一数据目录后 confirmed_offset 与持久结果可继续；环境：本地隔离目录 + 全新 listener/连接；重启收敛与部分前缀保留见 `tests/test_http_store.py::test_restart_converges_queued_and_running_but_keeps_partial_and_done`。
- **本卡证据**（待主脑验收，不自评完成）：
  - PR #38（draft，未合并、未部署）：`card/http-e2-upload-261001`。
  - 本地全量 `pytest tests/`：294 passed / 3 skipped（3 项为既有模型依赖缺失 skip：ForceAligner ×2、silero-VAD）。
  - 启用可见性：默认关闭、坏数据目录/端口冲突/同目录第二实例均真实非零退出，SIGTERM 零退出并释放端口。
  - 文件识别仍不可用：无推理协调者时 `commit` 明确 `503 inference_unavailable`，真实 runner 属于 M3。
  - 共同 `_read_body` 每次等下一块复用 `CW_UPLOAD_IDLE_SECONDS`：create/PATCH/commit 半开 408、两半开释放后新请求成功、慢传总时长大于 idle 仍成功；见 `tests/test_http_file_tasks.py` 与 `tests/test_http_supervision.py::test_real_process_body_idle_timeout_is_not_fatal_and_releases_port`。未把默认 300 冒充实跑 300 秒。
- **完成条件**：六个 HTTP 基础操作和五个提交窗口有跨进程测试，HTTP 未显式启用时旧 WS 全量不变。
