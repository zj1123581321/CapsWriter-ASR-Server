### 里程碑 1：任务键与结果错误通道

- 当前阶段：实现
- 本段结论：worker 会话和缓冲已改用 `(socket_id, task_id)`；推理异常转为 `inference_failed`，连接收发使用独立出站队列。T7 中间段异常与同任务号跨连接两项已移除 xfail，目标测试为 4 passed、1 xfailed。
- 关键决策与已否决方案：遵守卡面禁止修改 `pipeline.py`，由 `WorkerState.sessions` 的 tuple-key 映射按当前 socket 适配其字符串成员检查。
- 下一步唯一动作：补齐进程死亡/卡死监控与服务退出码，再添加错误契约行为测试。
