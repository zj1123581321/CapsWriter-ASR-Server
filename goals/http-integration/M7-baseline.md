---
lane: http-integration
id: M7
slug: baseline
status: 未开始
owner: pi协调
order: 2
priority: 中
depends_on: [http-integration/M6]
merged_pr: null
---

# 里程碑进度：http-integration/M7：部署、协议与质量基线

- **预期产出**：HTTP/WS 协议、部署、数据流、运维说明和真实字节/质量/资源基线。
- **当前范围**：记录同源样本、应用层字节、弱网重发、归一化结果、耗时、CPU/RSS 与未知项；不宣称未经测量的节省或生产上线。
- **关键决策**：HTTP 显式启用且初始化失败即失败；旧 WS 端口和默认行为保留；不把 systemd is-active 当业务健康。
- **已知阻塞**：等待 E6 的真实行为证据和代表性样本。
- **推进前必须拿到的证据**：
  - [ ] 文档从裸 shell/CI 隔离环境实际走新旧两入口并核对 health/版本/结果；环境：CI 与测试环境，入口：部署命令和客户端。
  - [ ] 同源 MP3/AAC/M4A/Opus 与既有 WS 基线的应用字节、重发、质量和资源数据；环境：隔离测试机，入口：基线脚本。
- **完成条件**：文档与实现一致，所有数字注明测量环境；无生产部署或未经测量的性能承诺。
