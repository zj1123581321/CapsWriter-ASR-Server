# 公开仓统一门禁接入设计

## 目标与边界

CapsWriter 公开仓提供可复现的 Python 开发测试环境，并通过 gate、shadow、disposition 三个 caller 接入 gate v2。质量入口执行仓内完整 `tests/`；PR #30 保持 draft，待独立审查和公开仓凭据核验后再决定 ready。此卡不改 gate 实现、平台 registry、生产依赖或部署配置。

E 卡只负责无凭据质量入口，因此当时否决根 `pyproject.toml` 是对该入口消费边界的限定。F 卡明确增加开发 workspace 元数据和测试依赖；`scripts/gate-quality` 继续使用 `--no-project`，不消费根项目清单，服务依赖也不迁入开发依赖。

## 配置与信任边界

Caller 使用 gate 仓官方模板的结构和移动 `@v2`：`tier=internal`、`runner=self`、`has_ui=false`、空 `design_doc`。公开仓只具名转发 repository secret；不继承全部组织或仓库 secret。质量 job 的 secret 隔离由 gate 的 job 边界承担，本地入口不实现环境过滤。

根 `pyproject.toml` 仅声明空项目依赖和七项测试开发依赖，`uv.lock` 由 uv 生成；服务端、模型、平台依赖仍留在部署路径。Dev Container 基于 Python 3.12 和固定 uv feature，只在本仓配置中补足缺失的 ffmpeg，再同步开发依赖。

## 不变式

1. `scripts/gate-quality` 从仓根运行 `uv run --no-project --python 3.12`，固定 CI 同款七项依赖与完整 pytest argv；`tests/test_gate_quality.py` 的真实子进程断言 `cwd=ROOT`、argv 和退出码。
2. 开发 workspace 的 `uv sync` / `uv run python -m pytest` 使用锁文件中的测试组，不锁服务或模型依赖；质量入口仍与该 workspace 解耦。
3. 质量入口测试 fixture 只向替身写入 argv、PATH、cwd 白名单，不序列化完整环境；受控子进程环境不证明入口会剥夺 secret，secret 边界由平台 job 实现。
4. Public caller 显式传递 Silo 凭据，不使用 `secrets: inherit`。当前 gate disposition v2 尚未在 `workflow_call` 声明两个可选 Silo secret；其 caller 在上游契约合并前不得启用。

## 已核事实与未完成项

A 卡 PR #251 已合并；GitHub API 当前显示 `v2` tag 指向 `08a3baa16650e314f05d4e3aea9ec3631cad3760`，由自动 canary 推广，本卡不手动改 tag 或 ACL。独立公开仓 Silo repository secret 尚未完成凭据核验；gate disposition secret 契约等待上游卡。

仍待验证主审是否执行 PR 代码、共享 runner/cache 隔离、公开仓 external fork/Dependabot/draft 转 ready 矩阵及完整接入容量门禁。Caps caller 进入默认分支后，须从真实 producer 取三条 SHA，补 gate-hub allowlist 并移除临时排除；不得用 tag 或预填 hash 代替消费证据。runner UV 边界 PR #1145 仍未部署。
