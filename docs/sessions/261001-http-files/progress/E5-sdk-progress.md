阶段：implementing

结论：已完成显式 HTTP 文件 SDK 的恢复文件、真实二进制分块上传、显式恢复、状态/结果查询及同步包装；旧 WebSocket SDK/CLI 入口保持不变。真实 TCP fixture 已锁定首请求前持久化、Bearer/幂等键、Content-Length、Upload-Offset、原始切片和坏响应失败可见。

锁定及否决：锁定 HTTPX 0.28.1、单次请求、关闭环境代理/重定向/自动重试、独立 capability 仅进 Authorization、恢复地址/源身份/选项不可替换；否决自动重试、自动轮询、WebSocket fallback、客户端转码和静默覆盖恢复文件。

唯一下一步：完成 CLI/文档和全量 Verify-Command，逐项记录真实网络错误与旧 WebSocket 回归结果。
