# E2 进度：HTTP 上传与持久受理

- **阶段**：原监督/准入/持久合同续修已落地，待主脑验收与远程 CI；本卡未合并、未部署。
- **本段结论**：本机持久存储（SQLite WAL/FULL/FK/timeout=0 + 单目录 OS 独占锁）、可信续传 offset、六个 HTTP route 与唯一 Job 受理事务已在 `core/server/http_store.py` 与 `core/server/http_server.py` 落地。HTTP 默认关闭，启用需 `CW_HTTP_PORT` + 稳定绝对 `CW_HTTP_DATA_DIR`，初始化错误 fail fast 非零。真实 SDK 提交验证：字节真实落盘、库内 offset 与恢复文件三方一致；无推理协调者时 `commit` 明确 `503 inference_unavailable`，已存在 Job 重复 `commit` 仍返回同一 `job_id`。
- **锁定否决**：未新增任何 feature flag；未在旧 WS listener 上加 HTTP 处理；未引入自动 retry/redirect/WS fallback；未删库重建；未在事件循环做 fsync；未使用无界 default executor；未改 SDK、proxy 或 GOALS 全局层。
- **2026-10-01 续修**：未知 HTTP/I/O/WS 运行错误现在进入实际进程监督并非零退出；16 handler、2 body 与 32 I/O 名额均无等待队列溢出，超限 429；POSIX 使用 `lseek+write` 和明确 `0700/0600` 权限；结果发布只接受匹配 Job 的完整 final，DONE/FAILED 不可覆盖；`options` 仅缺省/`None` 归一化为空对象。正式完整测试命令已与 CI 的 `aiohttp==3.14.3`、`httpx==0.28.1` 对齐。
- **续修边界**：未知 HTTP fatal、取消后未知 I/O fatal、WS RuntimeError、真实 handler 取消两序各五次、上传两个崩溃窗口与结果跨新进程读取均由隔离测试证明；只在 Linux/POSIX 做了真实运行，Windows/macOS 未实跑，Windows ACL 保持未验证。M3 runner、M4 GC、真实推理不在本卡。
- **正式全量 Verify（2026-10-01）**：按本页上方完整命令（含 `aiohttp==3.14.3` 与 `httpx==0.28.1`）运行 `tests/`，结果 `305 passed, 3 skipped, 86 warnings in 109.46s`。3 个 skip 是 ForceAligner 后端/模型缺失两项及 silero-VAD 模型或 onnxruntime 缺失一项；HTTP 测试均实际运行，无缺依赖跳过。`tests/test_http_config.py` 的子进程以仅含 PATH/HOME/PYTHONPATH/LANG 与测试输入的环境运行，不继承 PI/DELEGATE 身份。
- **下一步**：主脑安排独立审查（输入不含本卡实现报告推理）与完整 CI；M3 装配真实文件 runner 后把 `HttpServer.inference_available` 接通并落 `result`/`DONE` 同事务；M4 补 GC 与精确物理/残留/WAL 核算。
