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

SDK 将音频文件转换为选定编码并分块上传；自定义下游可直接实现 [协议](protocol.md)。服务端错误、连接终态、编码能力和版本兼容规则以协议文档为准。
