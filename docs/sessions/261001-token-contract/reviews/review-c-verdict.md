Frozen base: `c1e8808377cf205094711832076f51aebc77ff33`
H0: `6ed4f158f28e5d0e88be141d580c34918825f099`
H1: `3cc1d5faade0a82cfb74b09e9da1684af866584a`
risk-tier: `internal`
verdict: `PASS`
failure-visibility: clean

# 最终文件字符与时间契约独立终审

## 结论

冻结对象通过。本轮未发现违反 S1–S6 的 diff finding；35 项指定测试全部通过。错误输入会在成功 final 发布前失败，WS 端到端测试收到真实序列化的 error payload，未观察到伪成功 final。

## S1：file final 的 tokens、timestamps 与 text_accu

- `core/server/worker/pipeline.py:128-133` 在 token merge 前拒绝原始 token/time 数组长度不等；`195-206` 在置 `is_final=True` 前验证最终长度与正文拼接。
- `core/tools/token_sync.py:100-104` 在展开和后续 `zip` 前拒绝错长输入。
- 覆盖：`tests/test_pipeline_final_contract.py:251-265` 锁住 raw 错长与 final 同步后正文不符；`tests/test_token_sync_contract.py:96-98` 锁住同步函数的双向错长输入。
- `tests/test_token_sync_contract.py` 的同步样例覆盖保留、插入标点/空格、ITN/热词替换、删除和空正文，并检查生成时间来自原时间数组。

## S2：正常 final、短尾、空 EOF 与合法全空文件

- `core/server/worker/pipeline.py:87-94` 让跳过推理的 final EOF 进入共同收尾；`162-218` 对 normal final 与 EOF 统一格式化、同步、检查，并且只有检查成功后才设置 final。
- `tests/test_pipeline_final_contract.py:99-155` 覆盖已有 session 的短尾/空 EOF、1600 samples 边界和合法全空结果；短尾测试也断言没有额外推理。`tests/test_file_result_contract_e2e.py:28-67` 在真实 WS 与 worker 子进程链路上验证短尾最终 JSON payload。

## S3：不自洽结果的外部终态

- `core/server/worker/task_handler.py:186-208` 将管线异常转成 `inference_failed` 结果；`core/server/connection/ws_send.py:176-185` 对错误走 error-close 分支，不走 result JSON 发送。
- `tests/test_file_result_contract_e2e.py:70-132` 通过真实 WebSocket、TaskHandler 子进程和消费者收集实际 JSON，断言 raw 错长及 final sync 失败均产生 `inference_failed`，且没有任何成功 final。

## S4：text、schema、粒度与 SDK 兼容

- `core/server/worker/pipeline.py:168-169,201-206` 分别格式化 `text` 与 `text_accu`，只有 file final 检查 token 拼接；没有把两种正文合并成同一语义。
- `docs/reference/protocol.md:90-123` 说明 file final 的数组契约、不同 token 粒度、非固定粒度、时间戳含义、空结果及独立 `text`，未增加 schema/span/词尾保证。
- `tests/test_pipeline_final_contract.py:158-185` 覆盖 `text != text_accu`、原生 token 与对齐器两条路径。`sdk/capswriter_asr/outputs.py:53-64` 仍以 `transcript.text` 写 TXT，diff 未改 SDK 默认输出或旧结果解析域。

## S5：麦克风回退与非 final 兼容

- `core/server/worker/pipeline.py:153-156` 保留非 final 的早返回；`181-193` 的 mic 无 token 回退从均分字符中排除普通空格，并沿用 `duration / len(chars)` 的时间分配口径；`201` 将正文拼接拒绝限制在 file。
- `tests/test_pipeline_final_contract.py:188-250` 覆盖无空格回退、含空格回退时间、mic 短尾/非 final，以及 mic 原生 token 不受 file join 检查拒绝；`tests/test_aligner_failfast.py:182-186` 保留原 mic 均分时间用例。
- 未据此声称 mic 有新的精度保证。

## S6：HTTP 覆盖边界

- `tests/test_pipeline_final_contract.py:270-279` 只验证 `owner_kind="http"` 直接调用共用 TaskPipeline 的结果。
- 本轮没有验证 HTTP 生产路由/持久消费者；不声称完整 HTTP 生产覆盖，也没有真实模型声学精度结论。

## H0..H1 增量四问

1. 是否只恢复已批准范围：是。增量仅收窄 file join 校验并更新 mic 空格回退与边界测试。
2. 是否新增未经批准的抽象：否。
3. 是否新增无依据 state/fallback：否；沿用现有 mic 回退，只按 S5 排除普通空格。
4. 是否留下双路径：否；final 仍走共同收尾，file 与 mic 差异由任务类型条件限制。

## 验证、覆盖与未知

- 按卡面原命令，在清除 `PI_LEAD_SESSION`、`DELEGATE_DISPATCH_ID`、`DELEGATE_TASK_ID`、`DELEGATE_EXECUTOR` 的环境运行四个指定测试文件：`35 passed, 6 warnings`，耗时 `0.74s`，退出码 0。告警是 Python multiprocessing fork 在多线程进程中的弃用提示。
- H1 两项 CI `SUCCESS` 及旧 base 隔离环境 3 项目标断言失败为卡面提供的证据；本轮未打开原始日志或据此替代独立源码/测试核验。
- 未运行真实识别模型，因此声学准确度与真实模型输出分布未知。
- 被审源码、测试与公开协议严格限于任务卡白名单；额外读取卡面指定的 `review-discipline` 及本地 `pickup`、`long-run` 操作指引。未读取 `lead-workflow`、`docs/sessions/**` 中既有文件、前审/实施报告、consult 输出、delegate 外部状态或 Git 历史。源码审查输入保持独立。
