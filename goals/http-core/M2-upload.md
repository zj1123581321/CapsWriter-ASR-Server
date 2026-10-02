---
lane: http-core
id: M2
slug: upload
status: 已完成
owner: pi协调
order: 2
priority: 高
depends_on: [http-core/M1]
merged_pr: 38
---

# 里程碑进度：http-core/M2：HTTP 上传与持久受理

- **预期产出**：显式 HTTP listener、受限落盘、可信续传 offset、上传幂等和唯一 Job 受理事务；默认仍关闭。
- **当前范围**：只在 E1 合并后实现 HTTP 上传边界，不改 WS listener、proxy 或 SDK 默认行为。
- **关键决策**：二进制请求体、Bearer capability、无隐式 retry/redirect/fallback；确认晚于文件与任务记录可靠提交。
- **已知阻塞**：M1已合并，接口/依赖/资源契约已固定；本阶段开始受限上传与存储实现，真实模型runner属于M3。本条激活不代表HTTP已可用。
- **推进前必须拿到的证据**：
  - [x] 实际 HTTP producer body、headers、恢复文件字节和文件落盘字节一致；环境：本地隔离 port 0/tmp，入口：真实 SDK/CLI 请求；最终 PR38 全量与官方 merged-main 预检均通过。
  - [x] 真实服务进程重启后 confirmed_offset 可继续；环境：本地隔离服务进程，入口：HTTP upload/resume；重启收敛与部分前缀保留由 `tests/test_http_store.py` 与真实子进程测试锁定。
- **完成证据**：PR #38 已合并（merge `b7408fcd4ecdb5c121abc79ea14756e7c35e5fae`）；最终 head `e1fa9796a314f593e149c050cb92bae5639911b9`，独立复核无 P1，官方预检 merged-main `356 passed / 3 skipped`，Gate 经正式 disposition 后聚合通过。
- **完成条件**：六个 HTTP 基础操作和五个提交窗口有跨进程测试，HTTP 未显式启用时旧 WS 全量不变。
