<!-- delegate-outcome: failed -->
# issue40 生产部署裁决

冻结提交：`6b7a2b82fbc3ebe862250a8804902e5bf37f9211`

## 实作

| 角色 | 结果 | before SHA | after 磁盘 HEAD | after 运行 SHA | HTTP/守护 | worker 或 proxy 状态 |
|---|---|---|---|---|---|---|
| Studio-Paraformer | `deployed` | `2c5d244` | `6b7a2b8` | `6b7a2b8` | HTTP 200；重启后稳定 | worker=true；active/queued=`0/0` |
| Studio-MLX | `deployed` | `2c5d244` | `6b7a2b8` | `6b7a2b8` | HTTP 200；重启后稳定 | worker=true；aligner=loaded；llama_build=`b10621`；active/queued=`0/0` |
| Studio-Proxy | `deployed` | `2c5d244` | `6b7a2b8` | `6b7a2b8` | HTTP 200；重启后稳定 | proxy healthy；升级后端健康且运行 `6b7a2b8`；停用后端保持 unhealthy |
| Windows-Qwen | `blocked` | `2c5d244` | `2c5d244` | `2c5d244` | 未执行更新；原服务 HTTP 200 | worker=true；active/queued=`0/0`；启动脚本会结束全部 Python，另有 2 个非目标 Python |

Mac mini 的人为停用状态保持不变，未复活、未覆盖、未作为部署目标。

## 验证

| 角色 | file 短尾 `5.05s` | file 普通 final | mic 短句 | 稳定窗口 |
|---|---|---|---|---|
| Studio-Paraformer | `pass`；final=true；text_accu 长度=12；tokens/timestamps=`12/12`；join=pass | `pass`；duration=5.050；`12/12`；join=pass | `pass`；duration=1.000；`7/7` | 两次健康/重启计数一致 |
| Studio-MLX | `pass`；final=true；text_accu 长度=12；tokens/timestamps=`12/12`；join=pass | `pass`；duration=5.050；`12/12`；join=pass | `pass`；duration=1.000；`6/6` | 两次健康/重启计数一致 |
| Studio-Proxy | `pass`；final=true；text_accu 长度=12；tokens/timestamps=`12/12`；join=pass；status history delta=1；实际路由到健康 `6b7a2b8` 后端 | `not-required` | `not-required` | 两次健康/重启计数一致 |
| Windows-Qwen | `not-run` | `not-run` | `not-run` | 未部署 |

受控 fixture：非敏感合成语音，单声道、16 kHz、PCM16、`5.05s/80800 samples`，末尾 `800 samples` 静音。外部 WebSocket v2 探针实际断言了 `task_id`、`type=result`、`is_final=true`、非空识别、数组等长及 `join(tokens)==text_accu`；错误帧和 final 前断连均按失败处理。

## 风险和偏差

- 本卡为部分完成，不能称为全生产完成；Windows-Qwen 是明确 `blocked`，需要先在维护窗口确认无关 Python 风险，再单独复核并部署。
- 既有 Windows-Qwen 后端在 Studio-Proxy 健康列表中仍为 `2c5d244`，这是阻塞结果，不是新版本部署成功。
- 三个 Studio 角色未触发回滚条件；没有修改源码、依赖声明、workflow、模型、守护配置或停用目标。
- 两次临时日志重定向拼写错误均发生在更新脚本启动前，回读确认无远端副作用后修正；正式更新调用成功。
- 主干基线不可用，继承红/新红无法判定。

## 交付

- 私有完整报告已写入派发报告路径，含进程启动/重启证据与结构化探针记录。
- 本文件只保留角色、代码 SHA、健康状态和测试统计；未写入账号、地址、私有路径、生产响应体或凭据。
