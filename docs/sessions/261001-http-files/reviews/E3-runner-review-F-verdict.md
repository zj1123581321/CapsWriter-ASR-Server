<!-- delegate-outcome: succeeded -->
## Verdict
failure-visibility: p1-found

结论：FAIL。冻结范围 `2ab765050f8c5efcffd5e116d2f867c67b616a12..42f6da33d2c073f97ee880d1c4446a6e952c8c60` 的正常完成/失败路径大体成立，但 HTTP owner/运行闸门在持久失败事实可靠落库前会被释放，存在 P1。

## Previous fixes

- P1-2，违反 design R4/R6 及“终态可靠落库后才释放”不变式：`ProcessManager.monitor` 的 HTTP 段超时分支先 `transition_terminal`，该调用移除 `active_http_jobs`、清空 pending 并唤醒 runner，随后才等待 `runner.fail_job`。真实 port 0/temp 目录探针阻塞 FAILED 写入时得到 `first_db=RUNNING`、`second_db=RUNNING`、`ffmpeg_start_count=2`；第二 Job 已越过闸门。P1 两问：真实路径会触发吗？会，已实测；后果可接受吗？不可接受，闸门与持久状态顺序失效。建议所有 HTTP 超时/进程失败路径先完成 `runner.fail_job`，成功后再转本地终态、释放 owner/闸门。
- P1，同一不变式在 worker 进程崩溃路径也不成立：`fail_active_tasks` 对无 WebSocket 的 HTTP key 只做内存 `transition_terminal`，不调用持久 sink。真实 worker `os._exit(17)` 探针在主进程非零退出前读库仍为 `RUNNING/error_code=null`。P1 两问：真实路径会触发吗？会，已实测；后果可接受吗？不可接受，失败事实只能等重启才被改写为 `server_restarted`。应将 HTTP 进程失败纳入同一“先持久 FAILED、后释放”路径；写失败仍保持非零退出并保留明确可见错误。
- P2-2：通过。`mark_running` 是 `WHERE state=QUEUED` 的条件更新且 `started_at=COALESCE(...)`；36 项 E3 测试包含另一连接观察 RUNNING、终态不回退和重复 commit 不重投。
- P2-3：通过。`record_result` 的 `''.join(tokens)==text_accu` 拒绝不一致结果；变异探针输出当前 `invalid_result`，移除断言后同 payload 被接受为 DONE，断言有约束力。
- P2-1：正常段超时先落库探针通过，FAILED/error_code 在服务退出前可见；写入异常探针外层断言因 Rich 换行判据退出 1，不能记作测试通过，但原始日志独立检索命中 2 次相关错误，服务仍非零退出且读库为 `RUNNING/error_code=null`。其释放顺序仍由上述 P1 覆盖，不能判 clean。

## Full-domain review

- producer/边界：通过。真实 ffmpeg 容器输入、固定 argv/env 记录、管道 f32le、无临时 PCM、真实 multiprocessing Task/Result payload 均被 E3 测试覆盖。
- 结果契约/失败可见：解码器非零、结果超限、入队失败、后台未知异常、FAILED `GET result` 的已存 `error_code` 均有覆盖；完整结果与 DONE 同事务，P1 仅限崩溃/超时释放顺序。
- 重启语义：通过。QUEUED/RUNNING 重启收敛为 `FAILED[server_restarted]` 且不自动重跑。
- owner/生命周期：P1 如上；正常 DONE/FAILED sink 返回后才释放，上传连接关闭后任务仍继续。
- 熵增/WS 回归：未发现新增无第二消费者的抽象；旧 WebSocket、owner IPC、协议与分段定向回归 35 passed，未发现本 diff 的旧 WS 行为回归。

## Evidence and scope

- `git diff --check base..head`：通过；冻结改动 2037 行，低于 E3 3500 行预算。审查只读四份指定 spec、冻结 diff 和上述隔离探针，未读执行者报告/实现记录/验收记录。
- 隔离依赖下：`tests/test_http_file_runner.py tests/test_http_file_tasks.py` 为 `36 passed`；旧 WS/owner 定向集为 `35 passed`。OCR 前置扫描启动后约 8 分钟无 envelope/进展，已停止，未将其作为结论依据。
- PR #51 查询结果仍为 draft，head 为冻结 SHA；基线不可用，继承红无法判定。本 verdict 只新增本文件，建议先修复两个 P1 再进入下一轮。
