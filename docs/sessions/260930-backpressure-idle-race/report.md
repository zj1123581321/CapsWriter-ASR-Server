# 背压与上传空闲竞态修复报告

## 改动摘要

- `TaskLifecycle` 用 `idle_state_version`（单调递增版本号）和
  `idle_state_condition`（条件变量）替代 `idle_state_event`。
- `_acquire_segment_slot` 在进入、退出背压时都在条件变量锁内更新
  `backpressured`、递增版本号并通知等待者；两个写入点仍是原有的背压生命周期。
- `_receive_compressed_frame` 每轮记录当前版本号，并等待版本号发生变化。
  即使背压协程在 waiter 启动前完成状态转移，`Condition.wait_for` 也会在持锁检查时
  观察到已变化的版本号，不再依赖 `clear()` 与 `wait()` 之间没有 `set()`。
- 裸 PCM 路径的 `remaining` 计算、超时值、协议字段和 `_acquire_segment_slot` 签名均未改动。
- 版本号的两个生产/消费方是已有的背压写入协程和压缩接收协程；条件变量是实现
  “版本变化前阻塞、变化后唤醒”的必要同步原语，不增加独立业务抽象。

## 回归测试与约束力验证

新增测试为 `tests/test_backpressure.py::test_compressed_idle_state_version_survives_transition_before_wait`。
测试通过真实 `_acquire_segment_slot` 进入背压，并把旧事件的 `clear -> set -> clear`
丢信号序列安排在 waiter 启动前；正确实现仍保持接收循环存活，随后客户端帧可以正常返回。

正确实现：

```text
$ python3 -m pytest tests/test_backpressure.py::test_compressed_idle_state_version_survives_transition_before_wait -q
.                                                                        [100%]
1 passed in 0.10s
```

改坏实现的实际验证：临时将 `_receive_compressed_frame` 改回
`record.idle_state_event.clear()` + `record.idle_state_event.wait()`，写端仍使用真实
`_acquire_segment_slot`。同一命令实际变红：

```text
$ python3 -m pytest tests/test_backpressure.py::test_compressed_idle_state_version_survives_transition_before_wait -q
F                                                                        [100%]
...
>           assert not receiving.done(), "状态转移被吞后不应按原始 deadline 超时"
E           AssertionError: 状态转移被吞后不应按原始 deadline 超时
E           assert not True
...
1 failed in 0.12s
```

坏实现只做了临时验证，已恢复并提交版本号实现；当前源码不再引用
`idle_state_event`。

## 测试结果对比

### 指定套件

改动前实际结果（卡面写明的 `test_backpressure.py` 基线为 6 passed）：

```text
$ python3 -m pytest tests/test_backpressure.py tests/test_protocol_v2.py -q
..............F                                                          [100%]
1 failed, 14 passed, 28 warnings in 28.42s
```

基线失败：
`tests/test_protocol_v2.py::test_task_end_logs_one_structured_line_for_success_and_failure`
中 `caplog` 没有捕获两条 `task_end` 日志，实际 `own_lines` 为 0。

改动后：

```text
$ python3 -m pytest tests/test_backpressure.py tests/test_protocol_v2.py -q
...............F                                                         [100%]
1 failed, 15 passed, 28 warnings in 28.64s
```

新增回归测试计入 `test_backpressure.py` 后，该文件为 `7 passed`；唯一失败仍是上述
相同的既有日志捕获测试，未增加新红。

### 全量套件

改动后单独运行全量套件的实际结果：

```text
226 passed, 1 failed, 3 skipped, 81 warnings in 90.82s (0:01:30)
FAILED tests/test_protocol_v2.py::test_task_end_logs_one_structured_line_for_success_and_failure
```

该失败与指定套件基线相同，归类为继承红；新红为 0。改动前的全量基线后台采样
未取得完整可判定结果，因此不把它冒充为完整基线；卡面同时标注主干基线不可用。

## 风险与遗留

- 继承红仍存在：协议日志测试需要单独处理 `caplog` 捕获问题，本卡未扩大范围修复。
- `asyncio.Condition` 与版本号只改变内部背压唤醒实现，不改变上传超时配置、协议语义或
  裸 PCM 行为。
- 设计文档提交 `b6dabbb` 不在本 worktree 的当前树中，已按卡面带回其原文内容；原始
  cherry-pick 的提交钩子因 `pi-lead` 与当前 `cursor` 执行器归因冲突而拒绝，随后以
  `[codex]` 提交相同设计文档内容，未使用 `--no-verify`。

## 提交

- `1c5ba6e [codex] 修复背压空闲唤醒竞态`
- `6f7fc50 [codex] 带回背压竞态设计文档`

