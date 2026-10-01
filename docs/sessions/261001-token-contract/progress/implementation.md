# 实施进度

## 当前阶段
红验已落盘，准备在基线红验后实现最终收尾契约。

## 本段结论
- 设计锚点已按批准原文写入 `docs/sessions/261001-token-contract/design.md`。
- 最新可用 `origin/master` 与任务基线同为 `c1e8808377cf205094711832076f51aebc77ff33`。
- 基线三类断言均真实失败：短尾绕过最终格式化、`token_sync` 不等长输入被静默截断、最终正文不等式仍成功返回。
- 新增单测覆盖同步输入契约、pipeline 正常/短尾/空结果/原生模型/对齐器/HTTP owner，以及真实 WS 子进程发布边界。

## 关键决策与已否决
- 只在 pipeline 合并正常 final 与短尾/EOF final 的必要收尾，不统一 `text` 与 `text_accu`。
- 不新增 schema、版本、fallback、重试或错误码；不修改 HTTP 生产入口。
- 跨进程测试仅扩展既有 fake engine 的可编程输出与故障注入，默认输出保持不变。

## 下一步唯一动作
实现 `pipeline.py` 的统一 final 收尾和 `token_sync.py` 的 raw 数组契约检查，然后运行本阶段窄测并提交。

## 里程碑：实现与窄绿

### 当前阶段
核心实现完成，窄契约测试通过，准备补充协议文档和全量验证。

### 本段结论
- `TaskPipeline` 将正常 final 与既有 session 的短尾/空 EOF final 合并到同一收尾函数；收尾在 `is_final=True` 前检查数组长度和正文拼接。
- 原生模型 token/time 不等长在 `process_tokens_safely`、merge 和任何有损 zip 前失败；`token_sync` 入口也拒绝不等长 raw 数组。
- 麦克风无真实 token 的均分回退保留，并把空格作为正文 token 以满足最终拼接等式；合法全空文件结果继续成功。
- 真实 WebSocket/TaskHandler 子进程窄测通过：32 passed，含成功 final payload 和两类错误终态无成功 final。

### 关键决策与已否决
- 最终收尾只新增一个必要的 `_finish_final_result`，实际消费者是正常识别 final 与短尾/空 EOF final 两条既有入口。
- 不覆盖 `text_accu` 掩盖同步错误，不新增错误码、状态、重试或 fallback；错误继续由 TaskHandler 转成 `inference_failed`。

### 下一步唯一动作
更新 `docs/reference/protocol.md`，说明最终等式、空/标点计数、时间继承和 text 独立语义，然后运行两档全量测试。

## 里程碑：协议说明

### 当前阶段
协议文档已补充最终文件结果契约，准备运行两档全量验证并完成交付报告。

### 本段结论
- `protocol.md` 明确最终 `tokens`/`timestamps` 等长、`"".join(tokens) == text_accu`、合法全空结果和下游直接校验示例。
- 文档明确 `text` 独立于字幕正文，标点/空格计入拼接，timestamp 是起点且无词尾承诺。
- 文档覆盖原生模型、外挂对齐器、非 final、文件短尾/空 EOF、麦克风无真实 token 均分回退范围；协议版本和 SDK 默认行为未改。

### 关键决策与已否决
- 不把模型粒度、标点粒度或声学精度写成固定承诺；不把普通 `text` 统一成 `text_accu`。
- 文档只描述已实现的共享 pipeline 规则，不扩展 HTTP 生产入口或声学验证结论。

### 下一步唯一动作
在裸消费环境依次运行卡面两条全量测试命令，记录每档完整结果，再核对 diff、远端 PR 与最终 SHA。

## 里程碑：全量验证完成

### 当前阶段
实现、文档和两档全量离线验证完成，准备推送最终 head 并交付报告。

### 本段结论
- 固定 `websockets==15.0.1`：286 passed、3 skipped、91 warnings，106.34s。
- 当前 `websockets`：286 passed、3 skipped、91 warnings，103.02s。
- 两档均在清除 `PI_LEAD_SESSION` 与 `DELEGATE_*` 的裸消费环境执行，未安装主仓虚拟环境，未使用真实模型。
- 基线到最终 head 的净差异为 691 行，低于 850 行目标，也低于 1600 行硬上限。

### 关键决策与已否决
- 只声明 fake engine、TaskPipeline、WS 发布边界已验证；不把测试结果表述为真实模型声学精度或完整 HTTP 生产端到端验证。
- draft PR 不擅自标 ready、合并或部署；检查结论按 SUCCESS、SKIPPED、未完成分别记录。

### 下一步唯一动作
提交本进度记录，推送最终 head，读取远端 PR head/body/checks 和工作树状态，写入完整执行报告。

## 里程碑：审查后范围修复

### 当前阶段
已按 Review-A 的唯一 P2 做最小减法修复：恢复 mic 旧空格口径，文件正文拼接检查只约束 file。准备推送 H1 并保持 PR 41 为草稿。

### 本段结论
- 独立探针与新增回归确认 H0 把 `hello world` 切成 11 个含空格 token、步长约 0.00909；修复后恢复 10 个非空格 token、步长 0.01，`text_accu` 仍为原文。
- 最终 `"".join(tokens) == text_accu` 只对 `task.type == 'file'` 强制；mic 有原生 tokens 时不再被该等式误拒。raw 长度检查和最终等长检查保留。
- 窄测 35 passed；裸消费环境两档全量均为 289 passed、3 skipped、91 warnings。
- Review-A 结论文件按 `668be7c` 原样纳入，历史 FAIL 未改写。

### 关键决策与已否决
- 不把 mic 升格为全任务 join 保证，不新增状态、fallback 或错误码。
- 不修改审查 verdict 文本来让它变绿；修复只记本进度段。

### 下一步唯一动作
推送 H1，回读 PR 41 草稿正文确认仅 file 保证并保持 draft，写入执行器报告后释放。
