阶段：整份新增测试与竞态窄测完成，均通过；开始最小变异 AssertionError 红验。
结论：指定隔离依赖下完整 `tests/test_http_cleanup.py` 为 5 passed（2.94s）；`test_upload_io_and_cleanup_share_one_worker_five_times` 作为独立窄测重复五轮，5/5 passed。仅代表新增单文件和该竞态样本。
关键决策与否决：命令使用 uv --no-project、多项任务卡锁定依赖，未触碰主 `.venv`。真实 systemd/decoder/cleanup probe 结果与上个进度一致；OCR 状态仍是 skipped，不能算干净。
唯一下一步：在临时 scratch worktree 仅把 cleanup active-job 保护 guard 改成失效形态，先 grep 核实注入，再运行锁定周期集成 case，要求以 AssertionError 转红；随后完成 verdict/report、提交和远端核验。
