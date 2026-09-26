# 三机统一部署与升级

`update.sh` 和 `update.ps1` 在当前 clone 内切换到指定 Git ref、用服务实际使用的 Python 解释器安装依赖、重启一个服务并核对 `/health` 的 `git_sha`。脚本不创建虚拟环境；首次建环境按下文手工完成。更新脚本不含主机地址或凭据。

## 首次部署

1. 在目标机器 clone 本仓库：`git clone <仓库地址> CapsWriter-Offline-with-AI`。
2. 手工准备守护进程实际使用的解释器并安装依赖。需要虚拟环境时，在 clone 内运行 `python3 -m venv <环境目录>`，再用 `<环境目录>/bin/python -m pip install -r requirements-server-macos.txt`（macOS）或对应平台的 requirements；Windows 可直接使用 PATH 上的 `python`。之后更新脚本只会通过传入的解释器执行 `-m pip install -r ...`。
3. 按机器配置守护进程。更新脚本只重启已存在的 PM2 进程或计划任务，不创建守护配置。
4. 准备本机所需的 llama 动态库，步骤见「llama 库」。首次启动并确认 `/health` 可访问后，再运行更新脚本。
5. `hosts.example.toml` 是无地址、无凭据的参数模板，不会被 shell 或 PowerShell 自动读取。

## 生产机实际布局

- **Mac Studio**：三个进程各自使用独立 clone。PM2 的 `qwen-asr-server`（6017，`qwen_asr_mlx`）和 `capswriter-server`（6016，`paraformer`）都由各自 clone 的 `venv/bin/python` 启动。PM2 的 `capswriter-proxy`（6020）由 clone 内未跟踪的 `run_proxy.sh` 启动，使用 `.venv/bin/python`。
- **Windows**：计划任务 `CapsWriter-Server` 调用 clone 内未跟踪的 `run_server.bat`，服务使用 PATH 上的系统 `python`，没有虚拟环境。`run_server.bat` 启动时会 `taskkill` 全部 python 进程，因此运行它可能结束机器上其他 Python 任务。
- **Mac mini**：6017 自 2026-09-08 起人为停用，PM2 保存的是空进程表；proxy 会把这个后端显示为 unhealthy。不要把它当成应自动恢复的服务。

## 更新

macOS/Linux 在每个服务自己的 clone 根目录执行。`DEPLOY_PYTHON` 必填，值应指向该守护进程实际使用的解释器；可以是可执行文件路径，也可以是 PATH 上的命令名。

```sh
DEPLOY_PYTHON="$PWD/venv/bin/python" \
CW_MODEL_TYPE=qwen_asr_mlx DEPLOY_PROCESS_NAME=qwen-asr-server DEPLOY_PORT=6017 \
deploy/update.sh <git-ref>
```

Paraformer 使用 `CW_MODEL_TYPE=paraformer DEPLOY_PROCESS_NAME=capswriter-server DEPLOY_PORT=6016`，解释器同样填该 clone 实际使用的 `venv/bin/python`。Proxy 使用 `CW_MODEL_TYPE=proxy DEPLOY_PROCESS_NAME=capswriter-proxy DEPLOY_PORT=6020`，解释器填 `.venv/bin/python`。

Windows 使用已有计划任务：

```powershell
.\deploy\update.ps1 -Ref <git-ref> -Python python -TaskName CapsWriter-Server -Port 6016 -ModelType qwen_asr
```

`-Python` 必填，可填 PATH 上守护进程实际使用的 `python` 命令或解释器路径。计划任务的 `run_server.bat` 由 clone 中的未跟踪文件维护。

对每个服务分别调用脚本。脚本先 `git fetch --tags origin`，再 detached checkout 目标 ref；llama 预检通过后用指定解释器安装 requirements，随后重启并轮询本机 `/health`，最多 300 秒。HTTP 未到 200、超时或 `git_sha` 与 checkout 提交不一致都会以非零退出；失败诊断只列 `/health` 的 `status`、`git_sha`、`model`、`worker_alive`。

## llama 库

代码从 `core/server/engines/llama/bin/<LLAMA_BUILD>/` 加载动态库。需要 llama 的模型是 `qwen_asr`、`qwen_asr_mlx`、`fun_asr_nano`；`paraformer`、`sensevoice` 和 `proxy` 不需要。当前 build 值见 `core/server/engines/llama_build_info.py`。下载对应的 llama.cpp release 资产后，把其中的动态库解压到 build 子目录，三个必需文件应直接位于该目录：

- macOS：`llama-<build>-bin-macos-arm64.tar.gz`，解压到 `core/server/engines/llama/bin/<build>/`。使用 `tar -xzf` 解压，保留资产内的符号链接。
- Windows：`llama-<build>-bin-win-vulkan-x64.zip`，解压到 `core/server/engines/llama/bin/<build>/`。

macOS 目录需要 `libggml.dylib`、`libggml-base.dylib`、`libllama.dylib`；Windows 目录需要 `ggml.dll`、`ggml-base.dll`、`llama.dll`。缺失时更新脚本会在重启前失败，并输出缺失文件与对应资产名。`bin/` 根目录中的旧版本库要保留，旧代码回滚时仍从那里加载。

## 回滚到 T17 之前的版本

旧代码的端口预检可能把刚关闭连接留下的 `TIME_WAIT` 误报为端口冲突并挂起。回滚到 T17 之前的 ref（例如 `ed8ab58`）时，先停目标进程，等待约 40 秒，再切换代码并启动：

1. Mac 上运行 `pm2 stop <进程名>`；Windows 上运行 `Stop-ScheduledTask -TaskName CapsWriter-Server`。
2. 等待 40 秒。
3. 使用更新脚本切换并启动。手工操作时，先 `git checkout --detach <git-ref>`，再运行原有的 `pm2 start` 或 `Start-ScheduledTask`。

这段等待只针对 T17 之前的 ref。llama 预检在 checkout 后读取目标版本；旧 ref 没有 `llama_build_info.py` 时会打印跳过日志后继续。
