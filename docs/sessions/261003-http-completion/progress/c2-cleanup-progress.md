# M4-C2 进度

## 当前阶段

implementing：C2 实现、行为证据与双版本全量验证完成；PR #62 保持 draft，等待独立审查和 C1 合入顺序。

## 本段结论

真实 HTTP listener、单 I/O worker、runner 和 TCP 客户端已证明：DONE 落库后 runner 仍持有 decoder 引用时源文件保留；runner 释放后周期任务删除源文件，HTTP 结果仍可领取。七天边界、DONE/FAILED、terminal_at/任务状态、partial、未登记文件、幂等重复清理、append/commit/终态写入各五轮并发 I/O、后台 fatal 与 shutdown 在途 I/O 均有断言。定向测试 `5 passed in 1.74s`。固定 `websockets==15.0.1` 与最新 `websockets==17.1` 两轮全量均 `451 passed, 3 skipped`。

## 决策与否决

- 年龄只读 `jobs.terminal_at`，候选条件为 DONE/FAILED 且 `terminal_at <= now - 7 天`；不增加 schema、账本或配置项。
- 每轮在网络 loop 只读 `HttpFileRunner.active_jobs` 快照，SQL/unlink 与现有 I/O worker 操作串行。
- 周期同时将逾期 UPLOADING 持久化为 EXPIRED；partial 文件保留并继续 source-presence 计费，GET 返回 410。
- 不加源释放标记或第二账本；文件存在性本身是 C1 的计费真源，ENOENT 后自然释放额度，重复清理幂等。
- 两轮 suite skips 都只有 ForceAligner 两项和缺少 Silero VAD/ONNX 的一项；HTTP 测试全收集、无 HTTP decode skip。
- PR #62 以 PR #60 为 base，保持 draft；C1 合并前不 ready/merge，不部署生产。
- 上传状态、任务、结果与元数据保留；未登记文件与 UPLOADING/EXPIRED partial 不进入终态候选。
- 仅 unlink 的 ENOENT 按幂等处理，其他错误继续进入 fatal 监督链。

## 下一步唯一动作

由 Pi 主脑独立审查 PR #62；PR #60 合并后再将 #62 base 切到 master 并完成其 gate。
