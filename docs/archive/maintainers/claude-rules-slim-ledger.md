# CLAUDE.md 规则瘦身逐条核销表（初稿）

- 基准文件：仓根 `CLAUDE.md`（19074B，HEAD `5d18fba`）
- 配方：`/home/zlx/projects/personal/agent-config/docs/guides/rules-budget.md` 三问准入
  1. 这条每个会话都需要吗？
  2. 能从代码 / README / git log 推断吗？
  3. 现在还成立吗？
- 不全过则不准留在仓根规则文件。处置只能是：保留 / 搬 docs 留指针 / 删（删必须写明被哪个文档或事实覆盖）。
- 对账：原文件 `grep -c '^#'` 标题 20 条（H1=1，H2=9，H3=10）；本表标题行 20 条，另加 `risk-tier` 声明行 1 条（卡面硬性要求原样保留，虽非标题）。

| # | 原条目 | 级 | 处置 | 落点 | 理由 |
|---|--------|----|------|------|------|
| 0 | `risk-tier: internal` | 声明行 | 保留 | `AGENTS.md` 标题下原样一行 | 卡面锁定：已有 risk-tier 声明行必须原样保留。三问①每个会话的风险分档需要；②不能从目录树推断；③仍成立。 |
| 1 | `# CapsWriter-Offline 开发指南` | H1 | 保留 | `AGENTS.md` 标题 | 仓根规则文件需要文档名。三问①需要；②非推断项；③仍成立。 |
| 2 | `## 核心设计 (Core Design)` | H2 | 保留 | `AGENTS.md` 正文原样 | ①每个会话都要守「快、准、稳、离线」、C/S 子进程推理、配置在根目录，否则会把推理塞进 WS 主进程或改走云端。②设计意图不能单靠读代码一次就抓住。③仍成立。只做原样保留，不改语义。 |
| 3 | `## 架构细节与流程 (Architecture & Workflows)` | H2 | 搬 docs 留指针 | `docs/architecture-workflows.md`（本节无独立正文，作子条容器标题） | ①不是每个会话都要走完识别/热词/代理全流程。②细节可从 `core/` 推断。③仍成立，故搬不删。仓根只留一行指针。 |
| 4 | `### 1. 识别全链路 (Recognition Flow)` | H3 | 搬 docs 留指针 | `docs/architecture-workflows.md` | ①听写/切片/双路合并不是每个会话的硬约束。②`segmenter.py`、`merger/`、客户端后处理可推断。③仍成立。 |
| 5 | `### 2. 客户端模式 (Client Modes)` | H3 | 搬 docs 留指针 | `docs/architecture-workflows.md` | ①仅改听写/转录路径时才需要。②`MicRunner` / `FileTranscriber` 可推断。③仍成立。 |
| 6 | `### 3. LLM Agent & 智能修正` | H3 | 搬 docs 留指针 | `docs/architecture-workflows.md` | ①仅改 LLM 角色/热词热重载时才需要。②`core/client/llm/`、`LLM/*.py` 可推断。③仍成立。 |
| 7 | `### 4. 热词系统 (Hotword System)` | H3 | 搬 docs 留指针 | `docs/architecture-workflows.md` | ①仅改热词时才需要。②`core/client/hotword/`、`hot.txt` 可推断；用户手册已有 `docs/热词功能如何使用.md`。③仍成立。正文仍原样搬，不因已有用户手册而删。 |
| 8 | `### 5. 历史归档 (Diary)` | H3 | 搬 docs 留指针 | `docs/architecture-workflows.md` | ①不是每个会话都碰日记。②`core/client/diary/diary_writer.py` 可推断。③仍成立。 |
| 9 | `### 6. UDP 广播与控制` | H3 | 搬 docs 留指针 | `docs/architecture-workflows.md` | ①仅改 UDP 时才需要。②`core/client/udp/`、`config_client.py` 可推断。③仍成立。 |
| 10 | `### 7. ASR 负载均衡代理 (Proxy)` | H3 | 搬 docs 留指针 | `docs/architecture-workflows.md` | ①仅改代理时才需要。②`core/proxy/` 可推断；设计文已有 `docs/designs/proxy-concurrent-routing.md` 与 `docs/消费方并发改造指南.md`。③仍成立。正文原样搬（含 EWMA/探活数字），不按「已被设计文覆盖」删除，以免抽样对不上。 |
| 11 | `## 关键路径 (Key Paths)` | H2 | 搬 docs 留指针 | `docs/key-paths.md` | ①文件树导航不是每个会话的硬约束。②可由目录与模块名推断。③仍成立。 |
| 12 | `## 打包与部署 (Build)` | H2 | 搬 docs 留指针 | `docs/build.md` | ①仅打包时才需要。②`build.spec` / `build-client.spec` 可推断。③仍成立。 |
| 13 | `## 测试 (Testing)` | H2 | 搬 docs 留指针 | `docs/testing.md` | ①仅写测试或跑验证脚本时才需要。②`tests/`、`scripts/_verify_*.py` 可推断。③仍成立。`.gitignore` 的 `test_*.py` 例外等陷阱随正文原样搬走。 |
| 14 | `## 模型支持 (Models)` | H2 | 搬 docs 留指针 | `docs/models.md`（容器标题） | ①不是每个会话都换引擎。②`core/server/engines/`、`config_server.py` 可推断。③仍成立。 |
| 15 | `### ASR 引擎` | H3 | 搬 docs 留指针 | `docs/models.md` | ①仅加/换 ASR 引擎时才需要。②引擎目录与 `factory.py` 可推断。③仍成立。含 `qwen_asr_mlx` 长说明与「新增引擎必须改 `check_model.py`」——该约束不是每个会话都触发，随本节搬走。 |
| 16 | `### 辅助模型` | H3 | 搬 docs 留指针 | `docs/models.md` | ①仅文件转录/aligner/标点时才需要。②`ModelLoader` 与 `engines/llama/` 可推断。③仍成立（含 macOS 启用步骤、RTF、内存口径）。 |
| 17 | `### 引擎能力检测` | H3 | 搬 docs 留指针 | `docs/models.md` | ①仅改引擎能力标志时才需要。②`EngineCapabilities` / `ModelLoader` 可推断。③仍成立。 |
| 18 | `## LLM 提供商支持 (LLM Providers)` | H2 | 搬 docs 留指针 | `docs/llm-providers.md` | ①仅接新 LLM 提供商时才需要。②`LLM/*.py` 与客户端 LLM 适配可推断。③仍成立。 |
| 19 | `## 数据流 (Data Flow)` | H2 | 搬 docs 留指针 | `docs/data-flow.md` | ①ASCII 全链路不是每个会话的硬约束。②可由代码走读推断。③仍成立。 |
| 20 | `## 用户偏好 (User Preferences)` | H2 | 保留 | `AGENTS.md` 正文原样 | ①每个会话都要用中文、走 conda 环境 `c`、临时 Python 先落文件再跑。②偏好无法从代码推断。③仍成立。 |

## 删除清单

无。本卡不把任何标题标为「删」：即使与已有 `docs/` 用户手册或设计文重叠，也原样搬走，避免信息丢失与抽样对不上。

## 拟建承接文档（初稿，粒度可在搬移提交调整）

| 文件 | 承接原条目 |
|------|------------|
| `docs/architecture-workflows.md` | H2 架构细节与流程 + H3 识别全链路 / 客户端模式 / LLM Agent / 热词 / 日记 / UDP / 代理 |
| `docs/key-paths.md` | H2 关键路径 |
| `docs/build.md` | H2 打包与部署 |
| `docs/testing.md` | H2 测试 |
| `docs/models.md` | H2 模型支持 + H3 ASR 引擎 / 辅助模型 / 引擎能力检测 |
| `docs/llm-providers.md` | H2 LLM 提供商支持 |
| `docs/data-flow.md` | H2 数据流 |

终态：`AGENTS.md` 为唯一真身（≤8192B），保留条目 0/1/2/20 原样，其余 H2 在 `AGENTS.md` 只留一行指针；`CLAUDE.md` 内容恰为一行 `@AGENTS.md`。
