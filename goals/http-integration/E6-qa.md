---
lane: http-integration
id: E6
slug: qa
status: 未开始
owner: pi协调
order: 1
priority: 高
depends_on: [http-core/E4, http-sdk/E5]
merged_pr: null
---

# 里程碑进度：<!-- http-integration/E6：HTTP 边界 QA -->

- **预期产出**：12 组 producer、重启、并发、错误和旧 WS 验收，含至少五次并发回归。
- **当前范围**：只验证隔离服务与真实客户端/文件/进程边界，不访问生产端口、模型或录音。
- **关键决策**：反向注入必须以 AssertionError 变红；ImportError/恒真断言不算；CI/bare shell 消费环境都要覆盖。
- **已知阻塞**：等待 E4、E5 合并和 HTTP 业务入口可用。
- **推进前必须拿到的证据**：
  - [ ] 真实 SDK/CLI producer body、subprocess argv/env、文件字节和 Queue pickle payload 均有断言；环境：CI 标准/tmp，入口：pytest tests/。
  - [ ] 同一隔离服务执行并发/取消/重启矩阵至少 5 次且旧 WS 回归通过；环境：CI hosted runner 与无会话裸 shell。
- **完成条件**：12 组验收均有可追溯 fixture/命令/环境，真实 skip 原因单列，不能用 draft 绿代替行为证据。
