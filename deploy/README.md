# 部署维护与升级

从空环境完成首次安装，请先阅读[首次部署与第一次识别](../docs/guides/getting-started.md)。本页说明已有服务的更新脚本、守护进程边界和可选 llama.cpp 动态库准备。各主机个人布局与历史回滚记录见[维护者运维记录](../docs/maintainers/operations.md)，普通部署不需要这些配置。

## 更新脚本做什么

`update.sh` 和 `update.ps1` 在当前 clone 中切换到指定 Git ref，用服务实际使用的 Python 解释器安装依赖，重启一个已有服务，并检查 `/health`。脚本不会创建虚拟环境，也不会创建 PM2、计划任务或其它守护配置。

服务首次启动且确认健康后，再将更新脚本接入自己的守护进程。`hosts.example.toml` 只是无地址、无凭据的参数模板，不会被 shell 或 PowerShell 自动读取。

## 更新已有服务

macOS/Linux 在每个服务 clone 根目录运行。`DEPLOY_PYTHON` 必填，必须指向运行该服务且带 pip 的解释器；可以是可执行路径或 PATH 上的命令名。

```sh
DEPLOY_PYTHON="$PWD/.venv/bin/python" \
CW_MODEL_TYPE=paraformer DEPLOY_PROCESS_NAME=my-asr DEPLOY_PORT=6016 \
deploy/update.sh <git-ref>
```

需要为不同引擎或实例更新时，按实际服务分别设置 `CW_MODEL_TYPE`、`DEPLOY_PROCESS_NAME` 和 `DEPLOY_PORT`。`DEPLOY_PORT` 必须与该实例监听端口一致。

Windows 使用已创建的计划任务：

```powershell
.\deploy\update.ps1 -Ref <git-ref> -Python .\.venv\Scripts\python.exe -TaskName My-ASR -Port 6016 -ModelType paraformer
```

`-Python` 必填，应指向计划任务实际使用且带 pip 的解释器。计划任务的启动脚本和环境变量由部署者自行维护。

两个脚本会抓取 tags 并 detached checkout 目标 ref；llama 预检通过后，以指定解释器安装目标 requirements，再重启并轮询本机 `/health`，最多等待 300 秒。HTTP 状态非 200、超时或运行进程 `git_sha` 与目标提交不同会以非零退出。失败诊断只显示 `/health` 的 `status`、`git_sha`、`model` 和 `worker_alive`。

`/health` 的 `git_sha` 表示当前运行进程启动时加载的代码版本；磁盘上的 Git HEAD 改变后，只有服务重启完成才会更新。

## 可选模型所需的 llama.cpp 动态库

Paraformer、SenseVoice、proxy 不需要 llama.cpp 库。`qwen_asr`、`fun_asr_nano` 和 `qwen_asr_mlx` 的强制对齐路径会加载 llama.cpp。当前版本由 [`llama_build_info.py`](../core/server/engines/llama_build_info.py) 固定为 `b10621`，动态库必须直接放在 `core/server/engines/llama/bin/b10621/`；程序不会退回 `bin/` 根目录。

从 [llama.cpp b10621 官方发行页](https://github.com/ggml-org/llama.cpp/releases/tag/b10621)获取匹配操作系统和 CPU 架构的资产。仓库中记录的资产包括：

- macOS Apple Silicon：`llama-b10621-bin-macos-arm64.tar.gz`
- Windows x64 Vulkan：`llama-b10621-bin-win-vulkan-x64.zip`
- Windows x64 CPU：`llama-b10621-bin-win-cpu-x64.zip`
- Linux Ubuntu x64 CPU：`llama-b10621-bin-ubuntu-x64.tar.gz`

解压后，当前平台的 `ggml`、`ggml-base` 和 `llama` 三个核心动态库应直接位于 `b10621/` 子目录。Linux GPU 部署需要针对具体发行版、驱动和后端单独验证；本页不提供通用 CUDA/Vulkan 配方。不同平台和后端的资产不能混放进同一版本目录。

保留旧版本库目录，以便回滚到要求旧版本的代码。首次从旧版代码升级时，先按目标提交的 `LLAMA_BUILD` 准备新目录，再重启服务。

## 可选：启用 HTTP 文件任务入口

默认不启用。要启用必须同时提供 `CW_HTTP_PORT` 与稳定绝对路径 `CW_HTTP_DATA_DIR`：

```sh
export CW_HTTP_PORT=6116
export CW_HTTP_DATA_DIR=/var/lib/capswriter/http
```

约束：`CW_HTTP_PORT` 不得与 `CW_PORT` 相同，不自动推算邻近端口；`CW_HTTP_DATA_DIR` 必须是绝对路径，且同一时刻只允许一个 server 实例持有（OS 独占锁，进程退出后自动释放，不做网络盘与多实例共写承诺）。任一条件不满足或数据目录不可用时，服务端直接启动失败并以非零退出，不会退回关闭状态。服务端依赖里已声明 `aiohttp==3.14.3`（Python ≥ 3.10），仅在启用时导入。

POSIX 下服务端会将数据目录与 `sources/` 收紧为 `0700`，并将数据库、WAL/SHM、锁文件和 source 文件设为 `0600`，不依赖进程 umask。Windows 上代码未设置或验证 NTFS ACL；启用前需确认 `CW_HTTP_DATA_DIR` 的 ACL 仅允许服务账户与管理员访问，本仓未在真实 Windows 环境验证该 ACL。

当前该入口只交付上传与查询底座：文件识别协调者装配前，`commit` 明确返回 `503 inference_unavailable`，已上传字节与恢复凭据都保留，可在该入口可用后显式继续。
