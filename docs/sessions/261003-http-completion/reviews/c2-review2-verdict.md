# C2 独立审查结论

failure-visibility: p1-found

审查范围固定为 `820c3a2ee4fccc1b44187bd99c40cf99c16e1ca2..5134720e058e0e9ae3d3feddebf4942f8bf7ed7a`，仅 C2 源清理、监督与新增测试。未追 HEAD，未读实现报告、前 review 产物或 issue 61 判级，未修改源码、配置或部署。

## 发现

- **P1：cleanup 运行时存储错误会关闭 HTTP/WS 监听，但服务进程和 systemd unit 仍存活。** `core/server/http_server.py:281-301` 将 `unlink` 异常传入 cleanup task 的 done callback 并标记 fatal；`serve()` 随后抛错。整合路径 `core/server/app.py:163-191` 取消两条监听并设非零退出，但异常返回 `start()` 时没有执行 `CapsWriterServer.stop()`；识别子进程管理器与共享 Manager 的清理只在 `app.py:61-93` 的 stop 路径。实测在实际运行 App 中，对已完成且到期源注入 `PermissionError` 后，HTTP 与 WS TCP 连接均被拒绝，`HttpServer.fatal=PermissionError`，Python 主进程与识别 worker 仍在自有 systemd unit cgroup，unit 继续 `active`；人工结束前系统服务监督没有看到进程退出，无法触发正常重启。影响是服务已不可用却被报告存活。修复应让 fatal 异常走完整 App 资源收尾，再以非零状态退出。

## 逐项不变式核对

| 不变式 | 代码与测试 | 新外部证据 / 未知 |
|---|---|---|
| 仅删除 DONE/FAILED、`terminal_at` 非空且达到 7 天的已登记源；新鲜 terminal、NULL、非终态、未登记与 partial 保留；partial 到期持久变 EXPIRED；metadata/results 不删 | `http_store.py:783-805`；`test_store_cleanup_uses_terminal_boundary_and_keeps_every_other_source`（`test_http_cleanup.py:142-242`）与周期整合 case | systemd 主样本在 SQLite 中将真实 Job 的 `terminal_at` 老化后逐周期观察；未登记与 partial 保留，过期 partial 为 EXPIRED。物理盘容量压力未测。 |
| 真实 runner 引用未释放及同 worker 在途 I/O 完成前不能删除；终态落库但仍被引用是保护窗口 | `http_server.py:281-294` 快照真实 `runner.active_jobs` 并将清理送入单 worker；`http_store.py:796-804` 跳过 active；周期测试 `test_http_cleanup.py:276-475`、worker 竞态 case `553-739` |真实 FileSourceDecoder/ffmpeg 处理合成文件：完成结果已进 SQLite、runner 持源且清理至少两周期不删；释放 decoder close 后下一周期 unlink。真实上传/runner 与 SQLite worker 并发 I/O 探针验证同 worker 排队；未加载模型。scratch 变异已确认 guard 注入，再以原行为 `AssertionError: 周期清理在 decoder.close 释放前删除了仍被 runner 引用的源` 转红（`1 failed`）。 |
| 周期任务有退出监督；存储错误 fatal；正常 shutdown 等在途 I/O；主动 stop 期间错误须可见 | `http_server.py:269-320`；`test_cleanup_storage_failure_uses_fatal_serve_chain`（`741-775`）、`test_shutdown_waits_for_inflight_cleanup_io`（`778-816`） | systemd 短进程探针：正常 SIGTERM 在 cleaner 睡眠时退出码 0；stop 与 cleanup I/O/排队 GET 交错时等待释放且 PermissionError 有日志。运行中 PermissionError 造成上述 P1；unit-level 测试没有锁住 App 进程资源收尾，这正是缺口。 |
| source-presence 为删除真源；DONE 结果可查询且完整、FAILED 暴露原错误；重放不新建 Job/不重识别；HTTP disabled 不启 cleaner | `http_store.py:756-770`、`806-817`；`test_http_cleanup.py:230-240, 486-540`；只在 `app.py:142-148` 配置明确启用后构造 HTTP server，清理 task 只在 `HttpServer.serve()` 启动 |真实 HTTP 查询显示 source unavailable、DONE result 200 且 payload 字节一致，幂等 replay 返回原 Job 且 job 数不变。FAILED 的 409/error_code 在隔离测试覆盖。disabled 分支只做静态路径核对，没有额外真实 systemd disabled 探针。 |
| 无 retry/fallback、新池/schema/账本，已知 ENOENT 幂等，其它 unlink 错误 fail-fast | `http_store.py:800-805`，无新重试/池/schema；PermissionError 测试覆盖 | PermissionError 的真实清理路径已实测；没有增加新的持久状态机制。 |

## 验证范围与未知

- `uv run --no-project` 固定任务卡依赖运行整个 `tests/test_http_cleanup.py`：5 passed；`test_upload_io_and_cleanup_share_one_worker_five_times` 单独重复 5 轮：5/5 passed。
- 已按 scratch helper 在固定 H0 临时 worktree 做最小行为红验，核实一行 guard 注入后命中目标 AssertionError；临时树已清理，审查树没有源码修改。
- OCR 完整 JSON 为 `status=skipped`，原因 `primary=status_missing; backup:deepseek=status_missing`，不作为干净结论。派发时主干基线 API 为 `gh api request failed`，继承红无法判定；未据 draft PR 绿色或缺失基线作结论。无意外测试红；scratch 红验是预期行为。
- 本卡不负责全仓 tests 与 C1 formal precheck；真实识别计算和权重初始化遵照卡面 stub，未证明真实模型转写质量或三平台音频兼容。生产外部 probe 使用 2.5 秒合成 WAV，不使用真实录音/模型。
