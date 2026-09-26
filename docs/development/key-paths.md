## 关键路径

- [服务端入口](../../start_server.py) — 初始化并启动 ASR Server。
- [Proxy 入口](../../start_proxy.py) — 初始化并启动 ASR Proxy。
- [服务端配置](../../config_server.py) — 网络、模型、并发、分段和 GPU 参数。
- [Proxy 配置](../../config_proxy.py) — 后端地址、权重与监听参数。
- [服务端核心](../../core/server/) — WebSocket、任务处理、worker 与引擎管理。
- [Proxy 核心](../../core/proxy/) — 后端连接、健康状态与任务路由。
- [SDK](../../sdk/) — Python 文件转录 API 和命令行。
- [模型支持](../reference/models.md) — 引擎与辅助模型列表。
- [部署说明](../../deploy/README.md) — 依赖安装、进程托管、更新与回滚。
