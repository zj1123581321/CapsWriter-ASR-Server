# M4-C2 进度

## 当前阶段

implementing：核心清理路径已红验并完成首轮实现，正在补齐候选边界、监督失败与停机证据。

## 本段结论

真实 HTTP listener、单 I/O worker、runner 和 TCP 客户端已证明：DONE 落库后 runner 仍持有 decoder 引用时源文件保留；runner 释放后周期任务删除源文件，HTTP 结果仍可领取。首轮定向测试 `1 passed in 0.37s`。

## 决策与否决

- 年龄只读 `jobs.terminal_at`，候选条件为 DONE/FAILED 且 `terminal_at <= now - 7 天`；不增加 schema、账本或配置项。
- 每轮在网络 loop 只读 `HttpFileRunner.active_jobs` 快照，SQL/unlink 与现有 I/O worker 操作串行。
- 上传状态、任务、结果与元数据保留；未登记文件与 UPLOADING/EXPIRED partial 不进入终态候选。
- 仅 unlink 的 ENOENT 按幂等处理，其他错误继续进入 fatal 监督链。

## 下一步唯一动作

补齐七天边界、DONE/FAILED 与 partial/未登记负向对照，并验证周期失败监督和 shutdown 在途 I/O 收尾。
