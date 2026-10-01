## 服务端数据流

```text
Python SDK 或其他下游
        |
        | WebSocket 音频帧（proxy 可选）
        v
ASR Proxy（按任务选择后端）
        |
        v
ASR Server 主进程
  接收与校验 -> 解码 -> 分段 -> 有界任务队列
                                  |
                                  v
                         Worker 子进程
                         ASR / 标点 / 对齐
                                  |
                                  v
ASR Server 主进程
  合并文本与时间戳 -> 结果帧 / 错误帧 -> WebSocket 下游
```

SDK 将音频文件转换为选定编码并分块上传；自定义下游可直接实现 [协议](../reference/protocol.md)。服务端错误、连接终态、编码能力和版本兼容规则以协议文档为准。

## 未来 HTTP 文件任务边界

HTTP 文件任务是显式启用的并行路径，当前版本不监听 HTTP 文件入口：

```text
HTTP 客户端 -> (未来)受限落盘/续传确认 -> 稳定 job owner
                                      |
                                      v
                           共享有界 PCM 分段器
                                      |
                  owner_kind=http, socket_id="" 的 Task
                                      |
                                      v
                              既有 Worker Queue
                                      |
                  Result -> 注入的 HTTP 持久 sink（E3）
```

WS 仍沿用连接生命周期；它与 HTTP 只共享 PCM 段、offset、overlap、段长上限和有限提交，不共享连接取消或持久确认。两条路径通过 `(owner_kind, derived_owner_id, task_id)` 隔离：WS owner 从 `socket_id` 派生，HTTP owner 从稳定 `task_id` 派生。HTTP 任务必须存在于跨进程 `active_http_jobs` 集合，否则 worker 不继续推理。
