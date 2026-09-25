# CapsWriter ASR Server 开发指南
risk-tier: internal

## 项目定位

本仓库提供可在局域网部署的离线语音识别服务端、可选负载均衡 proxy，以及下游 Python SDK。服务端在独立子进程中运行识别模型，WebSocket 网络循环留在主进程。

## 关键入口

- `start_server.py`：启动 ASR 服务端。
- `start_proxy.py`：启动 ASR proxy。
- `config_server.py` / `config_proxy.py`：服务端与 proxy 配置。
- `core/server/` / `core/proxy/`：服务端与 proxy 实现。
- `sdk/`：Python 下游接入 SDK。
- `hot-server.txt`：受模型支持情况影响的服务端热词配置。

## 架构与开发文档

- [架构与工作流](docs/architecture-workflows.md)
- [数据流](docs/data-flow.md)
- [关键路径](docs/key-paths.md)
- [部署与升级](deploy/README.md)
- [测试](docs/testing.md)
- [模型支持](docs/models.md)
- [协议](docs/protocol.md)
- [SDK 使用说明](sdk/README.md)
- [上游关系](UPSTREAM.md)

## 用户偏好 (User Preferences)

- **语言**：中文，代码注释与项目文档使用中文。
- **环境**：服务端部署依赖、平台配置和升级方式以 [deploy/README.md](deploy/README.md) 为准；开发时可使用 Python 3.12。
- 临时 Python 代码先写入临时脚本文件再运行，用完不要删除。
