阶段：done

结论：已完成显式 HTTP 文件 SDK、同步包装、CLI 四命令、依赖、文档和恢复文件；旧 WebSocket SDK/CLI 入口保持不变。真实 TCP fixture 已锁定首请求前持久化、Bearer/幂等键、Content-Length、Upload-Offset、原始切片、三类响应丢失恢复和坏响应失败可见；全量 Verify-Command 已退出 0。

锁定及否决：锁定 HTTPX 0.28.1、单次请求、关闭环境代理/重定向/自动重试、独立 capability 仅进 Authorization、恢复地址/源身份/选项不可替换；否决自动重试、自动轮询、WebSocket fallback、客户端转码和静默覆盖恢复文件。

唯一下一步：主脑接手 draft PR #36，按服务端 E1–E4 完成后的集成依赖继续审查与验收。

阶段：repairing

结论：原卡未锁结果 task_id 与请求 job_id 一致、is_final 必须是布尔 true、GET upload 不得换绑、COMMITTED 必须 offset=size 且 job_id 合法；本轮仅对已有字段补比较/类型 guard 并用真实 TCP 负态回归锁死。未修文件名 http 与畸形 URL，未新增状态或重试。

锁定及否决：锁定原 HTTP_SCHEMA、无自动 retry、原文件二进制、独立 Bearer、首请求前保存恢复、旧 WS 兼容；否决用 P2/未上线豁免原验收、否决新增机制/DTO/fallback。

唯一下一步：全量 Verify-Command 与五次网络负态回归后 push 原 PR36，供主脑做 H0 到 H1 增量审。
