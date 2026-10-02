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
- **已知阻塞**：无；E2 的存储/HTTP producer 契约已合入并被 runner 复用。
- **推进前必须拿到的证据**：
  - [x] 真实文件解码 argv、PCM producer payload 和 Queue/Result 跨进程契约；环境：本地隔离 tmp/PID，入口：HTTP commit 后 runner。
    证据：tests/test_http_file_runner.py 用真实 ffmpeg 包装脚本记录 argv/env，四种真实容器（mp3/aac/m4a/opus）解码后段边界与样本数逐条核对，子进程记录真实收到的 Task 与真实发出的 Result。
  - [x] 关闭原客户端后由另一 HTTP 连接领取完整结果；环境：本地隔离服务，入口：HTTP status/result。
    证据：真实 SDK 提交（上传客户端随调用关闭）后，另一条 HTTP 连接读到 DONE 并领取与 worker 最终 Result 逐字段一致的持久结果。
- **完成条件**：runner、结果持久化和未知异常监督均 fail-loud，旧 WS/health/信号回归通过。
  - [x] 结果超限、worker 失败、ffmpeg 非零退出、SIGTERM 与崩溃窗口均有真实进程证据；未知后台异常让进程非零退出。
  - [x] 重启收敛为 FAILED[server_restarted] 且不自动重跑。
  - [x] 两个 websockets 版本全量绿。
