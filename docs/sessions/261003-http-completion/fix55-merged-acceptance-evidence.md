# #55 合并后正式验收证据

记录时间：2026-10-03 22:44 CST。本文记录合并提交 `29194075a088dd00b8b3a3e8d8fc3a752f643f24` 上的预检与完整测试证据；不代表主脑已记录业务 accepted，也不关闭 issue #55。

## 判据结果

| 判据 | 结果 | 证据 |
| --- | --- | --- |
| 原正式预检 | `timeout`，不可接受 | dispatch `dlg-20261003-095625-b3602a` 原结果总时限 1200 秒，停在 `verify_on_merged_main`；`scope`、`verify_on_merged_main`、`ci` 均为 `unknown`。 |
| 一次受限复验 scope | `green` | 同一 dispatch、原范围 `e066930ef38aabe8e5051c9256463646f62a7186..6adeba5b39409964ca11634ed2ba1760a4fde56c`；3 个变更文件均匹配卡面 Scope-Globs。 |
| 一次受限复验 merged-main Verify | `green`，退出码 0 | `426 passed, 3 skipped, 149 warnings in 220.56s`；预检 JSON 的 `main_sha_at_check=29194075a088dd00b8b3a3e8d8fc3a752f643f24`。 |
| 主干身份 | 已核实 | `git ls-remote origin refs/heads/master` 与 `main_sha_at_check` 均为 `29194075a088dd00b8b3a3e8d8fc3a752f643f24`。 |
| 主干 CI | `success` | GitHub workflow run `37125979194`，event=`push`，head=`29194075a088dd00b8b3a3e8d8fc3a752f643f24`；`websockets==15.0.1` 与默认 `websockets` 两个测试矩阵 job 均 `completed/success`。 |

受限复验写回原派发目录的 `accept_precheck.json`，当前顶层 `status=green`。旧 timeout JSON 已先复制到本机临时目录 `fix55-accept-precheck-before-rerun.json`，SHA-256：`6343f8f79c9403252920b5d128eb5bd310c0a42f5a29b3a0f730d1f7b3116b77`。原始事实日志仍在 delegate 私有状态目录的 `cards/caps-http-261003-fix55-formal-precheck.log`。

## 原超时停在哪

原预检执行器版本来自只读 runtime release `6be78d57128687f285353687f448bb7053ec5a6e`。卡面 Verify-Command 是：

```sh
uv run --no-project --python 3.12 --with numpy --with rich --with websockets --with colorama --with pytest==9.1.1 --with soundfile --with pytest-asyncio==1.4.0 --with aiohttp==3.14.3 --with httpx==0.28.1 python -m pytest tests/ -q -p no:cacheprovider
```

工具的 `scripts/ci/ci_evidence.py:142-150` 只把两条固定命令识别为 `ci-standard-v1`：`bash tests/all-shards.sh all` 与 `bash scripts/ci/remote-suite.sh`。本卡自定义 `uv ... pytest tests/` 命令不匹配，因此工具按普通 Verify 路径执行 `bash -c <卡面命令>`，工作目录是临时合并 worktree，子进程继承预检进程环境；`start_new_session=True`。这说明验证通道是本机完整 pytest，而不是 CI 等待通道。

临时树根由 `precheck_worktrees.py` 的 `precheck_worktree_root()` 决定：本机 `XDG_STATE_HOME` 未设置时默认为 `$HOME/.local/state/delegate/precheck-worktrees`，目录名含创建者 PID。原 PID `1846363` 当前已退出，未找到 `accept-precheck-1846363-*` 残留树、活动测试子进程或可归因于该次执行的锁。原结果没有保存子进程 PID、实际 cwd、环境白名单、Verify 输出或子步骤时长；因此不能证明当时是 `uv` 锁、环境、测试用例还是其他运行时延迟造成了 1200 秒耗尽。

能确定的是预算边界：预检主程序用总闹钟 1200 秒；默认 Verify 单步超时为 2700 秒。原报告的 `总时限 1200s 超时，卡在步骤 verify_on_merged_main` 表示外层总闹钟先于单步 Verify 超时触发，并把三项状态统一降为 `unknown`。它不能证明 pytest 本身卡死。旧代码的 `check_verify_on_merged_main()` 在单步 `communicate()` 上捕获 `subprocess.TimeoutExpired` 时会杀子进程组，但外层 `PrecheckTimeout` 走另一条捕获路径；源代码未在该路径显式杀掉 Verify 子进程组。当前未观察到旧子进程残留，故这一点是代码路径风险，不是已观察到的旧现场残留。

## 受限复验的真实命令与产物

执行一次，未重试：

```sh
XDG_STATE_HOME="$HOME/.local/state" python3 "$HOME/.local/lib/agent-config-runtime/current/scripts/delegate/accept_precheck.py" \
  --dispatch-id dlg-20261003-095625-b3602a \
  --repo-path "$PWD" \
  --commit-range e066930ef38aabe8e5051c9256463646f62a7186..6adeba5b39409964ca11634ed2ba1760a4fde56c \
  --timeout-sec 1200 --verify-timeout-sec 2700
```

`--repo-path` 指向本卡独立 worktree；工具按其 git common dir 定位主仓。上面的公开命令用 `$PWD`、`$HOME` 表示复验时展开的本机路径。`--commit-range` 保持原业务范围，参数没有扩大；1200 秒总上限未延长。复验临时树为 `accept-precheck-323199-k_pg2odf`，PID 323199 退出后由工具正常清理。运行环境为 Python 3.12.3、uv 0.12.10；`XDG_STATE_HOME` 显式设为 `$HOME/.local/state`。复验结果文件仍由真实 producer 写入，关键字段为：`status=green`、`head_sha=6adeba5b39409964ca11634ed2ba1760a4fde56c`、`main_sha_at_check=29194075a088dd00b8b3a3e8d8fc3a752f643f24`、scope green、Verify exit 0。

## 独立裸 shell 全量测试

同一最终主干 SHA `29194075a088dd00b8b3a3e8d8fc3a752f643f24` 上，直接执行卡面完整命令，没有添加 `-k`、`--ignore` 或文件级 skip：

```sh
uv run --no-project --python 3.12 --with numpy --with rich --with websockets --with colorama --with pytest==9.1.1 --with soundfile --with pytest-asyncio==1.4.0 --with aiohttp==3.14.3 --with httpx==0.28.1 python -m pytest tests/ -q -p no:cacheprovider
```

第一次独立裸跑退出码 1：`425 passed, 3 skipped, 1 failed, 149 warnings in 311.20s`。唯一失败为 `tests/test_http_file_tasks.py::test_concurrent_http_commit_respects_budget`：一次并发 commit 的本机 HTTP 请求超时，断言收到 `timeout` 而预期 `too_many_jobs`。同一命令随后在唯一一次正式预检复验中于 220.56 秒通过，显示 `426 passed, 3 skipped`；同 SHA 的远端 CI 两个矩阵 job 也成功。故该红在本次采样中未复现，原因未证，不能据此宣称它是稳定回归或继承红。

派发时主干基线查询为 `gh api request failed`，按卡面规则：

- **继承红**：无法判定。
- **新红**：没有确认可复现的新红；记录一次同 SHA 的本机瞬时失败，后续正式预检和远端主干 CI 均通过。没有用这两个绿覆盖首次红的存在。

## 责任仓 issue 草稿（未创建）

目标仓：`agent-config`。静态代码风险，尚未用合成 fixture 复现；不跨仓改动。

**标题**：`accept_precheck 总时限触发时应终止正在运行的 Verify 子进程组`

**正文草稿**：

`accept_precheck.py` 用 SIGALRM 抛出 `PrecheckTimeout`。当 Verify 子进程仍在 `Popen(..., start_new_session=True)` / `communicate(timeout=verify_timeout_sec)` 中运行时，若较短的 `--total-timeout-sec` 先到，异常会越过仅处理 `subprocess.TimeoutExpired` 的进程组清理分支；当前代码继续清理临时 worktree 并写总状态，但没有显式终止该 Verify 子进程组。超时 JSON 也把所有检查统一记成 unknown，未保留已经完成的 scope 状态与当前 Verify 的 argv/cwd/来源。建议加一个 sleep 子进程的负控：`--timeout-sec 2 --verify-timeout-sec 30` 后断言状态为 timeout、子进程组已退出、worktree 生命周期与报告字段一致。此次历史 PID 已退出且无残留树，故此风险未被认定为本次现场已发生的子进程泄漏。`
