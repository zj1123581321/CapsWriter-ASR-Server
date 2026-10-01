# E2 进度：HTTP 上传与持久受理

- **阶段**：实现完成，待主脑独立审查与远程 CI；本卡未合并、未部署。
- **本段结论**：本机持久存储（SQLite WAL/FULL/FK/timeout=0 + 单目录 OS 独占锁）、可信续传 offset、六个 HTTP route 与唯一 Job 受理事务已在 `core/server/http_store.py` 与 `core/server/http_server.py` 落地。HTTP 默认关闭，启用需 `CW_HTTP_PORT` + 稳定绝对 `CW_HTTP_DATA_DIR`，初始化错误 fail fast 非零。真实 SDK 提交验证：字节真实落盘、库内 offset 与恢复文件三方一致；无推理协调者时 `commit` 明确 `503 inference_unavailable`，已存在 Job 重复 `commit` 仍返回同一 `job_id`。
- **锁定否决**：未新增任何 feature flag；未在旧 WS listener 上加 HTTP 处理；未引入自动 retry/redirect/WS fallback；未删库重建；未在事件循环做 fsync；未使用无界 default executor；未改 SDK、proxy 或 GOALS 全局层。
- **下一步**：主脑安排独立审查（输入不含本卡实现报告推理）与完整 CI；M3 装配真实文件 runner 后把 `HttpServer.inference_available` 接通并落 `result`/`DONE` 同事务；M4 补 GC 与精确物理/残留/WAL 核算。
