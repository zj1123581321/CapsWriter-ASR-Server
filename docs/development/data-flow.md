## 服务端数据流

```text
Python SDK 或其他下游
        |
        | WebSocket 音频帧（proxy 可选）
        v
ASR Proxy（按任务选择后端）
        |
        v
ASR Server 主进程
  接收与校验 -> 解码 -> 分段 -> 有界任务队列
                                  |
                                  v
                         Worker 子进程
                         ASR / 标点 / 对齐
                                  |
                                  v
ASR Server 主进程
  合并文本与时间戳 -> 结果帧 / 错误帧 -> WebSocket 下游
```

SDK 将音频文件转换为选定编码并分块上传；自定义下游可直接实现 [协议](../reference/protocol.md)。服务端错误、连接终态、编码能力和版本兼容规则以协议文档为准。

## HTTP 文件任务边界

HTTP 文件任务是显式启用（`CW_HTTP_PORT` + `CW_HTTP_DATA_DIR`）的并行路径，与 WS 共用识别链：

```text
HTTP 客户端 -> 受限落盘/续传确认 -> commit 受理出唯一 Job
                                      |
                                      v
                    全局运行闸门（同时只放行 1 个 Job，其余保持 QUEUED）
                                      |
                                      v
                    QUEUED -> RUNNING 条件落库（SQLite 唯一真源）
                                      |
                                      v
                    runner：固定 argv 的 ffmpeg 流式解码原容器
                          （16k/mono/f32le，管道，无临时 PCM 文件）
                                      |
                                      v
                           共享有界 PCM 分段器
                                      |
                  owner_kind=http, socket_id="" 的 Task（task_id=job_id）
                                      |
                                      v
                              既有 Worker Queue
                                      |
                  Result -> 注入的 HTTP 持久 sink
                            （逐条核对上限；最终段整份持久化并与 DONE 同事务）
                          终态落库之后才释放闸门、活跃 owner 与源引用
```

受理后任务脱离原连接继续运行：另一连接凭同一凭据 `GET /v1/jobs/{id}` 与
`GET /v1/jobs/{id}/result` 领取状态与完整结果；识别进行中状态是持久化的 `RUNNING`，
失败任务在 result 接口上带出已存 `error_code`。runner 不可用时 commit 明确 503，不受理后永远排队；
任一段解码、提交或结果失败即整 Job FAILED（先可靠提交，再停止后续段），迟到结果不覆盖终态。
HTTP 同时只运行 1 个 Job，未拿到闸门的保持 QUEUED，不解码也不占在途段。
推理段超时发生在父进程监控协程时，会先可靠写入 `FAILED[inference_timeout]` 再让进程非零退出；
落库本身失败时仍然非零退出，但会显式记录“持久失败事实未落库”。
worker 崩溃、收尾排错等 runner 之外的 HTTP 终态一律经 `finalize_http_job`
唯一入口：先以条件更新（不覆盖已有终态）持久化 FAILED，成功之后才
`transition_terminal` 释放 owner/唤醒等待者；持久写有 `wait_for` 超时上限，
超时或失败时不释放 owner。结果路径上 HTTP 终态的唯一收尾人是结果 sink
（成功：record_result 落库 DONE → 转换 → 释放；失败：runner.fail_job 同形态），
`ws_send` 的 HTTP 分支只做段确认、不再做任何终态转换，每条路径恰好转换一次。
释放路径的位置表与顺序由
`tests/test_http_release_invariant.py` 的 AST 机械检查钉住。
服务重启后 QUEUED/RUNNING 收敛为 FAILED[server_restarted]，不做自动重跑。

WS 仍沿用连接生命周期；它与 HTTP 只共享 PCM 段、offset、overlap、段长上限和有限提交，不共享连接取消或持久确认。两条路径通过 `(owner_kind, derived_owner_id, task_id)` 隔离：WS owner 从 `socket_id` 派生，HTTP owner 从稳定 `task_id` 派生。HTTP 任务必须存在于跨进程 `active_http_jobs` 集合，否则 worker 不继续推理；该登记发生在首段入队之前，释放发生在结果持久化或 FAILED 可靠提交之后。
