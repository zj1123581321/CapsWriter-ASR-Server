# 验收台账脱离 Git 跟踪

- 卡号：`CapsWriter-Offline-with-AI-20260930-02`
- dispatch id：`dlg-20260930-143536-4fac56`
- 分支：`card/retro-ledger-untrack-260930`

## 改动摘要

1. `.gitignore` 在验收台账锁文件规则附近新增：
   `retro/*.jsonl`
   ，并注明“仓内台账不随仓分发（真身在私有仓 agent-config）”。原有 `memory/` 规则未改动。
2. `git rm --cached -- retro/acceptance-log.jsonl` 已执行，工作区文件保留。
3. 现场原工作区只有 4 行；根据任务卡指定备份 `/tmp/acceptance-log.local-only.jsonl` 恢复为 11 行，随后用 `cmp`、行数和 MD5 核对完全一致。
4. 提交：
   - `db6f84f [codex] chore: untrack local acceptance ledger`
   - `bb848cd [codex] chore: ignore local acceptance ledgers`

## retro 跟踪清单

取消跟踪前，`git ls-files 'retro/**'` 仅有：

```text
retro/acceptance-log.jsonl
```

取消跟踪后，`git ls-files 'retro/**'` 输出为空；没有其它被跟踪的 `retro/` 台账文件。

## 验收输出

忽略规则：

```text
$ git check-ignore -v retro/acceptance-log.jsonl
.gitignore:207:retro/*.jsonl	retro/acceptance-log.jsonl
```

最终状态：

```text
$ git status --porcelain
（空）
```

台账文件：

```text
$ wc -l retro/acceptance-log.jsonl
11 retro/acceptance-log.jsonl
$ md5sum retro/acceptance-log.jsonl
e292ec58428366842561e1286bcf2e58  retro/acceptance-log.jsonl
$ cmp -s retro/acceptance-log.jsonl /tmp/acceptance-log.local-only.jsonl && echo 'backup-compare: identical'
backup-compare: identical
```

## 测试

执行 `python3 -m pytest tests/ -q`，结果：

```text
1 failed, 226 passed, 3 skipped, 81 warnings in 91.81s (0:01:31)
```

唯一失败是任务卡已列明的继承红：
`tests/test_protocol_v2.py::test_task_end_logs_one_structured_line_success_and_failure`。
未发现新失败；本次环境通过数为 226（任务卡记录的基线为 225），基线作业名和首个失败步骤无法从派发时的 GitHub API 失败信息进一步核定。

## 推送与草稿 PR

GitHub 将任务卡中的旧仓名重定向到当前规范仓；当前 `origin` 为规范仓地址。第一次按任务卡旧仓名直接尝试：

```text
$ git push git@github.com:zlxlabs/CapsWriter-Offline-with-AI.git HEAD:refs/heads/card/retro-ledger-untrack-260930
ERROR: Repository not found.
fatal: Could not read from remote repository.
```

随后向规范 `origin` 推送，按预期被公开内容扫描阻断：

```text
pre-push public-scan blocked: docs/sessions/260930-retro-ledger-untrack/report.md:89:local_absolute_path
pre-push public-scan blocked: docs/sessions/260930-retro-ledger-untrack/report.md:89:username
pre-push public-scan: public content rejected
error: failed to push some refs
```

目标分支未产生远端命中；扫描阻断发生在推送写入前，因此没有创建 draft PR。修正本报告后仍需由主脑决定是否以公开安全的报告再次尝试推送。

## 接手巡检记录

- 接手简报：无交接单。
- `archive_orphan_debts.py --oneline`：`summary: orphan 0 owned 0 unattributable 0 too-new 0 recent-7d 0 stale-over-7d 0 missing_ledger_repos 0`
- `memory_report.py --oneline`：退出码 2，`memory 巡检报告不可用：memory_dir_mismatch（本机状态文件路径已省略）`
- 开放问题收件箱：无条目。
