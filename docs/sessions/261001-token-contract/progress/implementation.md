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
