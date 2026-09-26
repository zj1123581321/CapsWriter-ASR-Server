# 维护者运维记录

本页记录当前维护者的机器布局和历史运维经验，不是普通用户的部署要求。首次安装请看[首次部署与第一次识别](getting-started.md)，通用升级步骤请看[部署维护与升级](../deploy/README.md)。

## 生产服务布局（2026-09）

- **Mac Studio**：三个进程分别使用独立 clone。PM2 的 `qwen-asr-server`（6017，`qwen_asr_mlx`）和 `capswriter-server`（6016，`paraformer`）由各自 clone 的 `venv/bin/python` 启动。`capswriter-proxy`（6020）由 clone 内未跟踪的 `run_proxy.sh` 启动，使用 `.venv/bin/python`。
- **Windows**：计划任务 `CapsWriter-Server` 调用 clone 内未跟踪的 `run_server.bat`，服务使用 PATH 上的系统 Python，没有虚拟环境。该批处理启动时会结束所有 Python 进程，可能中断机器上的其它 Python 任务。
- **Mac mini**：6017 自 2026-09-08 起人为停用，PM2 保存的是空进程表；proxy 会将该后端标记为 unhealthy。它不应被当作待自动恢复的服务。

以上进程名、端口和本机脚本只描述维护者当前环境。独立部署时由部署者选择自己的进程管理方式。

## 历史回滚记录：T17 之前版本

旧代码的端口预检可能把刚关闭连接留下的 `TIME_WAIT` 误报为端口冲突并挂起。回滚到 T17 之前的 ref（例如 `ed8ab58`）时，先停目标进程，等待约 40 秒，再切换代码并启动：

1. Mac 上运行 `pm2 stop <进程名>`；Windows 上运行 `Stop-ScheduledTask -TaskName CapsWriter-Server`。
2. 等待约 40 秒。
3. 使用更新脚本切换并启动。手工操作时，先 `git checkout --detach <git-ref>`，再运行原有的 `pm2 start` 或 `Start-ScheduledTask`。

这段等待只针对 T17 之前的 ref。旧 ref 没有 `llama_build_info.py` 时，llama 预检会打印跳过日志后继续。
