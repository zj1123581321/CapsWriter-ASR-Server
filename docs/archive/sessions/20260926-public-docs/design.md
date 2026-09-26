# 公开文档与命名整理设计

## 目标

将项目说明统一为 **CapsWriter ASR Server**：源自 `HaujetZhao/CapsWriter-Offline`，作为可供各种应用接入的独立离线语音识别服务。目标读者是第一次接触项目的开发者；看完入口文档后，应能按平台准备服务端、启动最短路径模型、检查服务健康状态，并找到 SDK 和 WebSocket 接入文档。

文档中的最终 GitHub slug 使用 `CapsWriter-ASR-Server`，仓库 URL 暂按当前所有者拼为 `https://github.com/zj1123581321/CapsWriter-ASR-Server`。改名之前仓库仍是 fork；公开文案保留上游来源，不宣称已经与上游脱离。GitHub description、topics 和仓库改名由主脑在文档合并后统一执行。

## 卡片边界

### A 卡（本文件对应的任务）

- 编辑 `readme.md`、`deploy/README.md`、`docs/models.md`、`docs/模型下载的若干问题.md`、`UPSTREAM.md`。
- 新增 `docs/getting-started.md` 作为跨平台首次部署入口；新增 `docs/operations.md` 收纳个人生产机器布局和历史运维记录。
- 本设计文件记录目标、文档分工和待验证限制。
- README 保留名称、接入、上游关系与许可证等现有结构；更新定位和部署入口，补充平台 requirements 表与模型路径。
- 推荐默认模型为 Paraformer；依赖文件与代码路径须能支撑命令。Python SDK 是推荐接入入口，首页链接 SDK 文档；最小 WebSocket 示例由 B 卡实现。
- A 卡不得编辑 SDK、协议、下游接入文档、示例代码、GitHub 元信息或仓库 remote。

### B 卡（范围独立，并行实现）

- 负责 `sdk/README.md`、`docs/protocol.md`、`docs/下游客户端接入指南.md`、最小 WebSocket 示例与示例验证。
- B 卡按服务端已实现协议核实示例，不依赖 A 卡臆测 SDK 方法签名；A 卡只链接现有 SDK 文档，后续由 B 卡补实内容。
- B 卡可与 A 卡并行实施；A 卡先合并，B 卡随后更新到新 master 再验收合并。主脑复核交叉链接与首次接入路线。

### 主脑收尾

- GitHub description 拟为「可自部署的离线语音识别服务，支持多种模型、WebSocket 接入、负载均衡代理和 Python SDK。源自 CapsWriter。」
- 主脑更新 topics、GitHub description 并改仓库 slug；核对旧链接重定向与本地最终 slug 链接。A/B 卡均不操作 GitHub 元信息。

## 文档路线

1. 首页说明项目用途、当前 fork/上游关系、可用能力、平台与模型入口。
2. 快速开始说明服务端依赖、模型文件准备、监听端口、`/health` 健康检查和 SDK 的 `s16le` 首次识别路径；默认 FLAC 编码另说明服务端 FFmpeg 与健康端点编码能力要求。
3. 安装文档按 Windows、macOS Apple Silicon、Linux 标明 requirements 文件和可确认的限制；不能真实核实的平台不声称完整支持，不编造系统包命令。
4. `docs/models.md` 列出现有引擎、辅助模型与能力；模型下载说明保留上游 Release 的真实下载地址，去除过时的模型能力结论、性能承诺及面向个人硬件的选择建议。
5. 通用部署文档只描述用户自己的 clone、依赖、运行和升级流程；个人生产机器目录、守护配置、停用后端和历史回滚经验移入 `docs/operations.md`，不作为普通用户部署前提。
6. `UPSTREAM.md` 说明项目来源、摘取范围和同步记录；它不写成已脱离 fork 的独立上游项目。

## 代码核对所得

- 服务端入口是 `python start_server.py`；`config_server.py` 默认 `CW_MODEL_TYPE=qwen_asr`、监听 `0.0.0.0:6016`，入门路径须显式选择 `paraformer`。
- `GET /health` 由同一个监听端口处理，状态实现位于 `core/server/connection/health.py`；本地健康检查使用 `http://127.0.0.1:6016/health`。
- Paraformer 模型路径来自 `config_server.py`，要求 release 模型放在 `models/Paraformer/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-onnx/`；引擎使用 sherpa-onnx，强制 CPU provider。
- requirements 分为 `requirements-server.txt`、`requirements-server-macos.txt`、`requirements-server-linux.txt`。Windows 文件含启动所需 `rich`、`websockets`、`pypinyin`，但漏列 `core/__init__.py` 无条件导入的 `colorama`，快速开始命令需显式补装。Linux 文件内有机型专用 Vulkan/CUDA 注释，不能据此泛化为通用 Linux GPU 安装步骤；macOS 文件明确只适用 Apple Silicon。
- llama.cpp 版本固定为 `b10621`（`core/server/engines/llama_build_info.py`）；Paraformer 不需要 llama 动态库。旧文档中的 b7798 / 根目录回退 / Linux llama CUDA 说明需与当前 loader 和 release 说明核对后修订。
- 仓库开发可建议 Python 3.12；SDK 的 `pyproject.toml` 声明 `requires-python >=3.10`，不能把服务端开发建议写成 SDK 最低版本。

## 验收

- README 第一屏名称与定位一致，给出清楚的推荐平台安装入口、健康检查、SDK 首次识别入口和后续阅读链接。
- 快速开始中每条命令、依赖文件、模型目录和链接都能映射到仓库现有实现或明确标注的用户选择。
- 通用用户部署说明不要求使用个人生产机器的 clone、PM2、计划任务或私人路径。
- 新增和修改的相对 Markdown 链接均指向存在文件；代码链接按将改名后的最终仓库 slug 编写。上游模型下载地址不替换成本仓链接。
- A 卡仅检查文档链接和命令路径，不新增镜像单测。SDK 安装/运行与 WebSocket 示例由 B 卡验证。
- A 卡新增内容总量不超过 600 行；纯文档以精简旧内容控制范围。交付需说明没有真实复验的操作系统或模型。
