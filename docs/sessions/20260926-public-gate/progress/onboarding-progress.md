# 公开仓门禁接入进度

## 已核基线

- PR #30 `feat: 增加无凭据质量入口 scripts/gate-quality` 仍为 draft；本卡以 head `229710c33b3da89bc560f00eab0c63de607ee1f4` 为基线，默认分支为 `master`。
- A 卡 PR #251 已合并；GitHub API 当前核得 gate `v2` tag 指向 `08a3baa16650e314f05d4e3aea9ec3631cad3760`，自动 canary 推广已发生。
- 独立公开仓 Silo 凭据尚未完成核验；runner UV 边界 PR #1145 尚未部署。

## 本卡范围

增加根目录开发 workspace、锁文件、Python 3.12 Dev Container 和测试说明；增加 gate、shadow、disposition caller。质量入口保留 `--no-project`，既有 `.github/workflows/ci.yml` 双 websockets 矩阵原样保留。

Disposition caller 仅具名映射两个 Silo repository secret；当前 v2 callee 尚未声明 `workflow_call.secrets`。上游契约 PR #252 已实现、正在独立审查，合并并推广前不得启用此 caller。公开凭据不写入仓库或文档。

## 验证与关闭条件

官方 Dev Container CLI 0.89.0 实际创建并进入配置容器；容器内确认 Python 3.12、uv、ffmpeg 和 `.venv`，`postCreateCommand` 的 `uv sync` 已完成。`uv lock --check --python 3.12`、三份 workflow 的 actionlint、caller/开发依赖契约检查及坏 tier 负向配置均通过。

首次全套测试在不挂 Git 元数据的临时副本运行，结果为 229 passed、3 skipped、1 failed；唯一失败是健康冻结用例在 setup 执行 `git worktree add` 时因没有 `.git` 退出 128，未进入断言。随后在无凭据副本中按显式路径建立本地 Git 快照，基于 PR #30 源码 `229710c33b3da89bc560f00eab0c63de607ee1f4` 和本卡配置，排除与运行无关的 `retro/acceptance-log.jsonl`；git-dir/common-dir 均位于副本内、无 remote，未纳入 `.env*`、`.venv`、认证挂载或 `retro/consult*`。该临时快照提交短 SHA `859c154` 仅为验证环境，不是 PR SHA。健康冻结用例单测 1 passed；随后同一 Dev Container 中 `uv run python -m pytest tests/ -q` 为 230 passed、3 skipped、81 warnings，耗时 93.39 秒。失败首跑仍保留。完整输出在执行机 0600 文件 `/tmp/caps-public-gate-uv-tests-git-context-20260926.log`。

PR 继续 draft，等待本仓独立审查和公开仓 Silo 凭据核验。

尚未完成的信任隔离、fork/Dependabot/draft 矩阵和容量门禁保持为开放项。Caps caller 合并到 master 后，从实际 default-branch producer 取三条 SHA，补正式 capacity allowlist 并移除 D 卡临时排除，才算完成平台容量登记。
