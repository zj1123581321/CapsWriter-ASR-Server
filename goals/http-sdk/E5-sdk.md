---
lane: http-sdk
id: E5
slug: sdk
status: 未开始
owner: pi协调
order: 1
priority: 中
depends_on: []
merged_pr: null
---

# 里程碑进度：<!-- http-sdk/E5：显式 HTTP SDK/CLI -->

- **预期产出**：显式 HTTP submit/resume/status/result SDK 与 CLI；旧 WS 默认调用不变。
- **当前范围**：保存恢复凭据、按服务端 offset 续传、错误可恢复信息和真实 HTTP 请求 producer；不把 HTTP fallback 到 WS。
- **关键决策**：HTTPX retries=0、禁止自动 redirect；token 不进 URL、日志或普通 repr；客户端不要求 ffmpeg。
- **已知阻塞**：HTTP wire contract 需与 E2/E3 集成核对。
- **推进前必须拿到的证据**：
  - [ ] 首请求前真实恢复文件字节/权限和实际 Authorization header 经过隔离 HTTP server 核对；环境：本地裸 shell/tmp，入口：SDK submit/resume。
  - [ ] 旧 CLI FILE/WS 与显式 HTTP CLI 分别运行；环境：CI 标准和本地隔离，入口：CLI 命令。
- **完成条件**：提交/恢复/查询/领取契约完整、无隐式 retry/fallback，旧 SDK 测试全绿。
