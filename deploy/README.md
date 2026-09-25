# 三机统一部署与升级

`update.sh` 和 `update.ps1` 都在当前 clone 内更新到指定 Git ref、安装服务端依赖、重启一个指定进程，并核对 `/health` 的 `git_sha`。任一步失败即停止；不会接着重启其它服务。脚本不含主机地址或凭据。

## 首次部署

1. 在 Mac Studio、Mac mini 或 AMD Windows 上 clone 本仓库：`git clone <仓库地址> CapsWriter-Offline-with-AI`。
2. 首次安装依赖：macOS/Linux 在仓库根目录运行 `python3 -m venv .venv`，再用 `.venv/bin/python -m pip install -r requirements-server-macos.txt`（macOS）或 `requirements-server-linux.txt`（Linux）；Windows 创建 `.venv` 后安装 `requirements-server.txt`。
3. 按机器创建进程守护配置。Mac Studio 可复制 `pm2.ecosystem.config.js.example` 为本地 PM2 配置，再用 `pm2 start deploy/pm2.ecosystem.config.js` 注册三个进程；Windows 先创建计划任务。更新脚本只重启已有进程或任务。
4. 首次启动服务后，运行更新脚本切换到目标 ref 并核对 `/health`。
5. 按 `hosts.example.toml` 选择服务参数。该文件仅是无地址、无凭据的参数模板，不会被 shell 或 PowerShell 自动读取。

macOS/Linux 在 clone 根目录执行，每次只更新一个 PM2 进程：

```sh
CW_MODEL_TYPE=paraformer DEPLOY_PROCESS_NAME=capswriter-paraformer DEPLOY_PORT=6016 deploy/update.sh <git-ref>
```

proxy 进程使用 `CW_MODEL_TYPE=proxy`；Qwen MLX 进程使用 `CW_MODEL_TYPE=qwen_asr_mlx`。脚本按当前系统选择 macOS 或 Linux requirements；MLX 只允许在 macOS。

Windows 使用已有计划任务：

```powershell
.\deploy\update.ps1 -Ref <git-ref> -TaskName CapsWriter-Server -Port 6016 -ModelType qwen_asr
```

在 Windows PowerShell 7 中运行脚本。计划任务应使用 clone 中 `.venv\Scripts\python.exe` 启动 `start_server.py`，并设置与 `ModelType`、`Port` 一致的 `CW_MODEL_TYPE`、`CW_PORT`。

## 升级

对每个服务进程分别调用脚本。macOS/Linux 要设置 `CW_MODEL_TYPE`、`DEPLOY_PROCESS_NAME`、`DEPLOY_PORT`；Windows 传入 `-TaskName`、`-Port`、`-ModelType`。升级脚本先 `git fetch --tags origin`，再 detached checkout 目标 ref、安装依赖、重启并轮询本机 `/health`，最多 300 秒。HTTP 未到 200、超时或 `git_sha` 与 checkout 提交不一致都会以非零退出；失败诊断只列 `/health` 的 `status`、`git_sha`、`model`、`worker_alive`。

## 回滚

用同一脚本指定上一个可用 ref，例如 `deploy/update.sh <上一个-ref>`，Windows 则使用 `-Ref <上一个-ref>`。保留 `core/server/engines/llama/bin/` 下已有的 llama.cpp 版本目录；回滚代码后仍可加载旧版本，细节见 [`UPSTREAM.md`](../UPSTREAM.md)。
