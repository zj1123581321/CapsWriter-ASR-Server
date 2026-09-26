# 首次部署与第一次识别

本指南带你从空环境启动 CapsWriter ASR Server，并检查服务是否就绪。示例优先使用 Paraformer：它不需要 llama.cpp 动态库，也不要求 GPU；因为该引擎没有内建标点能力，还须额外准备 Punct-CT-Transformer 标点模型。

## 1. 获取代码

从本仓库克隆代码：

```sh
git clone https://github.com/zj1123581321/CapsWriter-ASR-Server.git
cd CapsWriter-ASR-Server
```

服务端开发和 CI 当前使用 Python 3.12，首次部署建议使用 Python 3.12。SDK 的 Python 版本要求以 [`sdk/pyproject.toml`](../sdk/pyproject.toml) 为准；这不是服务端最低版本声明。

## 2. 建立环境并安装依赖

根据操作系统选择一个依赖文件。下面的 Python 命令都会使用本项目自己的虚拟环境。

### Windows x64

在 PowerShell 中运行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-server.txt colorama
```

`requirements-server.txt` 已声明服务端启动所需的 `rich` 与 `websockets`；`colorama` 是 Windows 安装时还需补装的公共依赖。Paraformer 首次部署不使用 `pypinyin`、`watchdog` 或 llama.cpp 动态库；其它模型可能有额外依赖。

### macOS Apple Silicon

在 arm64 Mac 的终端中运行：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-server-macos.txt
```

仓库没有为 Intel Mac 提供专用 requirements 文件或安装验证。

### Linux

仓库提供 Linux requirements 文件，可先安装其中声明的 Python 依赖：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-server-linux.txt
```

这个文件包含 Linux 环境依赖，但本指南没有在 Linux 真机验证从创建环境到启动服务的完整流程，也没有覆盖所有发行版的系统动态库安装。首次采用前请在目标 Linux 发行版核对 `sherpa-onnx` 与 ONNX Runtime 的安装结果；不要将文件注释中的机器专用 CUDA/Vulkan 配置当作通用 GPU 安装步骤。Paraformer 配置使用 CPU 推理。

## 3. 下载并放置模型

从[上游 CapsWriter 模型发布页](https://github.com/HaujetZhao/CapsWriter-Offline/releases/tag/models)下载 Paraformer 和 Punct-CT-Transformer 模型。模型发布页由上游维护；不要将其下载链接替换成本仓代码链接。

解压后保留模型包中的内部目录，文件应位于：

```text
models/
├── Paraformer/
│   └── speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-onnx/
│       ├── model.onnx
│       └── tokens.txt
└── Punct-CT-Transformer/
    └── sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12/
        ├── model.onnx
        └── tokens.json
```

服务端在启动时检查 Paraformer 所需的 ONNX 模型和 `tokens.txt`。标点模型在加载识别引擎时使用；缺少时服务不能完成初始化。更多模型路径和下载说明见[模型支持](models.md)与[模型下载说明](模型下载的若干问题.md)。

## 4. 启动并检查服务

服务默认监听 `0.0.0.0:6016`。在同一终端中选择 Paraformer 并启动：

Windows PowerShell：

```powershell
$env:CW_MODEL_TYPE = 'paraformer'
.\.venv\Scripts\python.exe start_server.py
```

macOS / Linux：

```sh
CW_MODEL_TYPE=paraformer .venv/bin/python start_server.py
```

启动日志显示模型加载完成后，在另一终端访问健康状态：

Windows PowerShell：

```powershell
Invoke-RestMethod http://127.0.0.1:6016/health | Select-Object status, model, worker_alive
```

macOS / Linux：

```sh
curl --fail http://127.0.0.1:6016/health
```

健康响应中 `status` 应为 `ok`，`model` 应为 `paraformer`，`worker_alive` 应为 `true`。服务只供可信网络中的应用访问，不要将未加认证的监听端口直接暴露到公网。

## 5. 让应用完成首次识别

保持服务进程运行，再按照 [Python SDK 文档](../sdk/README.md)安装 SDK，并使用其中的 `s16le` 示例转录一段本地音频。SDK 在客户端读取音频时仍需文档列出的 `ffmpeg` / `ffprobe`；`s16le` 上传路径不要求服务端安装 `ffmpeg`。若改用默认的 FLAC 编码，服务端也需安装 `ffmpeg`，并先确认 `/health` 的 `encodings` 包含 `flac`。使用其它语言时，从 [WebSocket 协议](protocol.md)和[下游客户端接入指南](下游客户端接入指南.md)开始。

## 后续部署

- 选择其它引擎与下载对应模型： [模型支持](models.md)。
- 将多个后端接到同一入口： [ASR 负载均衡代理](ASR负载均衡代理.md)。
- 配置已运行服务的守护与升级： [部署维护与升级](../deploy/README.md)。
- 了解上游来源与项目差异： [UPSTREAM.md](../UPSTREAM.md)。
