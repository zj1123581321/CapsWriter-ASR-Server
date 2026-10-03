阶段：固定源码、规格、完整新增测试已审；真实短 systemd 进程已跑 HTTP / ffmpeg / runner / SQLite 存储失败场景。
结论：DONE 终态 sink 后，真实 ffmpeg decoder 与 runner active 引用期间可保留已达保留阈值的源；unlink PermissionError 经 cleanup done callback 进入 fatal 并日志，但 CapsWriterServer Python 主进程与 unit 仍 active，HTTP/WS 端口均已拒绝连接（connect errno 111），识别 worker 仍在 cgroup。该“父服务未退出”是 P1 候选，待其他进程场景交叉确认。
关键决策与否决：真实样本为 2.5 秒合成 WAV（80,044 B），ffmpeg /proc argv 与 cgroup 指向自有 probe unit，worker 子进程跨队列消费 160,000 B f32le PCM；recognizer/aligner/VAD 权重全 stub，SQLite 与 network/runner/ffmpeg 为真。首轮两次夹具失败由探针门控/缺 CutFinder/aligner 配置导致，不能作产品证据；修正后 storage-error 场景满足前置条件。OCR 完整 envelope 为 skipped，reason=primary=status_missing; backup:deepseek=status_missing，不按干净计。
唯一下一步：完成真实正常 SIGTERM 与 cleanup 在途/排队/睡眠时主动 stop 交错场景，记录日志/退出状态后停止并确认只清理本卡自有 probe unit。
