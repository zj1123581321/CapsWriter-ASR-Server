## 架构与工作流

### 组件

- **ASR 服务端**：`start_server.py` 创建 WebSocket 服务。主进程处理连接、任务状态和网络收发；识别模型运行在独立子进程，避免推理占用网络事件循环。
- **Proxy**：`start_proxy.py` 接受下游连接，并按任务把音频转发到配置的 ASR 后端。Proxy 不加载识别模型。
- **Python SDK**：`sdk/` 为下游应用提供文件转录 API 和命令行入口；其他语言也可按 [协议](../reference/protocol.md) 接入。

### 服务端识别流程

1. WebSocket 接收音频帧并校验任务参数。
2. 音频解码器将支持的编码转换为单声道 16 kHz PCM。
3. 分段器按任务参数和静音切点切分音频，并把有界任务放入 worker 队列。
4. worker 子进程调用配置的 ASR、标点或对齐引擎。
5. 服务端合并各段文本与时间戳，生成结果帧并通过原连接返回。

### HTTP 文件任务（后续增量，当前关闭）

HTTP 文件任务不会复用 WebSocket 假连接，也不会把完整文件交给 worker。未来显式启用时，接收/持久化层先确认受理，再由文件 runner 将输入解码为有限 PCM 段；共享 `core/server/segmenter.py` 产生带 offset、overlap 和 `is_final` 的段，真实 `Task` 以 `owner_kind=http`、空 `socket_id` 和稳定 job ID 进入既有 Queue。`active_http_jobs` 是主/子进程共享的运行门控，父进程通过注入 sink 持久化 Result；E1 只提供接线，不开放 HTTP listener/store/runner。

WS 与 HTTP 的共享边界止于有界 PCM 段和有限提交。WS 的 socket 取消、出站队列和断线清理，HTTP 的持久 ACK、查询、领取和源文件清理各自保持独立。

### Proxy 路由流程

Proxy 为每个任务选择健康且协议兼容的后端，并保持任务期间的连接关联。路由策略、健康检查和限制见 [ASR 负载均衡代理](../guides/ASR负载均衡代理.md) 与 [proxy 路由设计](designs/proxy-concurrent-routing.md)。

### 模型与热词

引擎由 `CW_MODEL_TYPE` 选择，模型路径和后端参数集中在 `config_server.py`。部分模型支持读取根目录的 `hot-server.txt`；可用能力见 [模型支持](../reference/models.md)。
