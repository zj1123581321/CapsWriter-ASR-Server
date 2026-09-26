# onboarding-verdict — PR30 无凭据质量入口与开发 workspace 独立审查

- 审查对象（H0 冻结）：`3baee635f89fd6480d5dc726bcf7ec3943fb4b0f..fc4568052975963cbce457c33615dc5720f8ee0f`，12 个文件全为新增。
- risk-tier: internal；独立 reviewer（dispatch dlg-20260926-135031-f1c8f7），未读实现报告/他人意见；源码只读，未重跑全量套件。
- 结论：**pass**（无 P1/P2；P3 七条均不阻塞）。

failure-visibility: clean

## OCR 前置扫描状态

`reviewed`（profile minimax / MiniMax-M3，cli_status=complete，coverage=complete，22 条 finding，子校验 16 confirmed / 2 refuted / 4 unverifiable）。envelope 与 stderr 原件：delegate state 目录 `ocr-envelope.json` / `ocr-stderr.log`（31,286B / 99B）。2 条 high 与全部 medium 已逐条经本仓证据重判（见下），无一条在本仓定级超过 P3。

## 逐文件覆盖与证据

1. `scripts/gate-quality`：`set -euo pipefail`；cd 仓根；缺 ffmpeg/uv 各自 stderr+exit 1（fail-loud，实测断言锁死）；`exec uv run --no-project --python 3.12` + 七依赖（含 `websockets==15.0.1`）+ `python -m pytest tests/ -q`，与 `ci.yml` 依赖清单和 argv 逐项一致。脚本继承环境为设计明示（secret 隔离归平台 job 边界）。
2. `tests/test_gate_quality.py`：真实子进程 + 替身 uv 落盘 argv/PATH/cwd；断言 cwd=仓根、argv 精确相等、exit 7 传播、缺依赖 fail-loud。本机 `uv run --no-project … pytest tests/test_gate_quality.py` 实测 **4 passed**。
3. `pyproject.toml`：`package=false`、`dependencies=[]`、dev 组恰为 CI 七依赖；非发行元数据属实。
4. `uv.lock`：`uv lock --check --python 3.12` rc=0；virtual source；仅 dev 依赖及传递依赖，无服务/模型依赖。
5. `.devcontainer/devcontainer.json`：python:3.12；uv feature digest 经 ghcr 匿名 API 实测存在（HTTP 200）；postCreateCommand 缺 ffmpeg 才装，`&&` 链失败即整体失败；无凭据挂载。
6. `.github/workflows/gate.yml`：callee `gate-v2.yml@v2`（v2=08a3baa，与 PR251 merge commit 一致，已核 API）声明 tier/runner/has_ui/design_doc 及三个 optional secret（FEISHU_CI_WEBHOOK/SILO_ACCESS_KEY/SILO_SECRET_KEY），caller 具名透传全部合法；permissions 与官方模板一致。未带模板 paths-ignore（见 P3-1）。`@v2` 为机群现行惯例（agent-config 与 gate-hub 的 gate/shadow/disposition 全部 `@v2`，已核），模板的 "pin SHA" 注释与机群实践脱节属上游文档问题。
7. `.github/workflows/gate-shadow.yml`：与模板逐键一致（含 paths-ignore）；callee 仅声明 tier/runner/design_doc 等输入、无 secrets 块，caller 不传 secrets 正确。
8. `.github/workflows/gate-disposition.yml`：9 个 dispatch 输入与 callee @v2 声明逐名一致（gate_ref 为 callee 标注的废弃兼容输入，不转发正确）；具名 SILO pair、不用 inherit——公开仓边界的刻意收紧。callee @v2 的 `workflow_call` 下**确无** secrets 声明（已核原文）：v2 推广前手动 dispatch 会在校验阶段即响亮失败、零副作用；PR 保持 draft、文档明示不得启用，属已登记的分阶段依赖，不判缺失。
9. `design.md`：事实核验一致——PR251 merged、v2=08a3baa、disposition callee 未声明 SILO secret（均实测）；未完成项（fork/Dependabot 矩阵、runner rollout、capacity 三条真实 producer SHA、PR1145）如实登记。
10. `onboarding-progress.md`：本机跑与容器跑分行记录、不互相替代；首次无 .git 失败保留；临时快照 859c154 明示非 PR SHA；基线 head 229710c 与提交栈一致。"PR252 正在独立审查" 写于合并前 7 分钟（fc45680 13:47Z < merge 13:54Z），操作性声明（v2 未推广）在审查时点仍为真。
11. `quality-entry-progress.md`：228→230 数字与新增 4 条契约自洽；`git diff --check base..HEAD` 实测干净。
12. `docs/testing.md`：与 pyproject/devcontainer 一致；"本次验证"限定词未把隔离副本外推成普遍承诺。

## P1 两问（对最高风险候选）

- 脚本继承环境（OCR high#4）：真实使用会触发吗？会（exec 继承）。后果可接受吗？可接受——唯一平台消费方是 gate-v2 quality job，其 step env 仅 `GATE_ARTIFACT_DIR`、job 级无 SILO/FEISHU，且跑在无凭据 ci 池（runs-on `[self-hosted,linux,ci]`，"quality 走无凭据 ci 池"，SILO 仅在 primary job env——均已核 @v2 原文）；本地运行继承的是用户本人环境。→ 非 P1。
- disposition caller 引用了 callee 尚未声明的 secret：会触发吗？仅 v2 推广前有人手动 dispatch；结果是工作流校验即失败、响亮且无写入。→ 非 P1。

## P2

无。

## P3（记 backlog，不阻塞）

1. gate.yml 未带模板 paths-ignore：docs-only PR 也烧自托管门禁资源；方向保守（更多评审），未记录是否刻意。
2. 依赖清单三处重复（`--with` / 测试 PKGS / pyproject dev 组）无交叉校验；漂移表现为测试失败，fail-loud，仅维护性。
3. `_assert_clean` 哨兵断言按现状不可达（env 替换后哨兵不进子进程，替身也先 exit 99 不写 record）；真实约束力仅剩 fixture 退回继承环境时的 exit 99 一道；设计未宣称环境过滤，无虚假声明。
4. 测试 `shutil.which("bash"/"dirname")` 未防空：极端环境报 TypeError 而非 skip。
5. devcontainer 基础镜像浮标签 `python:3.12`（uv feature 已按 digest 钉，镜像未钉）。
6. `docs/testing.md` 与存量 `docs/development/testing.md` 主题重叠；后者命令中 websockets 未钉版为存量问题，记 backlog。
7. dispatch 输入透传后的消费安全属上游 gate 边界（dispatch 需写权限），不在本仓可改范围。

## Unknown / 待验证（设计已登记，复核确认仍存在）

- fork/Dependabot/draft→ready 矩阵、生产 runner rollout 未完成；capacity allowlist 须待 caller 进默认分支后取三条真实 producer SHA 关闭，当前无预填 hash（已核仓内无 allowlist 写入）。
- 公开仓 Silo repository secret 凭据核验未完成；上游 PR252 已合并（bb443ef）但 v2 仍指 08a3baa，disposition 启用时点取决于 canary 推广，标 ready 前须重核 v2。
- 全量 230 passed 为进度文档声明，本审查按卡未重跑；仅实测 4 条新契约测试通过。

OCR 状态：reviewed（非 skipped）；本 verdict 为其 22 条 finding 的逐条重判收口。
