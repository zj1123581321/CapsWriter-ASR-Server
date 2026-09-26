# 文档导航

从不同任务进入对应的维护入口。首次访问可先看[仓库首页](../readme.md)；文档分类与迁移规则见[目录设计记录](development/designs/documentation-layout.md)。

## 部署与接入

- [首次部署与第一次识别](guides/getting-started.md)
- [部署维护与升级](../deploy/README.md)
- [Python SDK 安装与文件转录](../sdk/README.md)
- [下游应用接入路线与最小 WebSocket 示例](guides/下游客户端接入指南.md)
- [ASR 负载均衡代理](guides/ASR负载均衡代理.md)
- [服务端构建与运行入口](guides/build.md)
- [模型支持](reference/models.md) · [模型下载与目录](guides/模型下载的若干问题.md)
- [服务端热词配置](guides/热词功能如何使用.md) · [识别语言配置](guides/识别语言如何配置.md)
- [GPU 与推理后端配置](guides/显卡加速的若干问题.md)
- [消费方并发改造指南](guides/消费方并发改造指南.md)

## 协议参考

- [WebSocket 服务协议](reference/protocol.md)：消息字段、编码、错误码和健康状态的权威说明。

## 开发

- [架构与工作流](development/architecture-workflows.md)
- [数据流](development/data-flow.md)
- [关键路径](development/key-paths.md)
- [测试说明](development/testing.md)
- [文本拼接算法](development/algorithms/text-merge.md)
- [Proxy 并发路由设计](development/designs/proxy-concurrent-routing.md)
- [文档目录设计记录](development/designs/documentation-layout.md)

## 维护

- [运维记录](maintainers/operations.md)
- [项目维护记忆](maintainers/project-memory.md)
- [部署与升级](../deploy/README.md)

## 历史资料

- [归档说明与历史资料入口](archive/README.md)
- [上游关系与同步方式](../UPSTREAM.md)
