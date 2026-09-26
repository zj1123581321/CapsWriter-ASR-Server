# 更新日志

## 独立服务端阶段

- 仓库提供独立 ASR 服务端和可选 WebSocket proxy；服务端通过 `start_server.py` 启动，proxy 通过 `start_proxy.py` 启动。
- 服务端提供 WebSocket 协议 v2、`/health` 状态信息、任务错误消息，以及 Paraformer、SenseVoice、Fun-ASR-Nano、Qwen3-ASR GGUF 和 Apple Silicon MLX 引擎。
- Python SDK 提供文件转录接口；部署、SDK 和协议细节分别由 `deploy/README.md`、`sdk/README.md` 和 [`docs/reference/protocol.md`](docs/reference/protocol.md) 维护。
- Proxy 可将多个 ASR 后端汇入一个入口，并把同一任务的音频帧固定转发给同一后端；当前路由按活动任务数和配置权重选择后端，协议、模型或编码要求会限制候选范围。
- Proxy 在监听端口提供 `/status` JSON 和 HTML 状态页，展示后端健康、活动任务、权重、延迟诊断和近期任务记录。处理延迟保留为诊断信息，不参与当前路由评分。

此前继承的完整更新日志（包括上游历史和早期服务端条目）保留在 [`docs/archive/legacy-client/CHANGELOG.md`](docs/archive/legacy-client/CHANGELOG.md)。
