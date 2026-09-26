# 文档目录与维护入口设计

## 目标与边界

仓库面向离线语音识别服务端、可选 proxy 和 Python SDK。访客先从根目录 README 找到首次部署、SDK、WebSocket 协议；开发、维护和历史材料按读者任务分开。本文记录本次迁移决策，后续正式设计放在 `docs/development/designs/`，过程评审记录放在 `docs/archive/`。

本次只整理文档、链接和文档路径注释。SDK 用法以 `sdk/README.md` 为准，部署升级以 `deploy/README.md` 为准，wire contract 以 `docs/reference/protocol.md` 为准。分类导航在 `docs/README.md`；根 `readme.md` 继续提供面向首次访问者的入口。历史溯源材料保留，不作为当前行为承诺。

## 分类规则

- `guides/`：部署起步、模型选择、客户端接入等按任务操作的说明。
- `reference/`：协议和模型能力等需要查阅的契约资料。
- `development/`：架构、数据流、测试、算法及正式设计。
- `maintainers/`：运维记录与项目维护知识。
- `archive/`：旧客户端材料、历史 sessions、研究计划、旧账本和报告；归档入口说明其时效性。

SDK 与部署细节留在各自目录的 README，文档迁移不复制这两份维护手册，也不建立旧路径兼容页。下游指南只保留路线选择和最小 WebSocket 接入流程；字段表及错误码不在多处维护。原 changelog 完整保留在归档，新根 changelog 只记录可由当前代码或 Git 历史核实的独立服务阶段变化。

## 56 份原 tracked 文档迁移表

| 原路径 | 新路径 |
| --- | --- |
| `docs/sessions/**`（32 份，含旧设计与评审记录） | `docs/archive/sessions/**` |
| `docs/2026-08-28-claude-rules-slim-ledger.md` | `docs/archive/maintainers/claude-rules-slim-ledger.md` |
| `docs/ASR负载均衡代理.md` | `docs/guides/ASR负载均衡代理.md` |
| `docs/CHANGELOG.md` | `docs/archive/legacy-client/CHANGELOG.md` |
| `docs/Python 正则表达式.md` | `docs/archive/legacy-client/Python 正则表达式.md` |
| `docs/architecture-workflows.md` | `docs/development/architecture-workflows.md` |
| `docs/build.md` | `docs/guides/build.md` |
| `docs/data-flow.md` | `docs/development/data-flow.md` |
| `docs/designs/proxy-concurrent-routing.md` | `docs/development/designs/proxy-concurrent-routing.md` |
| `docs/getting-started.md` | `docs/guides/getting-started.md` |
| `docs/key-paths.md` | `docs/development/key-paths.md` |
| `docs/lixing/mlx-qwen-integration-plan.md` | `docs/archive/research/mlx-qwen-integration-plan.md` |
| `docs/models.md` | `docs/reference/models.md` |
| `docs/operations.md` | `docs/maintainers/operations.md` |
| `docs/project-memory.md` | `docs/maintainers/project-memory.md` |
| `docs/protocol.md` | `docs/reference/protocol.md` |
| `docs/reports/memory-fleet-import.md` | `docs/archive/reports/memory-fleet-import.md` |
| `docs/testing.md` | `docs/development/testing.md` |
| `docs/text_merge_algorithm.md` | `docs/archive/legacy-client/text_merge_algorithm.md` |
| `docs/下游客户端接入指南.md` | `docs/guides/下游客户端接入指南.md` |
| `docs/显卡加速的若干问题.md` | `docs/guides/显卡加速的若干问题.md` |
| `docs/模型下载的若干问题.md` | `docs/guides/模型下载的若干问题.md` |
| `docs/消费方并发改造指南.md` | `docs/guides/消费方并发改造指南.md` |
| `docs/热词功能如何使用.md` | `docs/guides/热词功能如何使用.md` |
| `docs/识别语言如何配置.md` | `docs/guides/识别语言如何配置.md` |

旧算法说明保留原文供溯源；现行 `merge_by_text` 行为另见 `docs/development/algorithms/text-merge.md`。tracked 文档以外的 ignored 本机研究资料不属于迁移表或提交范围。

## 维护约定

迁移只更新当前仍可点击的相对链接和仓内文档路径引用，不对历史快照里的命令、SHA、旧绝对路径做全局替换。外部历史链接不作网页存活承诺。新增维护说明前先检查是否已有权威入口，避免重复维护同一组字段或配置。
