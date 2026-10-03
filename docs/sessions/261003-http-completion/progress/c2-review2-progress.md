阶段：固定源码、规格、完整新增测试和真实短 systemd 进程场景审查完成，转入新增测试全文件和断言红验。
结论：真实 HTTP→SQLite DONE→真实 ffmpeg 160,000 B PCM→跨进程 runner 结果在 decoder.close 持有期间经历两次清理仍保源；放开引用后 unlink，HTTP source_available=false、结果完整可领、create/commit 重放仍是同一 Job；清理睡眠期间真实 SIGTERM 退出 0。运行时 unlink PermissionError 的 unit 却在 listener 停止、HTTP/WS 拒连接后仍 active、worker 仍存活，作为 P1 finding 候选进入 verdict。
关键决策与否决：真实主样本仅 2.5 秒 synthetic WAV，ffmpeg/SQLite/network/runner/SocketManager/worker 均为真实组件，recognizer/aligner/VAD 初始化全 stub。stop-interleaved 中 cleanup error 有明确错误日志、worker pending=2、queued GET 404，退出码 0。OCR 完整 envelope 是 skipped（primary=status_missing；backup deepseek=status_missing）；不引用为干净。
唯一下一步：以要求的 Python 3.12 `uv --no-project --with ...` 运行完整 `tests/test_http_cleanup.py`，再跑概率窄 case 五轮和 scratch AssertionError 红验；随后完成 verdict/report 并提交推送。
