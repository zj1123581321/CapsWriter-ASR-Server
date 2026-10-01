# Review A：最终文件文字—时间契约独立审查

- verdict: **FAIL**（确认 1 项 P2 mic 兼容性偏差；未发现 P1）
- risk-tier: internal
- frozen base: c1e8808377cf205094711832076f51aebc77ff33
- H0: 6ed4f158f28e5d0e88be141d580c34918825f099
- review scope: 仅上述冻结范围；审查期间不随新提交扩展
failure-visibility: p2-only
- OCR: reviewed_fallback；primary 超时，deepseek 备腿完成；1 条 finding，经独立复现确认并定为 P2

## 结论

文件 final 主路径、token/time 来源长度 fail-fast、短尾/空 EOF 统一收尾、实际 WebSocket JSON 成功与错误发布均符合规格。检测在 result.is_final = True 之前执行；长度不等与最终拼接不等都会抛错。

唯一 finding 是统一 finalizer 把 file 专属的拼接约束扩展到了 mic，并将 mic 无 token 回退的空格计数从“去掉普通空格”改为“计入普通空格”。这改变了已声明保持的 mic token 切分及均分时间步长，不能按任务卡原样通过；定为 P2，不是 P1。

## 不变式核对

1. **文件 final 等长、拼接正文自洽，失败不冒充成功**：core/server/worker/pipeline.py:191-204 在置 is_final 前检查两个条件。tests/test_pipeline_final_contract.py:206-215 锁最终同步失配；真实 WebSocket 子进程用例 tests/test_file_result_contract_e2e.py:70-93,110-133 收到 inference_failed 且没有成功 final。成功 JSON 由同文件 :28-54 从 WebSocket 实际收集并断言。
2. **raw 数组不等在有损转换前拒绝**：pipeline 在 core/server/worker/pipeline.py:127-136 先比较模型/对齐结果，再进入 process_tokens_safely 与 merge；sync 在 core/tools/token_sync.py:100-109 先比较，再进入 _expand_tokens 的 zip。双方向 sync 长度、native pipeline raw 长度分别由 tests/test_token_sync_contract.py:89-98 与 tests/test_pipeline_final_contract.py:199-204 覆盖。对齐器正常配对由 tests/test_pipeline_final_contract.py:168-185 覆盖；其 tokens/timestamps 都由同一 align_res.items 列表逐项投影。
3. **正常 final、1599 短尾、空 EOF 共用收尾；短音频不推理，空结果合法**：core/server/worker/pipeline.py:88-94,151-155,161-165；tests/test_pipeline_final_contract.py:99-155 覆盖 1599、空 EOF、1600 边界与全空；短尾/空 EOF 均断言识别调用次数不增加。
4. **text 独立，协议/SDK/TXT 兼容边界**：tests/test_pipeline_final_contract.py:158-165 明确断言 text != text_accu。冻结 diff 只改 docs/reference/protocol.md，没有改 schema、协议版本或 SDK/TXT 实现；协议文档继续说明 token 起点与不承诺固定粒度/词尾精度。没有真实声学精度结论。
5. **非 final 与 mic**：非 final 分支仍在 core/server/worker/pipeline.py:151-153 返回；1599 分支中非 final 的 samples is None 仍返回 is_final=False。mic 有一项 P2 finding，见下。新 mic 回归用例 tests/test_pipeline_final_contract.py:188-196 和既有 tests/test_aligner_failfast.py::test_microphone_pipeline_keeps_character_time_fallback 只用无空格中文，未锁英文空格计数。
6. **WS / TaskHandler / TaskPipeline 子进程实际发包**：tests/conftest.py:137-181 启动真实 WebSocket 收发处理、队列与 multiprocessing TaskHandler；tests/harness/worker.py:17-25 构造真实 handler；假引擎仅注入可编程识别结果。tests/test_file_result_contract_e2e.py 捕获实际 JSON 成功与错误消息。HTTP 用例 tests/test_pipeline_final_contract.py:218-225 只是共享 pipeline 的直接测试；没有把它表述为完整生产 HTTP 测试，符合规格边界。

## Finding

### P2 — mic 英文空格被加入 token 且改变均分时间

- **违反规格**：用户边界 5；docs/sessions/261001-token-contract/design.md:27 要求 mic 无 token 均分路径保持既有行为。
- **代码位置**：core/server/worker/pipeline.py:183-188 把旧的 list(result.text_accu.replace(' ', '')) 改为 list(result.text_accu)；core/server/worker/pipeline.py:191-202 的文件拼接校验也没有按 task.type 限定。
- **真实触发方式**：task.type='mic'、final、识别正文含普通空格且没有真实 tokens 时，会进入该 fallback。独立运行实际 TaskPipeline.process 的本地探针 /tmp/capswriter-issue40-mic-space-probe.py 用 text='hello world'、1600 samples 复现。重跑命令：

    env -u PI_LEAD_SESSION -u DELEGATE_DISPATCH_ID -u DELEGATE_TASK_ID -u DELEGATE_EXECUTOR PYTHONPATH="$PWD" uv run --no-project --python 3.12 --with numpy --with rich --with 'websockets==15.0.1' --with colorama --with 'pytest>=9.1.1' --with soundfile --with 'pytest-asyncio>=1.4.0' --with 'httpx==0.28.1' python /tmp/capswriter-issue40-mic-space-probe.py

- **期望 / 实际**：旧 mic 口径会排除普通空格，10 个 token 均分 0.1 秒，步长 0.01；当前实际输出 11 个 token（包含空格），11 个 timestamps，步长 0.00909091。正文仍是 hello world。这不是声学精度判断。
- **严重度判定**：OCR 标注 high；本仓判 P2。实际 TaskPipeline 分支已由本地探针执行，且 mic fallback 是现有支持行为；但真实部署中“无 token 且正文含英文空格”的发生频率未量，具体生产模型输出也未实测。当前可确认的后果是 mic 下游 token 数与时间均分改变，正文未变；现有证据不足以定为 P1。

## 验证与限制

- 指定环境命令（Python 3.12、websockets==15.0.1；清除 PI_LEAD_SESSION、DELEGATE_DISPATCH_ID、DELEGATE_TASK_ID、DELEGATE_EXECUTOR；TASK_ID 起跑环境未设置）运行三个新增测试文件及 tests/test_aligner_failfast.py：32 passed, 6 warnings in 0.60s，退出码 0。六条 warning 是 multiprocessing fork 对多线程进程的弃用警告。
- OCR envelope 为完整可解析 JSON（7055 bytes）：status=reviewed_fallback、coverage=complete、cli_status=complete、1 条 finding；reason=primary=leg_timeout; backup:deepseek=success。primary 诊断缓存：/home/zlx/.cache/ocr/ocr-failure-minimax-1790844152038679553.stderr；完整 envelope：/tmp/capswriter-issue40-review-a-ocr.json。finding 经上面的独立 mic 探针核实；工具标注 high 未直接当成本仓等级。
- 未做 base scratch-worktree 红验抽查。未读取 docs/sessions/261001-token-contract/progress/implementation.md，以遵守任务卡“不得读实现执行器 report/推理”的独立性边界；其余冻结实现、协议和新增测试 diff 已逐项审查。
- 主干基线在派发时不可用（gh api request failed）；因此继承红/新红无法比较，继承红标记为未能判定。未测真实 HTTP 服务、真实模型音频质量或部署行为。
