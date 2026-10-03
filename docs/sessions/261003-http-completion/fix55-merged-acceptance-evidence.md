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

主脑记录的旧进程实际工具命令使用 agent-config 主 checkout 中的脚本；以下路径用 `$HOME` 表示。命令明确传入 `--verify-timeout-sec 900`，并非依赖默认的 2700 秒：

```sh
python3 "$HOME/projects/personal/agent-config/scripts/delegate/accept_precheck.py" \
  --dispatch-id dlg-20261003-095625-b3602a \
  --repo-path "$HOME/projects/oss/CapsWriter-Offline-with-AI-worktrees/lead-261003-47579f61" \
  --commit-range e066930ef38aabe8e5051c9256463646f62a7186..6adeba5b39409964ca11634ed2ba1760a4fde56c \
  --verify-timeout-sec 900 --timeout-sec 1200
```

旧结果只证明总超时发生在 `verify_on_merged_main` 阶段，三项状态被记为 `unknown`；没有 Verify 退出码、子步骤耗时或完整输出。旧 PID `1846363` 已退出；只读检查未发现对应的 `accept-precheck-1846363-*` 临时树、活动测试子进程或可归因于该次执行的锁。历史根因仍未证实，现有证据不能区分测试、环境、锁或其他运行时延迟。

复验诊断时 `runtime/current` 指向 release `6be78d57128687f285353687f448bb7053ec5a6e`，但旧命令使用的是 agent-config 主 checkout 路径；没有归档旧 PID 的脚本版本、实际环境和 cwd。故该 runtime release 只能说明复验工具来源，不能证明旧 PID 加载了它。也没有足够的历史版本与参数证据判断旧运行中 `900` 如何被消费，不据此推测超时机制。

## 受限复验的真实命令与产物

执行一次，未重试：

```sh
XDG_STATE_HOME="$HOME/.local/state" python3 "$HOME/.local/lib/agent-config-runtime/current/scripts/delegate/accept_precheck.py" \
  --dispatch-id dlg-20261003-095625-b3602a \
  --repo-path "$PWD" \
  --commit-range e066930ef38aabe8e5051c9256463646f62a7186..6adeba5b39409964ca11634ed2ba1760a4fde56c \
  --timeout-sec 1200 --verify-timeout-sec 2700
```

`--repo-path` 指向本卡独立 worktree；工具按其 git common dir 定位主仓。复验时 `runtime/current` 指向上文 release；这只标识复验工具。复验保留原 `--commit-range` 和 1200 秒总上限，但将子步骤上限设为 `--verify-timeout-sec 2700`，高于旧实际命令中的 900 秒，是明确的参数偏差，不能称所有时限均不变。实际 Verify 用时 220.56 秒，低于旧命令记录的 900 秒；这保留了成功验收结果的证据，但不证明旧参数合规或旧超时根因。复验临时树为 `accept-precheck-323199-k_pg2odf`，PID 323199 退出后由工具正常清理。运行环境为 Python 3.12.3、uv 0.12.10；`XDG_STATE_HOME` 显式设为 `$HOME/.local/state`。复验结果文件仍由真实 producer 写入，关键字段为：`status=green`、`head_sha=6adeba5b39409964ca11634ed2ba1760a4fde56c`、`main_sha_at_check=29194075a088dd00b8b3a3e8d8fc3a752f643f24`、scope green、Verify exit 0。

## 独立裸 shell 全量测试

同一最终主干 SHA `29194075a088dd00b8b3a3e8d8fc3a752f643f24` 上，直接执行卡面完整命令，没有添加 `-k`、`--ignore` 或文件级 skip：

```sh
uv run --no-project --python 3.12 --with numpy --with rich --with websockets --with colorama --with pytest==9.1.1 --with soundfile --with pytest-asyncio==1.4.0 --with aiohttp==3.14.3 --with httpx==0.28.1 python -m pytest tests/ -q -p no:cacheprovider
```

第一次独立裸跑退出码 1：`425 passed, 3 skipped, 1 failed, 149 warnings in 311.20s`。唯一失败为 `tests/test_http_file_tasks.py::test_concurrent_http_commit_respects_budget`：一次并发 commit 的本机 HTTP 请求超时，断言收到 `timeout` 而预期 `too_many_jobs`。同一命令随后在唯一一次正式预检复验中于 220.56 秒通过，显示 `426 passed, 3 skipped`；同 SHA 的远端 CI 两个矩阵 job 也成功。故该红在本次采样中未复现，原因未证，不能据此宣称它是稳定回归或继承红。

派发时主干基线查询为 `gh api request failed`，按卡面规则：

- **继承红**：无法判定。
- **新红**：没有确认可复现的新红；记录一次同 SHA 的本机瞬时失败，后续正式预检和远端主干 CI 均通过。没有用这两个绿覆盖首次红的存在。

## 根因状态

原记录可证的范围仅为：总超时停在 `verify_on_merged_main`、旧 PID 已退出且未发现可归因残留；根因未证。此前基于复验时源码写出的子进程清理机制和责任仓 issue 草稿不能归因到历史 PID，本次不将它们作为现场根因或结论。
