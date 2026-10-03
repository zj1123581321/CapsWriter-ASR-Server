阶段：固定源码、规格、完整新增测试已审；真实短 systemd 进程已跑 HTTP / ffmpeg / runner / SQLite 存储失败场景。
结论：真实 cleanup PermissionError 在运行中经 fatal 日志传播，但其父 Python 与 systemd unit 仍 active、HTTP/WS 已拒连接，且 worker 仍存活；此 P1 候选得到实测支持。另在 cleanup I/O 占用单 worker且真实 GET 请求排队时 SIGTERM，stop 等待至释放、排队 GET 得 404、主动 stop 期间注入的 cleanup PermissionError 明确记入日志，unit 正常退出 0。
关键决策与否决：运行时故障与主动 stop 故障分开判：前者预期触发运行中错误退出；后者处于已收 SIGTERM 的正常退出，日志可见且 queued I/O 被消费，不要求强改退出码。停止 interleaved unit 后确认 unit inactive、probe cgroup 无残留。首轮夹具失败均为测试探针问题，不作产品证据。OCR envelope skipped，reason=primary=status_missing; backup:deepseek=status_missing。
唯一下一步：完成全真 runner/ffmpeg/SQLite 清理 hold→release→unlink、HTTP 结果重放和清理睡眠时正常 SIGTERM 场景，然后运行完整新增测试及红验。
