<!-- delegate-outcome: succeeded -->
## 位置表与逐行判定
failure-visibility: clean
结论：R4 位置表闭合，未发现遗漏的 HTTP 释放路径；本轮 verdict 为 clean。

| 位置 | 可带 HTTP key | 先落库 | 判定 |
|---|---|---|---|
| `ws_recv.py:_acquire_segment_slot:175` | 否，调用链只传 WS key | 不适用 | 合规 |
| `ws_recv.py:ws_recv:534,538,549,551,666` | 否，`active` 来自 `connection_tasks`，其余为 `make_task_key('ws',...)` | 不适用 | 合规 |
| `ws_send.py:schedule_error_close:46` | 否，字面量 WS key | 不适用 | 合规 |
| `ws_send.py:fail_active_tasks:99` | 否；HTTP 分支在此之前 `finalize_http_job` 后 `continue` | 不适用 | 合规 |
| `ws_send.py:ws_send:167,171` | 是 | `await sink(result)`；最终结果与 DONE 同事务，失败经 `runner.fail_job` | 合规 |
| `ws_send.py:ws_send:180,210` | 否；HTTP 分支已 `continue`，仅 WS | 不适用 | 合规 |
| `process_manager.py:171` | 否；HTTP 在 `161-168` 走 `finalize_http_job`，此处是 WS 兜底 | 不适用 | 合规 |
| `http_file_runner.py:fail_job:393` | 是 | `await http.fail_job` 后才 `transition_terminal`，再移除任务 | 合规 |

释放原语反向核对：`state.py:264` 的 `terminal_event.set()` 与 `:273` 的
`active_http_jobs.remove()` 只有 `transition_terminal` 一处实现；HTTP runner 的
`:433 _run_gate.release()` 发生在 `_run_under_gate` 返回之后，`:545` 的段槽补偿
释放只有终态事件/状态已发生后才可达；`state.py:299` 的结果槽释放位于 HTTP sink
之后。正常停止取消任务属于重启收敛路径，`_stopped` 同时阻止新任务，不形成新 Job
越过持久化闸门。HTTP listener 自身的 mailbox/handler/body semaphore 不属于 Job owner。

## 阻塞 FAILED 写入探针
使用 `/tmp/e3-r4-venv`（补齐 `aiohttp==3.14.3`，避免全局环境的模块级 skip）。
将当前探针夹具拷入 42f6da3 临时 worktree 后：
- `test_http_owner_released_before_persist[fail_active_tasks]`：红，观测到
  `a_db=('RUNNING', None)` 但 `a_owner=False`、内存记录已终态。
- `test_http_owner_released_before_persist[segment_timeout]`：红，观测到
  `a_db=('RUNNING', None)`、第二 Job 变为 `RUNNING`、解码启动数为 2。
- 同样的两参数在 6222e70：绿，2 passed；修复后组合套件另有
  `test_finalize_giveup_keeps_owner_when_persist_times_out` 通过。

## worker 崩溃探针与顺序变异
- `test_worker_crash_persists_failed` 在 42f6da3：红；真实假引擎执行
  `os._exit(3)` 后，主进程退出前只观察到 `RUNNING/None`。
- 同用例在 6222e70：绿；真实 worker 调用一次后，退出前观察到
  `FAILED/internal`，主进程非零退出。
- 在 6222e70 临时把 `fail_job` 的持久化移到 `transition_terminal`/释放之后：
  `test_http_owner_released_before_persist` 两参数均红，证明探针对顺序变异敏感。
- AST 位置表 `tests/test_http_release_invariant.py`：7 passed；冻结差异
  `git diff --check`：通过。探针不是恒真断言。

## 持久化超时行为与收口
真实独立服务进程中令 `CW_SEGMENT_TIMEOUT=1`，让真实 `HttpServer.fail_job`
阻塞 60 秒；`wait_for` 超时后日志明确写出“持久失败事实未落库，owner 不释放”，
进程以 `exitcode=1` 退出，数据库仍为 Job A `RUNNING/NULL`、Job B
`QUEUED/NULL`。因此“超时不释放且非零退出”实测成立。

最终核对：PR #51 仍 `OPEN`/draft，head 为 `6222e70d19fcc0b6b5f5b04bea80b9c4573b0696`。
