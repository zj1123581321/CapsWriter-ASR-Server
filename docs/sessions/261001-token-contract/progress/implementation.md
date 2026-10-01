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
