# M4-C2 关键证据

## 周期触发与真实引用保护

- 生产路径：`core/server/http_server.py::HttpServer.serve` 启动 `_source_cleanup_loop`；每轮在网络 loop 获取 `file_runner.active_jobs` 只读快照，再将 `HttpStore.cleanup_terminal_sources` 投给唯一 `HttpIoWorker`。`stop` 先停循环；当一轮已提交 I/O 时等待它完成，再按既有顺序停止 runner、listener、worker。
- 清理谓词：`core/server/http_store.py::HttpStore.cleanup_terminal_sources` 只查 `jobs.state IN (DONE, FAILED)`、`terminal_at IS NOT NULL` 且 `terminal_at <= now - SOURCE_RETENTION_SECONDS` 的已登记行。活跃 runner job 跳过；每行只 unlink `uploads.source_name`。ENOENT 可幂等通过，其他权限/存储错误上抛。
- 释放依据：沿用 C1 的源文件存在性计费，不写额外释放标记或账本。unlink 后 source-presence 查询遇到 ENOENT 就退出额度；若文件仍存在则继续计费，下一周期可再清理。M4 plan §3 曾写「unlink 后提交释放记录」，本卡锁定决策明确禁止新增持久释放状态，故按现有计费真源实现。
- 上传过期：同一 worker 操作将 `expires_at <= now` 的 UPLOADING 行更新为 EXPIRED；不 unlink partial。source-presence 扫描仍按文件是否存在计费，正确凭据之后通过原 GET 路由得到 410。
- 真实 producer 测试：`tests/test_http_cleanup.py::test_periodic_cleanup_waits_for_real_runner_reference_then_keeps_result` 经 TCP POST/PATCH/commit 发送已知源字节，runner 实际入队任务并由真实 result sink 将完整 Result 持久化。测试确认 SQLite 为 DONE 且 terminal_at 已过期、`runner.active_jobs` 仍含 job、decoder.close 正在阻塞；多个周期后源仍在。释放 decoder 后，周期循环在超时内删除同一文件，GET job 报 `source_available=false`，GET result 返回完整原 payload。
- 负向对照：周期保留 6 天终态（即使 created_at 已老 20 年）、非终态、terminal_at 缺失、UPLOADING/EXPIRED partial 与未登记 `junk.bin`；只释放两条七天以上终态源的声明额度，Job、Result、upload 行和结果 payload 字节不变。到期 partial 被改为 EXPIRED 后字节仍在，TCP GET 返回 410。
- 阈值矩阵：`tests/test_http_cleanup.py::test_store_cleanup_uses_terminal_boundary_and_keeps_every_other_source` 固定 `now`，断言 `terminal_at == now - 7 天` 和更旧可删、比边界新 0.5 秒不可删；DONE 与 FAILED 分别覆盖。
- 并发和停机：`test_upload_io_and_cleanup_share_one_worker_five_times` 对真实 append、commit、record_result 各重复 5 次；屏障期间确认操作正在执行、cleanup 在 mailbox 等候，放行后 ACK/字节/终态与清理顺序一致。`test_shutdown_waits_for_inflight_cleanup_io` 确认 stop 等 worker 操作结束并清空 future 后才关闭 executor。
- fatal 证据：`test_cleanup_storage_failure_uses_fatal_serve_chain` 对登记文件注入 `PermissionError`；`serve()` 在期限内以该异常失败，`server.fatal` 保留失败、源和元数据仍在。
- TDD 红输出：修正异步队列夹具后，旧实现以 `AssertionError: runner 释放引用后的下一轮周期清理没有删除已到期源` 失败；没有导入、语法或 fixture 错误。
- 绿输出：固定依赖下 `python -m pytest tests/test_http_cleanup.py -q -rs -p no:cacheprovider` 输出 `5 passed in 1.74s`。
- 独立保护断言红验：临时关闭候选行的 `job_id in active_job_ids` 检查后，同一真实 runner 测试以 `AssertionError: 周期清理在 decoder.close 释放前删除了仍被 runner 引用的源` 失败；还原保护后定向套件为绿。

## 全量验证

- 固定版本：卡面完整命令（`websockets==15.0.1`）输出 `451 passed, 3 skipped, 149 warnings in 231.97s`。
- 最新版本：相同全量命令改用 `--with websockets`，解析到 `websockets==17.1`，输出 `451 passed, 3 skipped, 149 warnings in 229.48s`。
- 两次 skip 身份完全一致：`tests/test_aligner_integration.py` ForceAligner backend/model 两项；`tests/test_segmenter.py` 缺 Silero-VAD 模型或 onnxruntime 一项。pytest 未报告 HTTP runner/HTTP decode skip，HTTP tests 均收集执行。
- 149 warnings 是既有 multiprocessing fork 与 websockets `ConnectionClosed.code/reason` deprecation warnings；无测试失败。
- 派发基线不可用（baseline lookup: `gh api request failed`），继承红未能判定；本卡两次全量均无新红。
- C2 分支已推送。PR #62 open/draft，base 是 C1 PR #60 的分支，5 files changed，944 additions/4 deletions。PR #60 仍 open/draft 时不将 #62 标 ready、不合并。

## 仍待验证

- 全量指定验证命令、最新 aiohttp/websockets 组合、真实 collected/skip 结果和最终提交/远端状态。
