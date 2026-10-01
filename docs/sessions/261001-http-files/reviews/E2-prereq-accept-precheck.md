<!-- delegate-outcome: succeeded -->
## 验收结果

本验证任务已完成：三个指定 dispatch 各调用官方 `accept_precheck.py` 一次，三份输出均可解析为 schema v2 JSON。schema 的 producer SHA 字段名为 `head_sha`（没有字面 `sourceSha` 字段）。源派卡的验收状态与本验证任务是否完成分开记录：实现 dispatch 的官方预检为 `green`；两份 review dispatch 为官方 `not_applicable[verdict_only]`，是非 green 终态，不是通过，也不是业务失败。本文件是本分支唯一新增仓库路径；包含精确私有文件路径的完整报告写在本 dispatch 的私有 `report.md`。

真正读取这些 JSON 的账务消费者是官方 `scripts/retro/log_acceptance.py` 调用的 `scripts/retro/acceptance_evidence.py`，后者再调用 `scripts/retro/acceptance_precheck_gate.py::evaluate_precheck_gate`。该闸读取对应 dispatch 目录的 `accept_precheck.json`，要求 schema v2、`status == "green"`，再核验 `commit_set` / `content_fingerprint` 身份及主干新鲜度；`not_applicable` 会被拒绝。本任务只生成元数据，没有调用 `log_acceptance.py`、写 acceptance ledger、记录 waiver、发布或合并。

### 主干对象与真实 Verify 消费方式

官方命令均传入当前独立 worktree 的 `--repo-path "$PWD"`。脚本从 git common dir 找到其主仓 checkout，但不以该 checkout 的工作树 `HEAD=79c39363af9c58c6404589f5905079cd46e1ef7f` 作消费基线：本地 `refs/remotes/origin/HEAD` 实际指向 `origin/master`，该真实 remote-tracking ref 的 SHA 是 `9e87c544070737b12269c76268f5d2376fad9f22`。`origin/main` 在本地不存在。官方 `resolve_trunk_ref` 优先使用可解析的 `origin/HEAD`，因此三份 JSON 的 `main_sha_at_check` 都是卡面要求的合并后 `9e87c544…`；没有 fetch、伪造 `origin/main` 或改写主干 ref。

实现卡的 `Verify-Mode` 虽写 `ci-standard-v1`，但命令不是官方识别的两条标准命令之一，所以官方工具没有把它当 CI 证据：它从源 tip 建立临时 detached worktree，合并上述 `9e87c544…`，然后通过 `bash -c <卡面 Verify-Command>` 在该合并树运行。实际 Verify 继承了本次显式设置的独立 `UV_CACHE_DIR` 与 `TMPDIR`。官方在 Verify 完成后清理临时 worktree。两份 review 卡被 verdict-only 分类提前终止，JSON 中 `command` 为空、`exit_code` 为 null，故没有实际 doc-existence Verify 结果。

### 三个官方结果

| Dispatch | 只检查执行者产物的范围 | 结果与 producer SHA (`head_sha`) | 官方 JSON |
|---|---|---|---|
| `dlg-20261001-134229-956261` | `2548e03629d8a40c6411c0696901c96fda909793..b11659cc3afec1bee85b4ee82953a24b31dfe77b` | 官方 CLI 退出码 0，顶层 `green`；`scope=green`，5 个变更文件匹配卡面 glob；`verify_on_merged_main=green`，退出码 0；`head_sha=b11659cc3afec1bee85b4ee82953a24b31dfe77b`、`identity_source=commit-range`、`commit_set=[b11659…]`、`content_fingerprint=""`。JSON 1,411 字节，SHA-256 `328e36321e421cfe08077cb671541678820f8e79761be6e4ddbec721a2f42f3e`。 | `<delegate-state-root>/20261001-134237-quick-cursor-http-e2-prereq-plan-261001/accept_precheck.json` |
| `dlg-20261001-141712-32a9d1` | `b11659cc3afec1bee85b4ee82953a24b31dfe77b..42a9b2e5c6cbde2de2b4877a28156cd99ee81c2f` | 官方 CLI 退出码 2，顶层 `not_applicable`，类别 `verdict_only`；`head_sha=42a9b2e5c6cbde2de2b4877a28156cd99ee81c2f`，唯一产物 `E2-prereq-review-verdict.md`，`commit_set=[42a9b2…]`、`content_fingerprint=a698fd9530c2ec3e968ca9a36ff33d58a80c32b2`。JSON 2,019 字节，SHA-256 `f773859de801d0712697f7df429a424193539047debfc1961a1664eaef848242`。 | `<delegate-state-root>/20261001-141721-big-codex-CapsWriter-Offline-with-AI/accept_precheck.json` |
| `dlg-20261001-144933-6a0c60` | `e57411d345d2405bd4fcbbfe22ac59c72ab18e4c..70c2ead63e0610abaaac7f753003934cc9cde6db` | 官方 CLI 退出码 2，顶层 `not_applicable`，类别 `verdict_only`；`head_sha=70c2ead63e0610abaaac7f753003934cc9cde6db`，唯一产物 `E2-prereq-combination-verdict.md`，`commit_set=[70c2ea…]`、`content_fingerprint=03247a80aa842df2e03e7ab2b11a6957e06ea2b3`。JSON 2,044 字节，SHA-256 `d816248c4ed8dd567814d4125e19999a26e54efc5ca2ea57e72874539ae78e70`。 | `<delegate-state-root>/20261001-144941-big-codex-CapsWriter-Offline-with-AI/accept_precheck.json` |

实际调用（各一次；工作目录为本 worktree；三个 Verify 总预算均为 600 秒、单步 Verify 上限 360 秒）：

```sh
UV_CACHE_DIR=<isolated-cache-for-134229> TMPDIR=<isolated-tmp-for-134229> python3 <agent-config-runtime>/current/scripts/delegate/accept_precheck.py --dispatch-id dlg-20261001-134229-956261 --repo-path "$PWD" --timeout-sec 600 --verify-timeout-sec 360 --commit-range 2548e03629d8a40c6411c0696901c96fda909793..b11659cc3afec1bee85b4ee82953a24b31dfe77b
UV_CACHE_DIR=<isolated-cache-for-141712> TMPDIR=<isolated-tmp-for-141712> python3 <agent-config-runtime>/current/scripts/delegate/accept_precheck.py --dispatch-id dlg-20261001-141712-32a9d1 --repo-path "$PWD" --timeout-sec 600 --verify-timeout-sec 360 --commit-range b11659cc3afec1bee85b4ee82953a24b31dfe77b..42a9b2e5c6cbde2de2b4877a28156cd99ee81c2f
UV_CACHE_DIR=<isolated-cache-for-144933> TMPDIR=<isolated-tmp-for-144933> python3 <agent-config-runtime>/current/scripts/delegate/accept_precheck.py --dispatch-id dlg-20261001-144933-6a0c60 --repo-path "$PWD" --timeout-sec 600 --verify-timeout-sec 360 --commit-range e57411d345d2405bd4fcbbfe22ac59c72ab18e4c..70c2ead63e0610abaaac7f753003934cc9cde6db
```

首个范围来自实现方最终报告的真实 base/head，且官方身份解析只得到 `b11659…` 这一个执行者提交；该提交改动的是三份 requirements、`.github/workflows/ci.yml` 与 `docs/development/testing.md` 五个卡面路径，合计 7 行。此前继承的计划提交及其他主干/合并父提交不属于本次执行者范围。两份审查范围分别从 verdict 提交的直接父提交开始：第一份父提交是 `b11659…`，第二份父提交是 merge commit `e57411d…`。这两个两点范围的 tip 各只含其自身 verdict 提交，第二份范围不把 merge 的其他父提交当作审查者修改。

第一份 JSON 的真实业务 Verify 命令是：

```sh
uv run --no-project --python 3.12 --with numpy --with rich --with websockets --with colorama --with pytest==9.1.1 --with soundfile --with pytest-asyncio==1.4.0 --with aiohttp==3.14.3 --with httpx==0.28.1 python -m pytest tests/ -q -p no:cacheprovider
```

结果 `299 passed, 3 skipped, 91 warnings in 113.34s`，`exit_code=0`。JSON 的 `checks.ci` 也是 `green`，但它仅供参考，并明确列出 draft 下 `gate / primary`、`gate / resolve_advisory`、`gate / ocr`、`gate / notify` 等 skipped；不能单凭该字段宣称完整主审通过。本卡没有重跑 cold review 或 fullgate。派发时主干 CI 基线 API 为 `gh api request failed`，故同作业名、首失败步骤的继承红标为未能判定；本次官方 Verify 无新红。

官方 scope 判据不是恒真：发布树自带的 `tests/delegate/test_accept_precheck.py::test_scope_red_exits_2_and_writes_json` 用 `forbidden.txt` 作为越界路径，断言 scope 与顶层均为 red 且 JSON 落盘；`tests/delegate/test_accept_precheck_not_applicable.py::test_verdict_only_is_not_applicable` 则断言 verdict-only 状态仍退出 2。本任务核读这两个既有自测契约，没有额外改动或运行测试套件。

### 两个 verdict 文档与 doc Verify 的因果

通过 Git 对象直接核对：两个指定 verdict 文件都不在官方消费主干 `9e87c544…` 中，但各自都存在于报告给出的 producer commit（`42a9b2e5…`、`70c2ead6…`）。这是 review 产物留在各自审查分支上的预期状态。官方分类器在 scope / Verify / CI 前先认定 `任务类型：review` 且本范围全部是 verdict Markdown，因此将三项置为 `not_applicable[verdict_only]`；它没有把 doc Verify 错放到 main 上，也没有执行 `test -s`。所以“主干对象中查不到 review 文档”不是本卡业务红或 producer 文件缺失，实际 doc Verify 结论是未运行、未知。两份 not_applicable 仍非 green，账务闸不会把它们当作验收通过；本任务没有把 verdict 移入 main、改命令或伪签 waiver。

## 踩坑

- 旧主 checkout `HEAD=79c3936` 落后于实际合并对象，且仓内没有 `origin/main`。如果只看工作树 HEAD 会选错 Verify 基线；官方此处实际选中的 `origin/HEAD -> origin/master -> 9e87c544…` 与任务给定 SHA 一致。
- Review verdict 不在主干是作者输出边界的一部分。应按其实际单提交范围执行前检，再尊重官方 verdict-only 终态；不能把 main 上查不到当成未生成，也不能把 JSON 的 `not_applicable` 改写为绿。

## 绕过

没有绕过硬闸或修改验收命令。没有运行 `log_acceptance.py`，没有写 acceptance ledger、waiver、PR 评论或发布状态；没有移动 verdict 文档、使用残留 branch 内容冒充 producer、伪造 CI/ref、改官方工具或操作主干。三个目标各执行一次；失败路径未发生，因此没有重试。仓库唯一新增文件为本报告。

## 偏差

- 本验证任务完成；源 dispatch 并非全绿：实现 dispatch precheck 为 green，两份 review dispatch 均为 not_applicable，不能据此声称 review 账务验收通过。平台与模型环境本卡未验证，保持未知。
- baseline 查询失败 `gh api request failed`，继承红无法判定；本次实现 Verify 通过，两项 review 的非 green 属官方预期不适用，不计为新业务红。
- pickup 没有本 worktree 的交接单，故接手锚点为“无”。收件箱仅有与本卡无关的 issue #43；孤儿巡检原行是 `summary: orphan 0 owned 0 unattributable 0 too-new 0 recent-7d 0 stale-over-7d 0 missing_ledger_repos 0`，巡检项未展开，需要时跑 `/worksite-audit`。记忆巡检探针为 `memory_dir_mismatch`（具体本机状态路径见私有 dispatch 报告）；没有改全局记忆状态。
- 任务卡指定的仓库报告目录在合并基线中尚不存在，首次文件写入因缺少父目录失败；随后只创建卡面 scope 路径的父目录并成功落盘，没有触及其他仓库文件。
- 每次命令使用按 dispatch 隔离的 cache 与 tmp 目录。官方临时 Verify worktree 已清理；检查官方 scratch 根时只见既有 `.gate-hub-claude-*` 目录，没有本次 `accept-precheck-*` 遗留。没有使用生产 `.env`、模型或部署进程。
- 首次推送被公开内容扫描器因本机绝对路径与账号标识拒绝；远端目标 ref 查询仍为空。仓库文档改用 delegate state/runtime 根占位符，私有报告保留精确路径，未绕过扫描器。

## 最贵一步

最耗时的是实现 dispatch 的合并后业务 Verify：113.34 秒，真实消费 `9e87c544…` 合并树并通过 299 项、跳过 3 项。两个 review-only dispatch 被官方判为 verdict-only，没有执行其测试命令；未额外重跑全量 CI、cold review 或 fullgate。
