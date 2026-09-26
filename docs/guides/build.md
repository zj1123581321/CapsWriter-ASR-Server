## 部署与运行

仓库以源码方式运行 ASR 服务端和 proxy。部署依赖、服务托管、更新与回滚步骤见 [部署维护与升级](../../deploy/README.md)。

- 服务端入口：`python start_server.py`
- proxy 入口：`python start_proxy.py`
- 平台依赖清单：`requirements-server-macos.txt`、`requirements-server-linux.txt`、`requirements-server.txt`
- 模型文件下载与目录结构：[模型下载说明](模型下载的若干问题.md)
