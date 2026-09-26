## 里程碑 1：启动 SHA 固化
- 当前阶段：实现
- 本段结论：`ServerState` 构造时读取一次 Git SHA，`/health` 只返回保存值。临时 Git worktree 假引擎服务验收通过：启动后 HEAD 改变，健康检查仍返回启动 SHA，20 次请求期间只执行一次 `git rev-parse`。
- 关键决策与已否决方案：将值保存在现有 `ServerState`，不在请求处理中查询磁盘版本。
- 下一步唯一动作：补充部署文档并进行红验。
