<!-- delegate-outcome: succeeded -->

# C1 merged-main 正式验证证据

## 结论

原 task07 的 schema 2 前置验收结果为 `green`：原 executor 的 5 个改动文件通过 scope，合入主干后的全量 Verify 退出码为 0。实际检查的主干 SHA 是 C1 PR #60 合并提交 `820c3a2ee4fccc1b44187bd99c40cf99c16e1ca2`，与验证后 `git ls-remote` 的非空输出一致；本证据只提供实测，不替主脑决定 accepted7。

本次合并后 Verify 的真实汇总为 `446 passed, 3 skipped, 149 warnings in 230.86s (0:03:50)`。precheck 工具未将 `-rs` 的逐条 skip 名称保留在 schema 2 结果中；因此本次三个 skip 的身份仍是 unknown，不能据此宣称“没有 HTTP decode 类 skip”。

## 身份、范围与调用

- 当前正式验证派发：`dlg-20261003-164205-3b4573`；task07 目标派发：`dlg-20261003-102044-3271a2`，Task-Id `CapsWriter-Offline-with-AI-20261003-07`。
- 目标分支：`card/http-m4-c1-capacity-261003`；正式验证 worktree 为 delegate 分配的独立树；工作分支：`card/http-m4-c1-formal-261003`，起始 HEAD/Base 为 `820c3a2ee4fccc1b44187bd99c40cf99c16e1ca2`。完整绝对路径保存在私有派发报告中。
- 锁定 executor 范围：`e066930ef38aabe8e5051c9256463646f62a7186..14eecc70dabd17823f6996dc57c652b25d195dea`。实际变更文件为：
  - `core/server/http_store.py`
  - `docs/sessions/261003-http-completion/m4-plan.md`
  - `docs/sessions/261003-http-completion/progress/c1-capacity-progress.md`
  - `docs/sessions/261003-http-completion/progress/m4-plan-progress.md`
  - `tests/test_http_capacity.py`

  共 5 个文件，未含根目录 `GOALS`/`goals`。schema 2 的 `checks.scope` 为 `green`，detail 为 `Scope-Globs 检查通过：executor commits (commit-range) 的 5 个变更文件均匹配声明的 glob。`
- 运行时工具解析到 release `0c0762567ba08b872482bbc92d1f091aaf0a2821` 下的 `scripts/delegate/accept_precheck.py`；该文件 SHA-256：`ad09896902b758c1c57c9849085cf7ff05ccea7dbcbafe0a13dabda0fb5a2c02`。记录的是本次调用前实际解析的 release 和哈希，不以当前指针推断历史版本；本机绝对路径保存在私有派发报告中。
- 调用前原派发目录没有 `accept_precheck.json`；没有发现该 task07 的前置检查进程。预检由 PID `325392` 启动，父 shell PID `325277`；观测到 `uv run` PID `330520`，其 pytest 子进程 PID `330571`。白名单环境、argv、cwd、PID、工具及依赖模块哈希和阶段时间记录于私有取证目录的 `invocation.meta.log`；`GH_TOKEN` 只记录为已设置，未记录值。

实际前置检查命令（cwd 即上列 worktree）：

```sh
python3 <runtime-release>/scripts/delegate/accept_precheck.py --dispatch-id dlg-20261003-102044-3271a2 --repo-path <formal-worktree> --commit-range e066930ef38aabe8e5051c9256463646f62a7186..14eecc70dabd17823f6996dc57c652b25d195dea --verify-timeout-sec 900 --timeout-sec 1200
```

上面命令中的两个路径为公开文档脱敏占位符；本次执行的真实 argv 与 cwd 已写入私有派发报告。

单步 Verify 限时 900 秒、总限时 1200 秒，未延长、未重试。实际 Verify-Command 与 task07 卡面一致，模式 `ci-standard-v1`：

```sh
uv run --no-project --python 3.12 --with numpy --with rich --with websockets --with colorama --with pytest==9.1.1 --with soundfile --with pytest-asyncio==1.4.0 --with aiohttp==3.14.3 --with httpx==0.28.1 python -m pytest tests/ -q -rs -p no:cacheprovider
```

调用及各阶段时间（本地 `+08:00`）：

| 时间 | 阶段/结果 |
|---|---|
| 00:46:33.861 | 调用启动；调用前结果文件不存在 |
| 00:46:36.912–00:46:36.921 | 读取 envelope、解析 repo/主干/范围、scope |
| 00:46:37.930 | 进入 `verify_on_merged_main` |
| 00:50:30.664 | Verify 完成，进入只作参考的 CI 检查 |
| 00:50:31.672 | 写出 schema 2 结果；工具退出码 0 |

`accept_precheck.json` 的 SHA-256 为 `a6ccdc3dad969648a7e7258f22ad989fa562c5fc4d06cbc8fe8b132e92b656e2`，完整文件位于原 task07 派发目录；同一字节副本保留在私有取证目录。

## schema 2 与主干 CI 结果

实际结构化结果的关键字段：

```text
schema_version=2
dispatch_id=dlg-20261003-102044-3271a2
task_id=CapsWriter-Offline-with-AI-20261003-07
checked_at=2026-10-03T16:46:35+00:00
head_sha=14eecc70dabd17823f6996dc57c652b25d195dea
main_sha_at_check=820c3a2ee4fccc1b44187bd99c40cf99c16e1ca2
commit_set=14eecc70dabd17823f6996dc57c652b25d195dea,563a54d35c08982e921dc87ace6d92dfedd24aba,5520986c30fe065e448d54819ef6122dbab69611,1b4e9b950ca890fafc8c60427ed86553e6c024c1
identity_source=commit-range
status=green
checks.scope.status=green
checks.verify_on_merged_main.status=green
checks.verify_on_merged_main.exit_code=0
checks.verify_on_merged_main.detail=446 passed, 3 skipped, 149 warnings in 230.86s (0:03:50)
checks.ci.status=green (仅供参考，不参与顶层判定)
```

验证后实际查询命令及 stdout：

```sh
git ls-remote origin refs/heads/master
820c3a2ee4fccc1b44187bd99c40cf99c16e1ca2	refs/heads/master
```

同 SHA 主干 CI 是独立参考，不替代上面的 merged-main Verify。`gh run view 37133802204 --json databaseId,headSha,status,conclusion,workflowName,jobs` 返回 workflow `CI`、head SHA `820c3a2ee4fccc1b44187bd99c40cf99c16e1ca2`、run conclusion `success`。两个实际 job 与 `运行 pytest` 步骤均为 `completed/success`：

| Job | Job ID | pytest 输出 | 真实 job |
|---|---:|---|---|
| `单元测试 (websockets)` | `111234067908` | `446 passed, 3 skipped, 149 warnings in 226.35s (0:03:46)` | [CI job log](https://github.com/zlxlabs/CapsWriter-ASR-Server/actions/runs/37133802204/job/111234067908) |
| `单元测试 (websockets==15.0.1)` | `111234068057` | `446 passed, 3 skipped, 149 warnings in 223.89s (0:03:43)` | [CI job log](https://github.com/zlxlabs/CapsWriter-ASR-Server/actions/runs/37133802204/job/111234068057) |

本地原始 precheck stdout/stderr、主干 CI 结构化 job JSON 和两份完整 job log 的 SHA-256 保存在私有取证目录。主干 CI 输出包含 3 个 skip 计数/进度标记但没有逐项 skip 名称；本次 precheck JSON 也只保留汇总行，因此本次 3 个 skip 的用例身份是 **unknown**。原 task07 交付报告曾在分支本地 run 中记录 `test_aligner_integration.py:53/62`（ForceAligner）和 `test_segmenter.py:208`（silero-VAD/onnxruntime）为三个模型相关 skip；那是另一轮 445-pass 的分支结果，不能冒充本次 820c3a2 merged-main 的逐项证据。可用产物没有显示 HTTP decode skip，但也不足以逐项证明没有此类 skip。

precheck 的 `checks.ci.detail` 显示目标分支查询有 `gate / primary`、`gate / resolve_advisory`、`gate / ocr`、`gate / notify` 等 skipped 状态；这是只供参考的分支线索，不参与顶层 green 判定，也不代表这些 gate 本次完整运行。派发时主干基线记录为 `gh api request failed`；本次没有红项，故无继承红/新红可比对；若登记红类归因，继承红仍应写“未能判定”。

## 已知 P2 与生产边界

截至本次核查，关联 issue [#61](https://github.com/zlxlabs/CapsWriter-ASR-Server/issues/61) 仍为 OPEN。本次未触及其已记录边界：PATCH 自身 SQLite/WAL 增量未完全计入 DB guard；外部删除非终态源会释放声明预留；逐 PATCH 扫描历史 uploads/stat 的成本尚无生产测量；EXPIRED partial 未写尾预留可能导致长期误拒。没有修复工具、配置或业务代码，没有部署，也没有改动生产服务；本 worktree 只新增本证据文档。

## 四问

### 踩到的坑

正式 merged-main 的唯一有效结果是这次新写出的 schema 2 文件；之前搭载 review 的 formal 没有结果，未借用。precheck 只持久化 pytest 汇总，不持久化本地逐条 `-rs` skip 文本，故没有把历史报告里的 skip 名称挪作本次证据。

### 闸与绕过

原范围五个变更文件全匹配原卡 Scope-Globs，没有 root `GOALS` 夹带；scope 与 merged-main Verify 均由正式 precheck 产物判绿。主干 CI 的两个矩阵日志只作同 SHA 参考，`ci` 字段里的 skipped gates 未被当成通过。

### 与卡面的偏差

无命令、范围、时限或代码改动偏差。当前 merged-main 的真实汇总是 446 passed，而原 task07 分支报告记载的历史本地汇总为 445 passed；正式证据采用当前 schema 2 和同 SHA 主干 CI 的 446 结果。逐条 skip 身份在本次产物中缺失，已标 unknown。

### 最贵的一步

merged-main 全量 Verify 用时 230.86 秒；它是唯一耗时检查，正常结束且退出码 0。
