<!-- delegate-outcome: succeeded -->
# E5 HTTP 文件 SDK 独立审查结论

failure-visibility: p2-only

审查对象固定为 `9e40df201e9e3cdf4dad18deec7629974b3d6fb3..2fbf645fec8d72b9ec5c9fb48bde1ac14a50ce3e`，风险等级 `internal`。

## 结论

本轮确认 5 条 P2 契约缺陷和 1 条 P3 错误呈现问题。结果身份/最终标记、恢复上传身份、已提交上传完整性及旧 CLI 路径冲突均有真实 TCP 或子进程输入复现；畸形 URL 会打印原始 traceback。没有把 OCR 工具严重度直接当作本仓定级，也没有检查完整服务端实现。

## 发现项

### P2-1：结果响应未核对请求的任务身份

- **违反**：spec #4，最终结果必须是本次所请求 Job 的完整 RecognitionMessage，`task_id` 必须相符。
- **位置**：`sdk/capswriter_asr/http_client.py:734-773, 776-797`。请求使用恢复文件中的 job ID，但解析器只要求响应 `task_id` 非空，没有与该 ID 比较。
- **实际触发**：隔离 Python 3.12 / HTTPX 0.28.1 环境下运行 `/tmp/http-e5-review-cli-probe-dlg-20261001-030056-3c406f.py`。本机随机端口 TCP fixture 收到 `GET /v1/jobs/expected-job/result`，返回完整字段、`task_id="other-job"` 的 HTTP 200。真实 CLI 退出码为 0、stdout 报告写入成功，并把 `wrong task transcript` 写进 `clip.txt`。
- **工具级 P1 两问**：真实触发输入已由 TCP/CLI 复现；若服务把其他任务的结果映射到该请求，用户会静默收到错误文本，后果不可接受。
- **本仓级 P1 两问**：当前捆绑服务尚未实现该 HTTP 接口，README `sdk/README.md:58-60` 明说服务端支持待后续增量；仓内也没有现成 HTTP SDK 消费者，因此当前部署是否会触发未能证实。若触发，错误结果不可接受。按本轮实际部署证据列 P2，后续服务器集成前须修复或用真实生产者证明该 identity 不变式。

### P2-2：真值为真的非布尔 `is_final` 被当成最终结果

- **违反**：spec #4，`is_final` 必须是布尔 `true`；非最终片段、错类型不得返回成功 Transcript。
- **位置**：`sdk/capswriter_asr/http_client.py:736-768` 只检查 `not is_final`，之后原样放入 `Transcript.is_final`。
- **实际触发**：同一真实 CLI/TCP 探针返回 `task_id="expected-job"`、`is_final="false"` 及其他完整字段。CLI 退出码 0、报告成功，保存的 JSON 中 `is_final` 类型仍是 `str`，文本文件也已写出。
- **工具级 P1 两问**：该 HTTP 200 输入实测会触发；如果服务以非布尔 truthy 值表示非最终数据，客户端会把它作为完整结果交付，不能接受。
- **本仓级 P1 两问**：与 P2-1 相同，当前没有可用的捆绑 HTTP 服务验证此负态在真实部署的发生率；后果不可接受但触发率未知，列 P2。

### P2-3：恢复上传响应可以改写原 upload ID

- **违反**：spec #3/#4，resume 必须继续原 upload，成功响应的标识必须与请求一致。
- **位置**：`sdk/capswriter_asr/http_client.py:350-372, 452-476` 只核哈希/大小等；`_record_upload` 在 `:411-421` 将响应的 ID 写回恢复文件。
- **实际触发**：`/tmp/http-e5-review-probe-dlg-20261001-030056-3c406f.py` 的随机端口 TCP fixture 收到 `GET /v1/uploads/original-upload`，回 `upload_id="server-returned-other-upload"`、同大小/哈希、offset 0。SDK 随后对新 ID 发出 6 字节 PATCH 和 commit，返回新 job，并把恢复文件中的原 ID 覆写为新 ID。
- **工具级 P1 两问**：错误 ID 响应能触发改绑与错误目标上传；会丢失原恢复绑定，不能接受。
- **本仓级 P1 两问**：当前服务端 HTTP 接口尚未交付，是否会从生产者实际发出该响应未知；后果不能接受，按当前不可触发证据列 P2。

### P2-4：COMMITTED 状态可带未完成 offset 并被当成成功

- **违反**：spec #2/#3/#4，确认 offset 必须和已发出/已确认字节一致；COMMITTED 必须代表整个原文件已上传，不能伪造成功句柄。
- **位置**：`sdk/capswriter_asr/http_client.py:368-372, 507-520`。偏移只检查在 `0..size`，COMMITTED 分支只要求 `job_id` 存在，未要求 offset 等于文件大小。
- **实际触发**：`/tmp/http-e5-review-followup-probe-dlg-20261001-030056-3c406f.py` 中，恢复文件绑定 6 字节源文件；fixture 对 GET 原 upload 返回同 ID、`state="COMMITTED"`、`confirmed_offset=0` 和 job ID。SDK 只发 GET，没有 PATCH，却返回 `state=COMMITTED, confirmed_offset=0` 的成功句柄。
- **工具级 P1 两问**：真实 API 调用已触发；CLI/调用者会把未传输的源文件看成已提交，数据完整性不可接受。
- **本仓级 P1 两问**：当前捆绑服务尚未实现，生产是否会发出此互相矛盾的状态未知；后果不可接受但本轮第一问未在真实服务测到，列 P2。

### P2-5：名为 `http` 的旧 CLI 输入被新子命令截获

- **违反**：spec #1，旧 CLI 的位置参数和默认 WebSocket 用法不变。
- **位置**：`sdk/capswriter_asr/cli.py:145-149` 对首参数只要等于 `http` 就切换解析器；旧解析器仍把第一个参数定义为 `audio_file`。
- **实际触发**：补充探针在临时工作目录创建名为 `http` 的文件，并运行旧式调用 `python -m capswriter_asr http --url ws://127.0.0.1:1`。新入口退出 2 并显示 HTTP 子命令用法，没有走旧文件参数路径。
- **工具级 P1 两问**：精确路径冲突可复现；只影响此文件名，用户可改名重试，不造成错误结果或数据损坏。
- **本仓级 P1 两问**：当前 CLI 对该合法位置参数可触发，但影响仅是拒绝一次命令，不达到 internal 风险档 P1 后果；列 P2 兼容性缺陷。

### P3：畸形 HTTP URL 绕过 AsrError 并输出 Python traceback

- **违反**：spec #4 的明确错误呈现要求；非零退出满足，但未转成 `AsrError`。
- **位置**：`sdk/capswriter_asr/http_client.py:65-66, 318-321` 未拦 `urlsplit` 的 `ValueError`；`sdk/capswriter_asr/cli.py:114-116` 只捕获 `AsrError`。
- **实际触发**：补充探针用有效恢复文件调用真实 CLI `http result --url 'http://[::1' --resume-file ...`。命令退出 1，标准错误有 Python traceback/`ValueError`；无凭据值出现在输出中，也没有发出网络请求。
- **两问与定级**：工具上该输入会触发；后果是无用 traceback，但 CLI 非零且未泄露凭据。当前 HTTP server 未实现，服务端影响不适用；属于低风险错误呈现，列 P3。

## 已核对的不变式与边界

- **确认**：四组异步/同步 API 已导出；HTTPX 0.28.1 同时写入 SDK 直接依赖与 CI 依赖行，符合本卡约束；HTTP 客户端明确 `retries=0`、`follow_redirects=False`、`trust_env=False`；HTTP CLI 仅在首参数为 `http` 时分流，其余旧入口仍走原解析器；README 说明服务端尚未实现、submit 仅表示受理、status/result 单次调用、结果不自动删。
- **静态核查**：hash/read 按 64 KiB 循环；上传按 `chunk_bytes` 切原始 bytes，最大 1 MiB；每次 PATCH 的 Content-Length/Upload-Offset 来自实际 chunk 与 offset；首次网络调用前先写恢复文件；创建用保存的 Idempotency-Key；网络异常映射为 AsrError 并带恢复路径。
- **测试覆盖判断**：新增 `tests/test_http_client.py` 保留真实 loopback TCP、文件与子进程边界；现有用例未覆盖上列错 task_id、truthy 非布尔、GET upload ID 换绑、COMMITTED 短 offset 及旧文件名冲突。此次只跑了定向真实 TCP/CLI 探针，未运行全量测试套件。
- **未能核实**：没有 Windows 环境实证恢复文件 ACL；没有本轮范围内可运行的 HTTP 服务端来验证其真实 producer 响应/状态；没有评估模型识别质量或 HTTP 服务端完整功能。

## OCR 前置扫描

- 命令：按卡面固定 base/head 执行 `ocr-review --repo "$PWD" --from 9e40df201e9e3cdf4dad18deec7629974b3d6fb3 --to 2fbf645fec8d72b9ec5c9fb48bde1ac14a50ce3e --audience agent --concurrency 4 --background-file /tmp/ocr-bg-dlg-20261001-030056-3c406f.txt`；摘要 1,696 字节，低于 8,000 字节限制。
- JSON：`/tmp/ocr-result-dlg-20261001-030056-3c406f.json`，完整解析成功，171,523 字节；envelope `status=reviewed`、`reason=primary_selected`、profile `minimax`；该运行输出 13 项 `findings`，没有 `comments` 字段。`/tmp/ocr-stderr-dlg-20261001-030056-3c406f.txt` 仅记录主腿启动/耗时进度，没有凭据。
- 对工具候选独立处理：候选 1–5 由真实代码/探针确认；候选 7（畸形 URL）确认但按 P3；同步 I/O 事件循环阻塞、依赖元数据未在 CI 安装、CI 与 pyproject 依赖重复、固定 pin 不利于别的项目、timeout 时长、特殊文件系统 chmod 失败及格式校验顺序等候选均未作为本仓本轮 finding：分别因无对应契约、与 spec 明确 pin/CI 约束冲突、未证明真实触发后果或仅属非破坏性请求浪费。OCR 标注 severity 只作输入，不作为本仓结论。

## P1 两问汇总

| 候选 | 工具级触发/后果 | 本仓真实使用触发/后果 | 本轮定级 |
|---|---|---|---|
| 错任务/非最终结果 | TCP 与真实 CLI 输入可触发；会成功保存错误文本，后果不可接受 | README 明示当前 HTTP server 未实现，仓内没有既有 HTTP consumer；当前服务部署未证实可触发，未来 producer 行为未知 | P2，服务端接通前修复/复测 |
| 错 upload ID / 短 offset 的 COMMITTED | TCP API 可触发换绑或零上传成功；恢复身份/源文件完整性受损，后果不可接受 | 生产 producer 尚不存在于本轮可测范围，是否会送出矛盾响应未知 | P2，服务端接通前修复/复测 |
| 旧文件名 `http` | 当前 CLI 可触发退出 2；只是该参数组合被拒，无数据损坏 | 合法但罕见的旧位置参数；后果是命令失败且可改名，不满足 P1 后果档 | P2 |
| 畸形 URL | 错误输入会触发 traceback；非零且无凭据泄露 | 依赖用户手工 URL；后果局限于诊断质量 | P3 |

## 方法限制

没有读取实现方私有 report、推理或对话；审查依据是卡面 spec、冻结 diff、仓内 SDK/README/协议调用点、OCR 候选及本报告列出的独立探针。完整服务端、模型、Windows ACL 和 CI runner 环境未纳入验证。OCR 是初筛，完整代码审查没有因其完成而缩小范围。
