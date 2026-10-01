<!-- delegate-outcome: succeeded -->
# issue40 生产部署裁决

冻结提交：`6b7a2b82fbc3ebe862250a8804902e5bf37f9211`

## 实作

首次滚动（保留原记录）：

| 角色 | 结果 | before SHA | after 磁盘 HEAD | after 运行 SHA | HTTP/守护 | worker 或 proxy 状态 |
|---|---|---|---|---|---|---|
| Studio-Paraformer | `deployed` | `2c5d244` | `6b7a2b8` | `6b7a2b8` | HTTP 200；重启后稳定 | worker=true；active/queued=`0/0` |
| Studio-MLX | `deployed` | `2c5d244` | `6b7a2b8` | `6b7a2b8` | HTTP 200；重启后稳定 | worker=true；aligner=loaded；llama_build=`b10621`；active/queued=`0/0` |
| Studio-Proxy | `deployed` | `2c5d244` | `6b7a2b8` | `6b7a2b8` | HTTP 200；重启后稳定 | proxy healthy；升级后端健康且运行 `6b7a2b8`；停用后端保持 unhealthy |
| Windows-Qwen | `blocked`（首次，计数口径过窄） | `2c5d244` | `2c5d244` | `2c5d244` | 未执行更新；原服务 HTTP 200 | 当时按「监听 PID 以外 2 个 Python」判无关；未杀进程、未改 batch |

Windows PID 树复核后补部署：

| 角色 | 结果 | before SHA | after 磁盘 HEAD | after 运行 SHA | HTTP/守护 | 复核要点 |
|---|---|---|---|---|---|---|
| Windows-Qwen | `deployed` | `2c5d244` | `6b7a2b8` | `6b7a2b8` | HTTP 200；计划任务 Running；worker=true；aligner=loaded；llama_build=`b10621`；active/queued=`0/0` | Python 共 3，目标树内 3，无关 0；根为计划任务 cmd 包装层且匹配 `run_server.bat`/目标 clone；pwsh 7.6 可用；未改 batch |

Mac mini 仍人为停用，未复活。Studio 三角色本次未重启；复核时 pid/restart/start 与首次部署后快照一致。

## 验证

原 Mac 烟测保留：Studio-Paraformer/MLX 的 file 短尾、普通 final、mic 均为 pass；Studio-Proxy 短尾 pass 且当时路由到健康 `6b7a2b8` 后端。

Windows 补部署后：

| 角色 | file 短尾 `5.05s` | file 普通 final | mic 短句 | 稳定窗口 |
|---|---|---|---|---|
| Windows-Qwen | `pass`；final=true；text_accu 长度=12；tokens/timestamps=`12/12`；join=pass | `pass`；duration=5.050；`12/12`；join=pass | `pass`；duration=1.000；`6/6` | 两次健康/监听进程 CreationDate 一致 |
| Studio-Proxy（未重启） | `pass`；history delta=1；completed；路由到健康 `6b7a2b8` 的 6016 后端 | `not-required` | `not-required` | Studio 进程未重启；Windows 后端 git_sha 已变为 `6b7a2b8`；停用后端仍 unhealthy |

## 风险和偏差

- 首次阻塞原因是把「监听 PID 以外的 Python」直接当无关进程；复核证明那 2 个 Python 是同一 cmd 包装树的后代 worker。原阻塞记录保留，不改写成从未阻塞。
- `run_server.bat` 仍会结束全部 Python；本次因无关数为 0 才沿现有机制更新，未改脚本。
- Windows CommandLine 对 python.exe 未同时命中 `start_server.py`+clone（布尔 false），归属以 PID/PPID 后代集合与 cmd 包装层布尔为准，未回显 argv。
- Studio/Mini 未作为本次写入目标；无回滚。
- 主干基线仍不可用，继承红/新红未能判定。

## 交付

- 授权活跃目标现均已验收：Studio 三角色 + Windows-Qwen = `deployed`。
- 本文件只保留角色、代码 SHA、健康/测试统计与目标/无关数量；PID 与私人路径不公开。
