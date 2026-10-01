# Review B：最终文件结果契约反向审查

- verdict: **FAIL（审查独立性受损）**；对冻结代码的技术核验未发现当前契约缺陷
- risk-tier: internal
- frozen base: `c1e8808377cf205094711832076f51aebc77ff33`
- H0: `6ed4f158f28e5d0e88be141d580c34918825f099`
- H1: `3cc1d5faade0a82cfb74b09e9da1684af866584a`
- 增量审查范围: H0..H1
- 全量审查范围: frozen base..H1
failure-visibility: clean

## 结论

H1 当前代码满足本卡文件最终结果契约：raw token/time 长度不等在合并前拒绝，正常 final、已有 session 的短尾和空 EOF 共用格式化与校验，合法全空文件结果可成功；文件 join 等式在 `is_final=True` 前检查。麦克风普通空格不进入无 token 均分回退，文件专属 join 检查不拒绝麦克风原生 token。当前代码审查未发现新的 P1/P2/P3 实现 finding。

整体 review verdict 仍判 **FAIL**：审查过程中一次宽范围 `git diff H0..H1` 输出意外包含任务卡明确排除的实现进度记录和前轮 verdict。后续没有再读取或引用这些内容，结论根据冻结源码、设计规格和本轮测试重新核对；但既然排除材料已显示，不能把本轮称为完全独立的盲审。该过程偏差不构成当前业务代码 finding。

## H0..H1 修复增量四问

1. **只修登记的范围偏差吗？是。** H0 的 `_finish_final_result` 在无 token 麦克风回退中把普通空格计入字符数组（H0 `core/server/worker/pipeline.py:184-189`），并对所有任务要求 join 等式（H0 `:197-202`）。H1 将空格过滤恢复为原有口径（`core/server/worker/pipeline.py:187-193`），并把 join 约束限定到 `task.type == 'file'`（`:201-206`）；新增用例分别锁英文空格、短尾/非 final、麦克风原生 token（`tests/test_pipeline_final_contract.py:199-248`）。增量没有扩大业务范围。
2. **新增未经批准的抽象吗？没有。** H0..H1 没有增加 helper、状态、schema、配置或依赖。H0 已有的 `_finish_final_result` 在 H1 仍由短尾/空 EOF 路径和正常 final 路径共同调用（`core/server/worker/pipeline.py:91-94,152-156`），用于保持同一收尾路径。
3. **增加无依据状态或 fallback 吗？没有。** H1 只恢复既有麦克风字符回退的空格计数（`core/server/worker/pipeline.py:181-193`），并给既有拼接校验加上文件任务范围条件（`:201-206`）。没有增加新状态、事实源、重试或 fallback。
4. **留下双路径吗？没有。** 普通 final 与已有 session 的短尾/空 EOF 都进入同一个 `_finish_final_result`（`:91-92,156`）；长度检查和 file join 检查各只有一处（`:195-206`）。麦克风回退仍是同一 finalizer 中既有的无 token 分支（`:181-193`）。

## 全量不变式矩阵

| 契约 / 边界 | 代码与测试锁定点 | 判定 |
|---|---|---|
| 文件 raw token/time 不等在 zip/合并前 fail-fast | `core/server/worker/pipeline.py:128-147` 先检查再处理/合并；`core/tools/token_sync.py:100-110` 先检查再展开；`tests/test_pipeline_final_contract.py:251-255`、`tests/test_token_sync_contract.py:89-98` | 通过；两种错长方向都有断言 |
| 正常 file final 在置 final 前格式化、同步和校验 | `core/server/worker/pipeline.py:135-156,162-208`；`tests/test_pipeline_final_contract.py:138-185,258-267` | 通过 |
| 1599 samples 短尾不新增推理且沿用已有 session 收尾 | `core/server/worker/pipeline.py:87-94`；`tests/test_pipeline_final_contract.py:99-122` 断言识别调用次数仍为 1，格式化、长度和 join 均成立 | 通过 |
| 空 EOF 有 session 时正常 final；初始全空结果合法 | `core/server/worker/pipeline.py:87-94,162-208`；`tests/test_pipeline_final_contract.py:124-155` | 通过；空数组与空 `text_accu` 成功 |
| `text` 与 `text_accu` 可不同，不固定 token 粒度 | `tests/test_pipeline_final_contract.py:158-185`；`docs/reference/protocol.md:90-120` | 通过；文档声明模型粒度不固定，未声称真实声学精度 |
| formatter 输入/输出包含插入、替换、删除、空格、标点、ITN、空串 | `core/tools/token_sync.py:100-153`；`tests/test_token_sync_contract.py:19-86`；短尾 formatter 测试 `tests/test_pipeline_final_contract.py:99-122` | 通过；测试断言输出 join、配对长度与时间继承 |
| 麦克风无 token、英文空格、短尾、非 final 和原生 token 兼容 | `core/server/worker/pipeline.py:181-205`；`tests/test_pipeline_final_contract.py:188-248` | 通过；空格不计入均分字符，join 等式不扩成 mic 拒绝条件 |
| 每个 pipeline producer 返回边界 | `core/server/worker/pipeline.py:91-94`（无 samples 的 final/non-final）、`:153-156`（有 samples 的 non-final/final）、`:208,218`（finalizer 校验后单一返回）；`tests/test_pipeline_final_contract.py:99-155,199-248,258-267` 覆盖短尾/空 final、有 samples 的 non-final、正常 final 与失败 | 通过；无 samples 的 non-final 返回由源码核对，本轮四文件套件没有专门断言；异常分支另由下行 WS 错误测试覆盖 |
| WS/TaskHandler 子进程发布真实 payload；不自洽不发布成功 final | `tests/conftest.py:136-169` 启动真实 WebSocket 收发和 worker 子进程；`tests/harness/worker.py:17-25` 构造真实 `TaskHandler`；`core/server/worker/task_handler.py:191-208` 将异常转为 `inference_failed`；`core/server/connection/ws_send.py:99-112,176-189` 序列化并发送 JSON；`tests/test_file_result_contract_e2e.py:28-54,70-94,110-133` 从真实 WS 收集消息 | 通过；成功断言实际 JSON 的 tokens、timestamps、text_accu；失败断言实际 error 且无成功 final |
| HTTP 共享 owner 测试声明边界 | `tests/test_pipeline_final_contract.py:270-277` 直接调用共享 pipeline | 通过；这里只证明共享 pipeline，不宣称完整 HTTP 服务/模型验证 |
| schema、SDK TXT 与旧服务器接收域 | frozen base..H1 文件清单不含 schema 或 SDK；协议改动仅补文件 final 结果说明 | 未发现兼容性扩张；未对真实旧服务器部署做端到端测试 |

## 测试与旧基线红验证据

- 在无会话身份的 Python 3.12、`websockets==15.0.1` 和任务卡列出的依赖下运行四个指定文件：`35 passed, 6 warnings in 0.56s`。完整原始输出：`/tmp/capswriter-token-contract-review-b-261001-tests.log`。六条 warning 是 multiprocessing fork 对多线程进程的弃用提示。
- 按要求通过 `scripts/git/scratch-worktree.sh` 在 base `c1e8808377cf205094711832076f51aebc77ff33` 建临时树，仅复制两个测试文件，不复制生产代码。实际树为 `/home/zlx/scratchpad/vt-tVBVch/worktree`；导入来源确认是该树的 `core/server/worker/pipeline.py` 与 `core/tools/token_sync.py`。测试结果为 3 个目标失败：1599 短尾未格式化，实际 `text='一二'` 而断言期望 `一二。`；两种 raw 长度不等输入都未抛出预期 `ValueError`。这些是目标断言失败，不是导入错误。完整原始输出：`/tmp/capswriter-token-contract-review-b-261001-base-red.log`；scratch 树已自动清理。
- 任务卡记录 H1 两项 CI 为实际 `SUCCESS`；本地测试仍独立执行。派发时主干基线不可用（`gh api request failed`），因此继承红与新红无法区分，继承红状态为未能判定。
- OCR 范围：任务卡记录已有 `c1e8808..6ed4f15` 扫描，状态 `reviewed_fallback`（主腿超时、deepseek 备腿成功）。它只覆盖旧范围；本轮未调用 OCR，也不宣称 H1 全量已扫。

## 残余风险与过程记录

- 未测真实模型声学时间精度、模型输出粒度的分布或字幕质量；这些保持未知。未运行真实 HTTP 服务；HTTP 结论限于共享 pipeline 直接 owner 测试。
- **踩坑 / 闸绕过：** 为快速读取 H0..H1 增量，使用了未按文件过滤的 diff，输出包含任务卡排除的进度记录和前轮 verdict。没有主动绕过审查闸；这是实际发生的范围越界，已停止继续读取并明确影响独立性。
- **偏差：** 当前技术代码核验未发现业务 finding，但由于上述输入污染，整体 review verdict 为 FAIL。若要求完全独立盲审，应由新的 review 卡重新审查。
- **最贵步骤：** 指定四文件测试套件，实测 pytest 执行时间 0.56 秒；base 红验 pytest 0.09 秒。未执行耗时 OCR 或生产模型验证。
