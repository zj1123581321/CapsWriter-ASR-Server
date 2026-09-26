# CapsWriter ASR Server

源自 [CapsWriter-Offline](https://github.com/HaujetZhao/CapsWriter-Offline) 的独立离线语音识别服务，供各种应用通过 WebSocket 接入。仓库包含多种 ASR 引擎、推荐的 Python SDK，以及可选的多后端负载均衡 proxy。

## 快速开始

第一次部署请从[首次部署与第一次识别](docs/guides/getting-started.md)开始。指南涵盖平台对应的依赖文件、推荐的 Paraformer 模型、健康检查和 SDK 接入入口。

| 部署环境 | 依赖文件 | 说明 |
| --- | --- | --- |
| Windows x64 | `requirements-server.txt` | 还需安装 `colorama`；本仓库未提供 Windows ARM64 配方 |
| macOS Apple Silicon | `requirements-server-macos.txt` | MLX 引擎只支持 Apple Silicon；首次部署也可选择 Paraformer |
| Linux | `requirements-server-linux.txt` | 依赖文件存在；不同发行版和 GPU 安装仍需按指南说明核验 |

## 接入应用

- [Python SDK 安装与首次识别](sdk/README.md) — 推荐的文件转录入口。
- [WebSocket 协议](docs/reference/protocol.md) — 其它语言或自定义客户端可直接使用协议。
- [下游客户端接入指南](docs/guides/下游客户端接入指南.md) — 接入路线与最小示例。

## 能力与模型

- Paraformer、SenseVoice、Fun-ASR-Nano、Qwen3-ASR GGUF，以及仅 Apple Silicon 可用的 MLX 后端。
- 协议通过 WebSocket 提交音频；`GET /health` 返回服务和推理 Worker 状态。
- 可选 proxy 将多个 ASR 后端汇聚到单一入口。

模型能力、文件目录和首次选择建议见[模型支持](docs/reference/models.md)与[模型下载说明](docs/guides/模型下载的若干问题.md)。按首次部署指南选择 Paraformer 时无需安装 llama.cpp 动态库；选择 Qwen、Fun-ASR 或时间戳对齐路径则需准备相应动态库。

## 进一步阅读

- [部署维护与升级](deploy/README.md)
- [Python SDK](sdk/README.md)
- [ASR 负载均衡代理](docs/guides/ASR负载均衡代理.md)
- [架构与工作流](docs/development/architecture-workflows.md)
- [协议](docs/reference/protocol.md) · [数据流](docs/development/data-flow.md) · [配置热词](docs/guides/热词功能如何使用.md)
- [文档总导航](docs/README.md)
- [项目来源与上游同步记录](UPSTREAM.md)

本仓库从上游 CapsWriter 演进为面向应用接入的服务端项目；来源、取舍和同步方式见 [UPSTREAM.md](UPSTREAM.md)。

## 许可证

本项目使用 [MIT 许可证](LICENSE)。
