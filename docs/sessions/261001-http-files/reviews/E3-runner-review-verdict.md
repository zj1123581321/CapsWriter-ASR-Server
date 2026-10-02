<!-- delegate-outcome: succeeded -->
# E3 HTTP 文件 runner 独立冷审查

## Verdict
failure-visibility: p1-found

审查对象为 `2ab765050f8c5efcffd5e116d2f867c67b616a12..29d843f952277d09d7ab48106a74cd907970cf8a`。结论：FAIL，不能按本卡完成条件放行。

## Findings
### P1-1：冻结差异违反 200 行硬预算
- 违反：任务卡 `Diff-Lines-Hard: 200`。
- 证据：`git diff --numstat` 实测 12 个文件、1710 additions、22 deletions，共 1732 行；`git diff --check` 本身通过。
- 建议：拆分或减法收敛到不超过 200 行后重新审查；本轮不改业务代码。

### P1-2：HTTP 主动运行数绕过 R7 的 1 个预算
- 违反：`design.md` R7“HTTP 主动运行 1、与 WS 共用 max_tasks=8”，以及 `qa.md` 第 9/11 组资源与监督契约。
- 证据：`HttpFileRunner.submit()`（`core/server/http_file_runner.py:300-308`）为每个 Job 直接创建后台任务，没有全局运行闸门；隔离 `port 0`/临时目录/假引擎探针实测 `submitted=3, active_http_jobs=3, runner_active_jobs=3`。`MAX_HTTP_JOBS=8` 只限制持久 Job 数，不能证明主动运行数为 1。
- 两问：真实路径已由 3 个 HTTP commit 实测触发；同时解码/分段/在途 slot 随 Job 增长，绕过既有总预算不可接受。
- 建议：在进入解码前使用单一全局运行闸门，未获闸门的 Job 保持 QUEUED；只在获闸门后标 RUNNING，终态提交后释放。

### P2-1：段超时释放内存 owner 前没有持久 FAILED
- 违反：`design.md` R4“worker 超时或进程失败先完成持久 sink 提交，再释放 HTTP owner”。
- 证据：E3 接通后用真实独立服务、`CW_SEGMENT_TIMEOUT=0.2`、假 worker 延迟 5 秒实测服务 `exitcode=1`，但 SQLite Job 为 `state=QUEUED, error_code=NULL`；当前监督路径只做内存 `transition_terminal`，重启后才由 `server_restarted` 收敛。
- 建议：超时路径在移除 HTTP owner 前可靠写入 FAILED 和错误码；写入失败继续非零退出，但不能先丢掉持久失败事实。

### P2-2：运行中的 Job 永远不落 RUNNING
- 违反：`design.md` R5/R6 与 `protocol.md` HTTP Job 状态机 `QUEUED -> RUNNING -> DONE/FAILED`。
- 证据：`commit_upload()` 只建立 QUEUED（`core/server/http_store.py:541-570`），冻结 diff 没有 QUEUED→RUNNING 写入；真实 stalled-runner 用例仍断言活动识别期间为 QUEUED（`tests/test_http_file_runner.py:418-438`）。
- 建议：实际取得运行闸门并开始解码/入队时条件更新为 RUNNING；该状态须由 SQLite 真源读取。

### P2-3：持久 sink 没有断言 tokens 拼接等于 text_accu
- 违反：`protocol.md` 结果契约明确要求 `len(tokens)==len(timestamps)` 且 `''.join(tokens)==text_accu`。
- 证据：`HttpStore.record_result()`（`core/server/http_store.py:651-684`）只检查长度，不检查拼接一致性；隔离真实 SQLite 探针已成功持久化 `DONE`，同时保存 `tokens=['a']`、`text_accu='ab'`。当前 `TaskPipeline` 生产端虽在 `core/server/worker/pipeline.py:201-205` 检查，sink 边界仍可落入不一致结果。
- 建议：在 `record_result` 的同一校验处增加拼接断言并补反例测试；不要比较独立语义的 `text`。

## Evidence
- 按规格依赖运行：E3 runner `15 passed`；HTTP store/supervision `27 passed`；旧 WS、owner IPC、分段、pipeline 回归 `60 passed, 1 skipped`。
- 系统 Python 因未安装 aiohttp 首次运行结果为 `1 skipped`；用规格要求的隔离 uv 环境安装 `aiohttp==3.14.3` 后已重跑，不能把 skipped 当通过。
- 真实 producer 证据成立：测试用真实 ffmpeg 包装脚本记录实际 argv/PATH，真实 multiprocessing Task/Result 过 Queue，容器解码不落临时 PCM；结果与 DONE 同事务路径通过。
- `templates/REFACTOR-guide.md` 在给定 agent-config 树中不存在，熵增条款无法机械核对；本 verdict 未把“未发现”伪装成 clean。
- 远端核验：`origin/card/http-e3-runner-261002` 为 `29d843f...`；但 `gh pr view 38` 显示 PR38 已 merged、head 为 `e1fa979...`，因此“PR38 head 为 29d843f”这一卡面前提无法成立，本轮未改远端。

## Disposition
本轮只新增本 verdict 文件，未改业务代码、测试、协议、配置、CI 或部署；未重跑 Gate、未合并、未部署。由于存在 P1，失败可见性取 `p1-found`。
