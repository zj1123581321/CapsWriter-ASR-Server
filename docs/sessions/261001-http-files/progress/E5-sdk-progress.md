阶段：verifying

结论：已完成显式 HTTP 文件 SDK、同步包装、CLI 四命令、依赖、文档和恢复文件；旧 WebSocket SDK/CLI 入口保持不变。真实 TCP fixture 已锁定首请求前持久化、Bearer/幂等键、Content-Length、Upload-Offset、原始切片、三类响应丢失恢复和坏响应失败可见。

锁定及否决：锁定 HTTPX 0.28.1、单次请求、关闭环境代理/重定向/自动重试、独立 capability 仅进 Authorization、恢复地址/源身份/选项不可替换；否决自动重试、自动轮询、WebSocket fallback、客户端转码和静默覆盖恢复文件。

唯一下一步：取得任务卡 Verify-Command 的可信退出码，完成 git diff/status、推送和 draft PR 回读核对。
