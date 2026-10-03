<!-- delegate-outcome: succeeded -->
## 结论

固定审查范围：`f882cd63c9701f7b5bbd0bc274d7b0dc86e08fdc..3f3acff1a00c5681283c315d05fc26eaa2a1a886`，risk-tier `internal`。审查时 HEAD 正是 H0；未追随后续 HEAD，也未修改生产代码或测试。

**Verdict：P2-only，非阻塞；未发现 P1。** 记录两个 P2：stop 重入会在一个实际停机时序中漏掉清理任务的完成观察；到期历史行每小时重复全量物化并重试 ENOENT unlink。两者均不改变被保留的 Job、结果或源字节。未要求引入新 schema、清理账本、retry 或 fallback。

failure-visibility: p2-only

## 审查边界与现场

- 只审生产代码与完整新增测试：`core/server/http_server.py`、`core/server/http_store.py`、`tests/test_http_cleanup.py`。全量测试文件 816 行；生产者自述 `docs/sessions/261003-http-completion/c2-cleanup-evidence.md`、`progress/c2-cleanup-progress.md` 未读、未用作证据。未打开或引用 C1 存档 verdict。
- 规格整读：`docs/sessions/261001-http-files/design.md`、`qa.md`。其他协议和既有问题没有横向审查。
- 固定范围总 diff 为 5 个文件、955 insertions / 4 deletions，其中生产文件改动 42 行与 48 行、测试新增 816 行，另有两份未读的生产者自述文件。没有改这些源文件。
- 工作树在审查开始时干净，HEAD=`3f3acff1a00c5681283c315d05fc26eaa2a1a886`。派发 id 对应的 worktree 锁是本卡现场，按卡面要求继续执行。Pickup 简报无交接单；本次接手锚点：无。
- 主干 CI 基线按卡面记录不可用（`gh api request failed`）；本轮没有继承红/新红 CI 对照，也没有用 draft CI 绿作门禁证据。开放 issue #61 与 HTTP 资源扫描背景相关，但其内容未用于 C2 正确性判断。

## 规格不变式对照

| 不变式 | 生产代码 | 独立测试证据 | 结论 / 未知 |
|---|---|---|---|
| 1. 只清理登记的 DONE/FAILED 且 `terminal_at <= now - 7 days`；保留上传/Job/结果，partial 仅转持久 EXPIRED | `http_store.py:773-804`：查询以 jobs 状态、非 NULL `terminal_at` 和 `<= cutoff` 为准；JOIN 登记上传；unlink 只删 source path；ENOENT 幂等继续；上传只由 UPLOADING 转 EXPIRED。没有按 created_at/mtime 判断，没有删除行或结果 | `test_store_cleanup_uses_terminal_boundary_and_keeps_every_other_source`：等值 DONE、较旧 FAILED 删除；新鲜 terminal_at（即使 created_at 很旧）、RUNNING/QUEUED、NULL terminal_at、活跃 id、partial、未登记 junk 保留；过期 partial 行变 EXPIRED 且字节保留；合法缺文件不报错；Job/结果计数不变，FAILED 仍给原 `decode_failed` | 符合。终态时间等值覆盖；created_at 对照覆盖 |
| 2. 保护 runner 与在途 I/O 完整引用窗口；磁盘/SQL 走单 worker | `http_server.py:281-294` 在网络 loop 取 `runner.active_jobs` 快照，仅将快照送至现有 worker；`http_file_runner.py:340-358, 490-503, 574-585` 中 `_jobs` 存续到 Job task 完成，decoder close 后才释放；`cleanup_terminal_sources` 只在 store 内由该 worker 调用 | `test_periodic_cleanup_waits_for_real_runner_reference_then_keeps_result` 通过真实 TCP listener 上传/commit，持真实 `HttpFileRunner` job 引用；真实 SQLite DONE 已持久化且被测试推进至七天外时，阻塞 close、等两个清理周期仍断言源字节在；释放后下一周期删除。并验证 TCP job/result、结果 JSON 存储字节、partial/junk/FAILED 结果和 replay | 主路径符合。测试用受控 decoder 替身阻塞 `close()`，runner 与网络服务是真实实现；没有声称这是 ffmpeg 真实进程 I/O 的基准 |
| 3. 清理监督与关闭顺序；未知 I/O/权限错误 fatal；无 catch-log-continue/retry/fallback | `http_server.py:269-301` 启动周期任务并将未取消异常交 `_mark_fatal`；`stop()` 先停清理 producer，再等清理 task，再停 runner/store/worker。正常活动清理不取消，睡眠态取消 | `test_cleanup_storage_failure_uses_fatal_serve_chain` 在真实 unlink 点注入 PermissionError，断言 `serve()` 原异常、源和 COMMITTED 元数据保留、worker 关闭；`test_shutdown_waits_for_inflight_cleanup_io` 屏障阻塞 worker 操作，确认 serve 不提前完成，释放后 worker future 清空且 `fatal is None` | 普通单次 stop / fatal 路径符合。发现一个 re-entry 例外见下方 P2；sleep 态没有单独用例，但控制流为 stopping 时取消 sleeper、done callback 忽略 cancelled task |
| 4. 释放依赖 C1 source presence，不新增释放事实；完整结果/FAILED 错误/replay | `http_store.py:756-770, 789-804` 通过文件 `stat` 得出 `source_available`；没有列、表、缓存或第二账本 | 周期 TCP 用例断言删除后 `source_available=false`、DONE result 200 完整字段及 SQLite payload 字节完全相同；FAILED result 409 带原 error_code；create/commit replay 返回同 id、jobs 行数不增 | 符合；没有持久释放状态 |
| 5. HTTP disabled/stop 不创建额外周期、不动默认 WS | `core/server/app.py:140-170` 仅显式 HTTP 设置时装配 `HttpServer`；新周期只在 `HttpServer.serve()` listener start 成功后创建；改动不触及 WS route/protocol | 正常 HTTP stop 用例断言 `fatal is None`；现有 app 装配分支保留默认关闭 | 静态调用路径支持。此轮没有启动外部 systemd unit 做部署配置验证；HTTP 开关来自既有配置解析，不由新周期常量控制 |

### 生产者和事实源枚举

- 源名只有 `create_upload()` 生产：服务端 `upload_id = uuid.uuid4()`，`source_name = f"{upload_id}.bin"`（`http_store.py:589-590`）；uploads 的 upload_id 是主键，jobs.upload_id 有 UNIQUE，job_id 单独 UUID。job 与源的一对一映射不依靠新缓存或新 ID 规则。
- 源读取/计费调用路径：上传 append/hash 校验、`job_source()` 给 runner path、`_iter_present_sources()` / `_reserved_source_bytes()` 按实际 `stat` 计费、`job_record()` 的 `source_available` 也看实际 `stat`。清理 unlink 是唯一新增删除路径。`FileNotFoundError` 仅在该合法终态删除操作幂等；permission/stat 等其他错误向上交监督。
- `terminal_at` 写路径全部核过：启动收敛 `QUEUED/RUNNING -> FAILED`（`_converge_restart`）、`fail_job()`、`record_result()`；新任务初始为 NULL，`mark_running()` 不写 terminal_at。删除候选不漏过等价成功/失败分支。
- unlink 前不可逆动作是删除一个 UUID 源路径的目录项；Job/上传元数据/结果均留存。删除后第二计算点是 C1 `stat` source-presence，实际返回 false 并按源字节停止预留；不存在待清理状态或第二份计费记录。

## Findings

### P2 — `HttpServer.stop()` 并发重入时第二个调用绕过清理 task drain

违反条款：不变式 3（停机等待清理 I/O 完成并观察，再停 runner/worker）。位置：`core/server/http_server.py:303-316`。调用关系：`core/server/app.py:72-89` 在 SIGINT/SIGTERM 处理里启动一个 `http_server.stop()`；同一时序中 `SocketManager` 因退出 sentinel 结束后，`_serve_all()` 会取消仍等待中的 `http_server.serve()`（`app.py:163-189`），其 `finally` 再调用一次 `stop()`（`http_server.py:274-279`）。第一次 stop 将 `_source_cleanup_task` 设为 None 后等待局部 task；第二次看不到 task，直接进入 runner/listener 收尾。

独立现场：`/tmp/http-m4-c2-shutdown-fatal-real-socket.py` 使用实际 `CapsWriterServer.stop/_serve_all`、实际 `SocketManager` 和实际 `HttpServer`，只替身了识别进程/WS 结果消费者，并在真实周期清理调用中屏障阻塞后注入 unlink PermissionError。观测：`server.fatal` 已是 PermissionError、源仍存在，但 `app_exit_code=0`、`serve_all_outcome=returned`；输出出现 `Task exception was never retrieved`，且有 `HttpServer.stop()` 仍等待 cleanup gather 的 pending task。错误有 ERROR 日志，源与记录没有被错误删除，因此按 P2；这个现场不满足结果错误且无任何可见告警的 P1 条件。

P1 两问：①真实代码触发路径存在，正常 App stop 会并行启动显式 stop 和 serve-finally stop；隔离 TCP 服务路径实际复现了在途 I/O + PermissionError。②后果是退出状态可能未反映 cleanup 错误、清理任务未观察，但该特定现场同时正在执行用户请求的停机，错误有日志，源被保留并可在后续启动重试周期清理；未发生用户结果/账本丢失，故按非阻塞 P2 记，不要求本轮加入新机制。

这不是“按卡面边界未改”的豁免：单次 stop 的新增代码符合顺序，实际 `App.stop` 与 `serve().finally` 的既有双入口使该顺序在并发调用下失效。

### P2 — 每个周期重复物化并 unlink 全部历史到期 Job

违反条款：不变式 3 的长期周期监督可用性要求（单 worker 承载请求 I/O）；位置：`core/server/http_store.py:789-804` 与 `http_server.py:293-294`。SQL 每小时 `fetchall()` 所有七天前 DONE/FAILED 行；任务与结果按合同不删，清理后也没有标记区分已 unlink 行，所以后续周期仍会物化这些行、逐个尝试 unlink 并处理 ENOENT。内存高水位与系统调用量都随历史终态 Job 数增长；这是性能/容量风险，不是源误删。

证据：与现有 `_iter_present_sources()` 逐游标迭代（`http_store.py:437`）对比；周期单 worker 的调用位置 `http_server.py:287-294`。可用 cursor iteration 降低一次 `fetchall()` 的峰值内存，但无法消除受合同约束的重复历史扫描/ENOENT。禁止新增 release ledger/schema/cache，所以不据此提出第二事实源。

P1 两问：①代码实际每个周期都会走该查询，终态行能随 Job 创建持续积累；但此 worktree 无真实部署数据库/历史行数，不能声称已经触发内存或延迟故障。②增长后会占用单 worker 时段并抬高内存，但当前没有量到不被接受的运行影响，按 P2 接受不阻塞。没有为理论规模增加状态/防御分支。

## OCR 工具 finding 判级对照

OCR 固定输入 SHA=`f882cd6...` 到 `3f3acff...`，`--audience agent --concurrency 4`，中性背景摘要 1,303 bytes（低于 8,000）。全量 JSON 保存为 `/tmp/http-m4-c2-review1-ocr.json`（52,286 bytes）；从文件解析：`status=reviewed`、`reason=primary_selected`、`profile=minimax`、`model=MiniMax-M3.1-Flash-Preview`、`cli_status=complete`、`coverage=complete`、`verify_status=completed`、2/2 verified/confirmed。stderr 记录 primary elapsed 553.789s。不是 skipped；扫描完成。完整 finding 文件保留，当前报告按本仓风险等级复判。

| OCR 工具标注 | 本仓判定 | 真实路径（第一问） | 后果（第二问） | 处置 |
|---|---|---|---|---|
| low：`http_store.py:789-797` 全量 fetchall 与重复 unlink | P2 性能 finding | 终态行达到七天后即进入此周期；代码可证。真实生产行数/最大值未量得，工作树没有使用真实服务数据库 | 历史增长时成本线性，但未测到当前不可接受负载 | 上述 P2；接受不阻塞，不引入释放标记 |
| medium：`http_server.py:286-292` `io_overloaded` 变 fatal | ≤P3，未作为确认 defect | mailbox 为 32；普通入口并发受 16 handler、单 runner gate、共享 admission lock 与 max_tasks 限制。没有从受支持 producer 证明可到 32 个同时在途 future，也没有真实运行计数证明发生过；直接人为填满 worker 不代表真实服务路径 | 若真满，周期任务会让 listener fatal，但这是显式失败路径；不会把错误结果当成功，且不符合增加 retry/fallback 的合同 | 不按 OCR medium 升级，不派 P1；未引入新机制。已记录为可再验证前提 |

## 验证与实际输出

- 指定依赖隔离运行完整文件（uv `--no-project`、Python 3.12、numpy/rich/websockets 15.0.1/colorama/pytest 9.1.1/soundfile/pytest-asyncio 1.4.0/aiohttp 3.14.3/httpx 0.28.1）：

```text
.....                                                                    [100%]
5 passed in 1.77s
```

HTTP 用例未 skip。定向生命周期与 worker 竞态各重复 5 轮，共每轮 2 passed：

```text
.                                                                       [100%]
2 passed in 1.35s
..                                                                       [100%]
2 passed in 1.50s
..                                                                       [100%]
2 passed in 1.31s
..                                                                       [100%]
2 passed in 1.31s
..                                                                       [100%]
2 passed in 1.66s
```

worker 竞态单测内部又各跑 5 个 append/commit/record_result 屏障交错；pending future 达 2 时 cleanup 已入单 worker 队列，释放 writer 后再删源并检查 offset/结果。`test_shutdown_waits_for_inflight_cleanup_io` 检查实际 worker pending 集合、closed 状态和 fatal；权限失败测试检查 serve 抛出的异常、worker 收尾、源与数据库行。

- base 红验：通过 `本机 agent-config 中的 scripts/git/scratch-worktree.sh <repo> f882cd63... -- /tmp/http-m4-c2-red-run.sh` 在固定 base 临时树拷入完整新测试文件，只增加一个实际周期 producer 断言；先由注入脚本确认测试名/断言都已写入，再执行目标用例。旧 base 的 HTTP listener 启动后，eligible DONE 源仍在，pytest 以目标 `AssertionError` 失败（不是 ImportError/AttributeError）：

```text
confirmed copied test and injected eligible-source assertion in tests/test_http_cleanup.py
821:async def test_red_verify_periodic_cleanup_removes_eligible_source(tmp_path):
836:        assert not source.exists(), "periodic cleanup did not unlink eligible source"
F                                                                        [100%]
=================================== FAILURES ===================================
___________ test_red_verify_periodic_cleanup_removes_eligible_source ___________
tests/test_http_cleanup.py:836: in test_red_verify_periodic_cleanup_removes_eligible_source
    assert not source.exists(), "periodic cleanup did not unlink eligible source"
E   AssertionError: periodic cleanup did not unlink eligible source
E   assert not True
E    +  where True = exists()
------------------------------ Captured log call ------------------------------
INFO     server:http_server.py:263 HTTP 文件任务 listener 已就绪 (监听: 127.0.0.1:38413)
INFO     server:http_server.py:308 HTTP 文件任务 listener 已停止
1 failed in 0.33s
confirmed intended AssertionError red (pytest rc=1)
```

scratch helper 自动清理了脏临时 worktree；之后 `git worktree list --porcelain` 未见该 scratch 树。注入/运行脚本和输出仍在 `/tmp`，没有拷回 repo。

- `git diff --check HEAD^ HEAD` 退出码 0。
- 红验后 shebang 检查发现仓库没有 `.venv`，因此不存在被临时 uv 环境覆盖的 repo console scripts；uv 命令全部 `--no-project`。

## 四项复盘

### 踩坑

第一次红验脚本在 base 找不到新增的 `tests/test_http_cleanup.py`；复制文件后第二次又因 `_StubApp` 的推理协调者默认 false，真实 commit 在 503 处结束。两者均是红验夹具设置错误，不计为测试红。修正为从 H0 拷入测试并在新建 Job 前启用 stub inference，再次确认注入存在后得到上面的目标 AssertionError。第一次 shebang 探针在 zsh 下因不存在 `.venv/bin/*` 触发 `NOMATCH`；改为先判目录后确认无 `.venv`，结论只限于“无本仓 venv 可污染”。

### 闸与绕过

review-discipline 全文已读；H0 固定、OCR wrapper 固定 SHA、完整 5 项测试、定向重复、base scratch 红验均完成。完整 OCR JSON 虽然单行回显在终端被截断，但原文件完整保存，并用 jq 从该文件读取 status/reason/findings/verification/counts；不把工具 severity 当本仓 verdict。没有绕过 CI/部署闸，也没有把 draft 绿当通过。

### 偏差

生产实现/测试均未修改。严格审查了卡面要求的代码与测试；自述文件、C1 存档 verdict、其他协议均未作正确性依据。独立 stop 重入探针为隔离 App/HTTP 服务，不等同部署主机上的 systemd 实测。生产历史 DB 最大行数未量取，所以性能量级保持未知。pickup memory 探针退出 2，原文为：`memory 巡检报告不可用：memory_dir_mismatch（本机 memory-doctor 状态文件）`；欠账探针为 `summary: orphan 0 owned 0 unattributable 0 too-new 0 recent-7d 0 stale-over-7d 0 missing_ledger_repos 0`。

### 最贵的一步

OCR 主腿从 start 到完成 553.789 秒；整文件测试约 1.77 秒，两个定向测试 5 轮合计约 7 秒。主干基线不可用，所以没有成本去分辨继承红与本轮新红。

## 下一步

按卡面只提交本 verdict 与进度文件、push 当前 review 分支，并从远端核验 tip 与本地 clean status；不创建 review PR，不改 ready/merge/deploy 状态。

机读判据由 `本机 agent-config 中的 scripts/review/extract-failure-visibility.sh` 对本文件提取，实际 stdout 为 `p2-only`。仓库内相对路径 `scripts/review/extract-failure-visibility.sh` 不存在，已在 agent-config 中按文件名检索并调用本机脚本路径；判据未靠人读文本推断。
