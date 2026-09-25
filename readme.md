# CapsWriter ASR Server

CapsWriter ASR Server 是供局域网下游应用调用的离线语音识别服务，包含 WebSocket 服务端、可选负载均衡 proxy 和 Python SDK。

## 部署

从源码运行服务端：

```sh
python start_server.py
```

服务端默认监听 `0.0.0.0:6016`。模型、网络、日志及并发参数通过 `config_server.py` 和 `CW_*` 环境变量配置。依赖安装、PM2、Windows 计划任务与升级步骤见 [部署指南](deploy/README.md)。

需要将多个服务端实例统一接入时，运行 proxy：

```sh
python start_proxy.py
```

proxy 配置见 [config_proxy.py](config_proxy.py)，多后端路由、健康检查与状态查询见 [ASR 负载均衡代理](docs/ASR负载均衡代理.md)。

## 下游接入

- [Python SDK 安装与调用](sdk/README.md)
- [WebSocket 协议与消息契约](docs/protocol.md)
- [下游接入示例与时间戳说明](docs/下游客户端接入指南.md)

## 与上游的关系

本项目基于上游 CapsWriter 并持续同步服务端与识别引擎改动，分叉范围及同步方式见 [UPSTREAM.md](UPSTREAM.md)。

## 许可证

本项目使用 [MIT 许可证](LICENSE)。
