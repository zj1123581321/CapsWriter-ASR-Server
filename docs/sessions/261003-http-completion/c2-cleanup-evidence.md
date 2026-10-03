# M4-C2 关键证据

## 周期触发与真实引用保护

- 生产路径：`core/server/http_server.py::HttpServer.serve` 启动 `_source_cleanup_loop`；每轮在网络 loop 获取 `file_runner.active_jobs` 只读快照，再将 `HttpStore.cleanup_terminal_sources` 投给唯一 `HttpIoWorker`。`stop` 先停循环；当一轮已提交 I/O 时等待它完成，再按既有顺序停止 runner、listener、worker。
- 清理谓词：`core/server/http_store.py::HttpStore.cleanup_terminal_sources` 只查 `jobs.state IN (DONE, FAILED)`、`terminal_at IS NOT NULL` 且 `terminal_at <= now - SOURCE_RETENTION_SECONDS` 的已登记行。活跃 runner job 跳过；每行只 unlink `uploads.source_name`。ENOENT 可幂等通过，其他权限/存储错误上抛。
- 真实 producer 测试：`tests/test_http_cleanup.py::test_periodic_cleanup_waits_for_real_runner_reference_then_keeps_result` 经 TCP POST/PATCH/commit 发送已知源字节，runner 实际入队任务并由真实 result sink 将完整 Result 持久化。测试确认 SQLite 为 DONE 且 terminal_at 已过期、`runner.active_jobs` 仍含 job、decoder.close 正在阻塞；多个周期后源仍在。释放 decoder 后，周期循环在超时内删除同一文件，GET job 报 `source_available=false`，GET result 返回完整原 payload。
- TDD 红输出：修正异步队列夹具后，旧实现以 `AssertionError: runner 释放引用后的下一轮周期清理没有删除已到期源` 失败；没有导入、语法或 fixture 错误。
- 绿输出：固定依赖下 `python -m pytest tests/test_http_cleanup.py -q -rs -p no:cacheprovider` 输出 `1 passed in 0.37s`。

## 仍待验证

- 七天前后边界、DONE/FAILED、terminal_at 缺失/新鲜、老 created_at、新旧 partial、未登记残留、重复扫描与来源额度释放。
- 后台 stat/unlink 失败是否通过 `serve` fatal 路径失败；停机等待真实在途清理 I/O 与正常释放。
- 全量指定验证命令、最新 aiohttp/websockets 组合和最终提交/远端状态。
