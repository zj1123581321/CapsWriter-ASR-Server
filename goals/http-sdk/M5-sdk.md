---
lane: http-sdk
id: M5
slug: sdk
status: 已完成
owner: pi协调
order: 1
priority: 中
depends_on: []
merged_pr: 36
---

# 里程碑进度：http-sdk/M5：显式 HTTP SDK/CLI

- **预期产出**：显式 HTTP submit/resume/status/result SDK 与 CLI；旧 WS 默认调用不变。
- **当前范围**：保存恢复凭据、按服务端 offset 续传、错误可恢复信息和真实 HTTP 请求 producer；不把 HTTP fallback 到 WS。
- **关键决策**：HTTPX retries=0、禁止自动 redirect；token 不进 URL、日志或普通 repr；客户端不要求 ffmpeg。
- **已知阻塞**：客户端增量已合PR36；真实ASR HTTP端点集成与Windows ACL由M6验收，不能把客户端fixture外推为全服务通过。
- **推进前必须拿到的证据**：
  - [x] 首请求前真实恢复文件字节/权限和实际 Authorization header 经过隔离 HTTP server 核对；环境：本地裸 shell/tmp，入口：SDK submit/resume。
  - [x] 旧 CLI FILE/WS 与显式 HTTP CLI 分别运行；环境：CI 标准和本地隔离，入口：CLI 命令。
- **完成条件**：提交/恢复/查询/领取契约完整、无隐式 retry/fallback，旧 SDK 测试全绿。

- **验收证据**：PR36 head454873f、合并a7ad711；本地全量248通过/3资源跳过，真实已安装wheel与无源码注入CLI网络30项×5通过；最低websockets15.0.1/最新两个CI矩阵SUCCESS。额外幂等请求/文件名与URL低风险边界接受说明在PR36正文，无部署。
