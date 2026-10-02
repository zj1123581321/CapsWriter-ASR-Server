<!-- delegate-outcome: succeeded -->

# E3（HTTP 文件 runner）实现记录 —— 由独立复验重建

> **来源声明**：上一轮执行者（派发 `dlg-20261002-124436-9ed5d9`）4 个提交已推远端但**没有留下
> `report.md`**，collect 判为 `died / report_missing`。本文件**不是**执行者自述，
> 是**另一轮独立复验（派发 `dlg-20261002-133618-2d23e7`，verify-mode `docs-only-v1`）**
> 在不重跑实现、不改代码的前提下，把 `git diff` 里看得见的事实与**本卡亲自跑出来的**
> 命令输出重建成规范记录。凡本卡没亲自跑过的，一律在第 4 节标「未由本卡复验」，
> 不引用上一轮的自述当结论。

- 复验时间：2026-10-02T21:48:51+08:00
- 复验工作树：<repo>/http-e3-record-261002（`<repo>` = 本仓 worktree 根，
  下文所有 `cd <repo>` 都指它；本仓是公开仓，故不写本机绝对路径）
- 复验分支：`card/http-e3-record-261002`（父提交 = 实现卡 HEAD `29d843f`）
- 复验环境：Linux / Python 3.12 / ffmpeg `/usr/bin/ffmpeg`（`which ffmpeg` 命中）
- Verify-Command：`test -s docs/sessions/261001-http-files/reviews/E3-runner-implementation-record.md`

---

## 1. 四个提交实际做了什么（逐文件，只描述 diff 看得见的事实）

`git log --oneline 2ab7650..29d843f`：

```
29d843f [pi-executor] docs: 记录 HTTP 文件 runner 的真实数据流与 M3 证据
b4b59b5 [pi-executor] test(http): 修掉跨进程挂死与孤儿泄漏，runner 测试全绿
6645ceb [pi-executor] feat(http): E3 原容器解码 runner 与持久结果下沉
b62afc1 [pi-executor] test(http): E3 runner 跨进程红灯测试
```

`git diff --stat 2ab7650..29d843f`：

```
 core/server/app.py                |   6 +
 core/server/connection/ws_send.py |   8 +-
 core/server/http_file_runner.py    | 519 ++++++++++++++++++++++++++++
 core/server/http_server.py        |  57 +++-
 core/server/http_store.py         |  54 ++-
 core/server/state.py              |  14 +
 docs/development/data-flow.md     |  23 +-
 docs/development/key-paths.md     |   2 +
 goals/http-core/M3-runner.md      |  11 +-
 tests/harness/server.py           | 265 +++++++++++++++
 tests/harness/worker.py           |  85 +++++
 tests/test_http_file_runner.py    | 688 ++++++++++++++++++++++++++++++++++++++
 12 files changed, 1710 insertions(+), 22 deletions(-)
```

### b62afc1（红灯测试，纯新增 984 行，只碰 tests/）

- `tests/test_http_file_runner.py`（+668）：12 个 async 测试函数，其中 1 个按容器参数化
  ×4（mp3/aac/m4a/opus）、1 个按故障注入参数化 ×2（`pcm_chunks` / `enqueue`）。
- `tests/harness/server.py`（+231）：新增 `ObservedTaskQueue`、`_FaultyPutQueue`、
  `_apply_fault`、`run_managed_http_server`、`_child_with_stderr`、`ManagedHttpServerHarness`。
- `tests/harness/worker.py`（+85，新增文件）：`run_recording_worker` 包住 `queue_in.get` /
  `queue_out.put`，把**子进程真实收到**的 Task（`received_task_record`）与**真实发出**的
  Result（`result_payload`）写进父进程的 `Manager().list()`。

### 6645ceb（实现，+681/-24）

- 新增 `core/server/http_file_runner.py`（519 行）：`FileSourceDecoder`（固定 argv 的
  `create_subprocess_exec` ffmpeg，输出只走管道）、`HttpResultSink`、`http_result_payload`、
  `HttpFileRunner`、`JobFailure` / `RunnerUnavailable`。常量：`DECODE_READ_BYTES=64KiB`、
  `MAX_TASK_SECONDS`（`CW_MAX_TASK_SECONDS`，默认 14400）、`DECODER_CLOSE_TIMEOUT=10.0`、
  `RUNNER_STOP_TIMEOUT=20.0`、`STDERR_TAIL_BYTES=500`。
- `core/server/app.py`（+6）：`CW_HTTP_PORT` + `CW_HTTP_DATA_DIR` 显式开启时装配
  `HttpFileRunner`，`attach_runner` 后把 `state.http_result_sink` 指向 `runner.result_sink`。
- `core/server/http_store.py`（+54）：`HttpStoreError` 加 `error_code`；新增 `job_source()`、
  `fail_job()`；`record_result` 的 docstring 由「供 E3 使用，本增量不主动调用」改为「由 sink 调用」。
- `core/server/http_server.py`（+57）：`attach_runner`，commit 首次受理才 `submit`，重复 commit
  返回同一 Job 不重投，无 runner 仍 503，收尾先停 runner。
- `core/server/state.py`（+14）：`register_http_job` / `register_segment_submission` /
  `transition_terminal` / `release_terminal_task` 等运行态记账。
- `core/server/connection/ws_send.py`（±8）：**删掉**「HTTP 结果无对应活动任务就
  `raise RuntimeError`」，改为按契约记 debug 并丢弃。

### b4b59b5（只碰 tests/harness/server.py +46/-17 与测试 +30/-12）

diff 里可见的两处实质修改：

1. `_child_with_stderr` 开头加 `os.setsid()`；`cleanup()` 改为先 `pgid = self.process.pid`、
   kill 主进程、`join(5)`、再 `os.killpg(pgid, SIGKILL)`。注释写明「必须在回收主进程之前拿组号，
   reap 之后 getpgid 会查不到」。
2. 两处重启用例把 `assert restarted.received == []` 改成 `assert list(restarted.received) == []`，
   并把 SDK 调用换成新增的裸 HTTP `raw_job()`。
3. `_apply_fault` 补 `enqueue` 分支返回 `_FaultyPutQueue`，否则 `enqueue` 注入直接启动失败。

### 29d843f（纯文档，+27/-9）

`docs/development/data-flow.md` 把「未来 HTTP 文件任务边界」改成当前真实链路并写明失败语义与
重启收敛；`docs/development/key-paths.md` 增列 runner 与存储入口；
`goals/http-core/M3-runner.md` 勾掉 2 条「推进前必须拿到的证据」与 3 条「完成条件」。

> **事实提示**：实现卡改了 `goals/http-core/M3-runner.md`。本卡 Scope-Globs 不含该文件，
> 本卡**没有**改它，仅在此记录。

---

## 2. 本卡亲自复跑的结果

### 2.1 实际使用的完整命令（原文，逐字粘贴）

工作目录统一为 `<repo>`（本仓 worktree 根；公开仓不写本机绝对路径）。
仓库无 `pyproject.toml`，裸 `uv run pytest` 落在空环境（实测 `1 skipped in 0.06s`，
退出码 5，`aiohttp` / `websockets` 均 `ModuleNotFoundError`）；因此沿用
`docs/development/testing.md` 里记录的 `--no-project --with …` 形式。

**(a) 目标文件**

```sh
cd <repo>
timeout -k 10 420 uv run --no-project --python 3.12 \
  --with numpy --with rich --with websockets --with colorama --with pytest==9.1.1 \
  --with soundfile --with pytest-asyncio==1.4.0 --with aiohttp==3.14.3 --with httpx==0.28.1 \
  python -m pytest tests/test_http_file_runner.py -q -p no:cacheprovider
```

**(b) 全量 · websockets 固定版**

```sh
cd <repo>
timeout -k 10 1800 uv run --no-project --python 3.12 \
  --with numpy --with rich --with "websockets==15.0.1" --with colorama --with pytest==9.1.1 \
  --with soundfile --with pytest-asyncio==1.4.0 --with aiohttp==3.14.3 --with httpx==0.28.1 \
  python -m pytest tests/ -q -p no:cacheprovider
uv run --no-project --python 3.12 --with "websockets==15.0.1" \
  python -c "import websockets;print('websockets',websockets.__version__)"
```

**(c) 全量 · websockets 浮动版**

```sh
cd <repo>
timeout -k 10 1800 uv run --no-project --python 3.12 \
  --with numpy --with rich --with websockets --with colorama --with pytest==9.1.1 \
  --with soundfile --with pytest-asyncio==1.4.0 --with aiohttp==3.14.3 --with httpx==0.28.1 \
  python -m pytest tests/ -q -p no:cacheprovider
uv run --no-project --python 3.12 --with websockets \
  python -c "import websockets;print('websockets',websockets.__version__)"
```

**(d) 残留进程扫描器**（脚本见 2.3）

```sh
python3 /tmp/e3rec/leakscan.py --self-check
python3 /tmp/e3rec/leakscan.py
```

### 2.2 结果表（本卡实测）

| # | 命令 | websockets 实测版本 | 退出码 | 耗时 | 末行摘要 |
|---|---|---|---|---|---|
| a | `pytest tests/test_http_file_runner.py -q` | 17.1 | `0` | 29s（pytest 自报 27.92s） | `15 passed, 36 warnings in 27.92s` |
| b | `pytest tests/ -q` | `websockets 15.0.1` | `0` | 161s（pytest 自报 159.64s） | `372 passed, 3 skipped, 127 warnings in 159.64s (0:02:39)` |
| c | `pytest tests/ -q` | `websockets 17.1` | `0` | 175s（pytest 自报 173.34s） | `372 passed, 3 skipped, 127 warnings in 173.34s (0:02:53)` |

3 个 skip 的理由由 `-rs` 单独跑出，全部**与 E3 无关**：

```
SKIPPED [1] tests/test_aligner_integration.py:53: ForceAligner 后端/模型未安装
SKIPPED [1] tests/test_aligner_integration.py:62: ForceAligner 后端/模型未安装
SKIPPED [1] tests/test_segmenter.py:208: 缺 silero-VAD 模型或 onnxruntime
```

`tests/test_http_file_runner.py` 一次 skip 都没有（`15 passed`，无 `s`）——
该模块的 `pytest.importorskip("aiohttp")` 与 `skipif(FFMPEG is None)` 两个门都过了。
模块顶层注释自述「只证明进程、队列、协议与存储契约，不冒充真实模型质量；引擎一律是
子进程里的 ProgrammableFakeEngine」，与本卡的观察一致。

### 2.3 残留进程扫描器及其自检方式

判据来源是 `/proc/<pid>/cmdline` 的**原始 NUL 分隔 argv**（含被 fork 的子进程），
不是 `ps` 的格式化回显。脚本本身不提交进仓，放在本卡的临时目录
`<tmp>/e3rec/leakscan.py`（下文命令里的 `/tmp/e3rec/leakscan.py` 即它）。
不自匹配：排除自身 pid、ppid，以及 argv 中含自身路径或
`leakscan` 的进程。目标串：`harness.server`、`harness/server.py`、`test_http_file_runner`。
退出码 `0`=无残留，`1`=有残留（打印 pid/argv），`2`=自检失败。

**自检（负样本先行，证明判据有约束力）**：脚本先 `subprocess.Popen` 一个 argv 里带
`# tests/test_http_file_runner.py::canary` 注释的 `sleep 30`，断言该 pid **必须**出现在
扫描结果里；不出现就退出码 2。本卡实测：

```
SELF-CHECK OK: pid=1882452 命中 -> /usr/bin/python3 -c import time; time.sleep(30)  # tests/test_http_file_runner.py::canary
selfcheck_rc=0
```

**已知为「否」时的读数**（在 pytest 正在跑时手工执行，扫描器如期变红，证明它不是恒真断言）：

```
LEAK pid=1875025 argv=timeout -k 10 420 uv run … pytest tests/test_http_file_runner.py -q …
LEAK pid=1875026 argv=uv run … pytest tests/test_http_file_runner.py -q …
LEAK pid=1875136 argv=<uv-cache>/builds-v0/.tmpBhU0aK/bin/python -m pytest tests/test_http_file_runner.py -q …
（另 4 个同形 pid）
残留进程 7 个
scan_rc=1
```

**三次跑完后的扫描结果**（均在本卡亲自执行）：

| 时点 | 输出 | 退出码 |
|---|---|---|
| (a) 目标文件跑完后 | `无残留进程` | `0` |
| (b) 全量 ws==15.0.1 跑完后 | `无残留进程` | `0` |
| (c) 全量 ws==17.1 跑完后 | `无残留进程` | `0` |
| 追加 `-rs` 全量跑完后 | `无残留进程` | `0` |

对照：`ps -eo pid,cmd | grep -c "[t]est_http_file_runner"` 在 (a) 之后为 `0`。

> 覆盖范围的实话：Linux 上 multiprocessing 默认 fork，子进程继承父进程 argv，
> 所以 SIGKILL 用例遗留的服务主进程 / Manager / 识别子进程确实带目标串、能被扫到；
> 但 `spawn` 启动法或 argv 不含目标串的孤儿，这个判据扫不到。

---

## 3. 预算与远端核对

- 实现卡整 PR 对基线 `2ab7650..29d843f` 的 `git diff --numstat` 合计
  （本卡实测 `awk '{a+=$1;d+=$2} END{print…}'`）：**added=1710, deleted=22, total=1732**。
  与任务卡写的 **预算 3500** 的关系：`1732 / 3500 ≈ 49.5%`，**在预算内**，余量约 1768 行。
- 业务代码（`core/`）在 4 个提交里合计 `6+8+519+57+54+14 = 658` 行新增、22 行删除。
- 远端核对（本卡实测 `git ls-remote origin`）：

```
29d843f952277d09d7ab48106a74cd907970cf8a	refs/heads/card/http-e3-runner-261002
```

  实现卡的 4 个提交确已推远端，SHA 与任务卡给的 Base commit 一致。
  `refs/heads/card/http-e3-record-261002` 在本卡开工前**不存在**（stdout 无匹配行），
  由本卡新建并推送。

---

## 4. 本卡未复验 / 报告无法证明的清单

以下各项本卡**没有**亲自跑过，**未由本卡复验**，不得当作已验证结论引用。

### 4.1 E3 卡要求、但报告无法证明的项（逐条去读了代码后的核对结果）

- **跨进程 Result 的完整字段断言 —— 存在，但不「完整」**。
  `tests/harness/worker.py::result_payload` 让子进程记录 15 个字段
  （`task_id/socket_id/owner_kind/type/duration/time_start/time_submit/time_complete/text/
  text_accu/tokens/timestamps/is_final/error_code/error_message`）；但
  `http_result_payload()` 持久化的只有 13 个（刻意不含 `error_code` / `error_message`）。
  测试里把持久结果与 worker 发出值**逐字段等值比对**的是 10 个：
  `task_id`、`type`、`owner_kind`、`socket_id`、`is_final`、`text`、`text_accu`、`duration`、
  `tokens`、`timestamps`。
  **`duration` 之外的 3 个时间戳 `time_start` / `time_submit` / `time_complete`
  只断言了 `isinstance(..., float)`，没有和 worker 发出的值做等值比对**
  （`tests/test_http_file_runner.py` 主路径用例第 1 组断言）。这 3 个字段的跨进程一致性
  本记录**不能**担保。
- **真实模型质量**：全部引擎是 `ProgrammableFakeEngine`，输出固定为 `text="正文"`、
  `tokens=["正","文"]`、`timestamps=[0.1,0.2]`。真实 sherpa/MLX 引擎一个字都没跑过，
  **未由本卡复验**。
- **ffmpeg 真实容器四格式样本**：mp3/aac/m4a/opus 四参数化用例本卡跑过（15 passed 含这 4 个），
  但样本是测试里用确定性正弦 PCM（`tone_pcm`）现场合成的 20s 音频，
  **不是真实语音的四格式样本**；识别质量维度未由本卡复验。
- **`ManagedHttpServerHarness` 的 `ffmpeg_shim` 参数是死路径**：`tests/harness/server.py`
  第 255/267/268/385/403 行定义了并在子进程里生效，但 `rg -n "ffmpeg_shim" tests/` 显示
  **没有任何测试传它**。argv 证据只来自主路径用例的 `monkeypatch.setenv("PATH", …)`
  变体，与真实跨进程服务主体无关。
- **300 秒墙钟 / 大文件 / `MAX_TASK_SECONDS` 上限**：无用例把样本推到
  `MAX_TASK_SECONDS`（默认 14400s）附近，`audio_too_long` 这条失败分支在
  `tests/test_http_file_runner.py` 里**没有对应用例**，未由本卡复验。
- **Windows / macOS**：本卡只在 Linux 跑；`requirements-server-macos.txt`、macOS 路径、
  `os.setsid()`（POSIX-only）在 Windows 上直接不可用，未由本卡复验。
- **生产部署**：本卡全程未启动 `start_server.py`、未起 systemd unit、未连真实模型；
  `config_server.py` 的真实生产路径未由本卡复验。
- **CI 绿灯**：本卡**没有**触发 GitHub Actions，只在本机按 `.github/workflows/ci.yml`
  的依赖清单复跑；Required Gate v2 模型主审从未在本卡跑过。
- **仓库 lint**：本卡未跑任何 lint / formatter。

### 4.2 预算与账本口径

- 本卡 `Diff-Lines-Target=0` / `Diff-Lines-Hard=0`，本卡的仓库内改动**只有**本文件
  （`docs/sessions/261001-http-files/reviews/E3-runner-implementation-record.md`，新建）。
  机器预算合同：`budget_target=0, budget_hard=0, budget_diff=None, budget_status=fail_loud`，
  且 `budget_base_sha=None` / `budget_head_sha=None`——**没有可比对的基线与头 SHA**，
  该合同在本卡**无法判定是否违约**，记为未能判定。
- 本卡未改任何业务代码、测试、CI、Gate 或其他文档；`git status --short` 在写本文件前为空。

---

## 5. 踩到的坑

1. **`uv run pytest` 在这个仓里是个陷阱**。仓根**没有 `pyproject.toml`**，
   裸 `uv run pytest tests/test_http_file_runner.py -q` 第一次跑出
   `1 skipped in 0.06s`、退出码 `5` ——看起来像「用例被跳过 / 环境不支持」，
   实际是空环境：`aiohttp` 与 `websockets` 双双 `ModuleNotFoundError`。
   正确形态在 `docs/development/testing.md` 里，用 `--no-project --with …` 显式列依赖。
   若照抄退出码 5 当成「本机缺 ffmpeg 所以跳过」，会得出完全错误的结论
   （本机 `which ffmpeg` 命中 `/usr/bin/ffmpeg`）。
2. **恒真断言伪装成通过**。b4b59b5 修掉的 `assert restarted.received == []` 就是这一类：
   `received` 是 `Manager().list()` 的 `ListProxy`，和字面量 `[]` 比较恒为假，
   「重启后不自动重跑」这条核心承诺在旧写法下**根本没有被验证**。
   本卡在 2.3 用同样的思路对自己的扫描器做了负样本自检。
3. **孤儿进程的判据要能扫到 fork 出来的孙进程**。服务主进程被 SIGKILL 后，
   它 fork 的 Manager 与识别子进程留在原进程组变孤儿；只 kill 主进程收不掉。
   b4b59b5 的解法（`setsid` + reap 之前先取 `pgid` 再 `killpg`）在 diff 里可直接看到，
   但注释里那句「reap 之后 getpgid 会查不到」是踩过坑才写得出来的顺序约束。

## 6. 绕过

1. **不复跑实现、不改代码**：本卡对 4 个提交只做 `git log` / `git diff` 读取，
   对测试只做复跑；仓库内唯一写入是本文件，符合 `docs-only-v1`。
2. **不引用上一轮的自述当结论**：b4b59b5 的 commit message 里写了「15 个用例现在 33s 内结束，
   残留进程 0」，本卡**没有**采信，而是自己跑出 27.92s 并逐次执行残留扫描（2.2、2.3）。
3. **绕过 uv 空环境**：不新建 venv、不装系统包，改用 `--no-project --with …` 一次性环境，
   免动仓库依赖文件，也免得和别的 worktree 抢 `.venv`。
4. **判据先喂已知答案**：所有「无残留」「无 skip」的结论都用同一批命令产出，
   扫描器先在 pytest 运行中如期报红（7 条）证明它不是恒真。

## 7. 偏差

1. **命令形态与任务卡写的不同**。卡里写 `setsid timeout -k 10 420 uv run … pytest …`，
   实际执行时用了 `timeout -k 10 420 uv run --no-project --with … python -m pytest …`
   并用 `setsid nohup bash <脚本>` 包住整条命令以脱离本会话。
   偏差原因：卡里的 `…` 未给出 `--with` 参数，而裸 `uv run` 在本仓是空环境（坑 1）。
   外层包裹未改变 pytest 自身的超时语义（`timeout -k 10` 仍在）。
2. **全量超时从 420s 放宽到 1800s**。实测全量需 ~175s，420s 本可覆盖；
   放宽是为两轮串行复跑留余量，实际未触发超时。
3. **`ffmpeg_shim` 死路径未清理**（见 4.1）。按「不新增没有第二消费者的抽象」，
   这条参数在 `tests/harness/server.py` 里已存在且无消费者，属实现卡遗留；
   本卡受 Scope-Globs 限制**没有**删，留给后续卡决策。
4. **主干基线不可用**（派发时 `gh api request failed`），因此：
   **「继承红」未能判定**；本卡**新红=无**（三次运行全部退出码 0，见 2.2）。
   - 继承红：**未能判定**（无基线可比）。
   - 新红：**无**。本卡引入的新物只有扫描器 `/tmp/e3rec/leakscan.py`（仓外），
     它自检退出码 0、实测命中已知样本、三次跑完均为 `无残留进程`。

## 8. 最贵一步

**本卡最贵的一步 = 在只有「4 个提交已推、零报告」的条件下，从 diff 反推出 12 个文件各自
做了什么，并把「上一轮说了什么」和「本卡验证了什么」彻底切开。**

具体贵在两处，都不是跑测试：

1. **逐 commit 读 diff 并区分「实现」与「补测」**。b62afc1 是纯红灯测试（只碰 `tests/`），
   b4b59b5 只碰 `tests/harness/server.py` 与测试文件——也就是说 4 个提交里**没有一个
   修过生产代码的跨进程挂死或孤儿泄漏**。上一轮的 commit message 把「跨进程挂死与孤儿泄漏」
   写进了标题，容易被读成生产代码有 bug；而 diff 显示修的是夹具。
   读出这个差别，靠的是对 4 个 commit 分别 `--stat` 后逐行读 hunk，不是读 message。
2. **去测试文件里核对「完整字段断言」到底完不完整**（4.1 第一条）。
   这是本卡唯一一处推翻乐观读法的地方：`duration` 比了等值，`time_start` /
   `time_submit` / `time_complete` 只比了类型。若不真的打开文件逐条比对字段，
   很容易把「逐字段一致」当成已验证写进报告。

跑测试本身不是最贵的：目标文件 29s，两轮全量各 ~3 分钟，加起来不到 7 分钟，
且一次通过、没有返工。相比之下，从零信息（没有上一轮报告）重建一份可核验的事实记录，
并把「能证明什么 / 不能证明什么」的边界划清，是本卡真正花时间的地方。

---

## 9. 仓库内公开记录的实际路径与 REPORT_PATH 对应

- 本文件（仓内公开记录）：
  `docs/sessions/261001-http-files/reviews/E3-runner-implementation-record.md`
- 任务卡要求在报告里把 `Verify-Command` 的 `REPORT_PATH` 替换为本次实际的 `$DELEGATE_REPORT_DIR`。
  **实测本次派发的 `DELEGATE_REPORT_DIR` 环境变量为空字符串**，因此按
  `$DELEGATE_REPORT_PATH` 的目录为准（该目录在 delegate 的 state 目录下，
  属派发运行时的现场，公开仓不写本机绝对路径）：

  ```sh
  $ echo "DELEGATE_REPORT_DIR='$DELEGATE_REPORT_DIR'"
  DELEGATE_REPORT_DIR=''
  $ dirname "$DELEGATE_REPORT_PATH"
  <delegate-state-dir>/20261002-133625-quick-ocgo-bunny-pi-CapsWriter-Offline-with-AI
  ```

- 对应的 Verify-Command（在本仓根执行）：

  ```sh
  cd <repo>
  test -s docs/sessions/261001-http-files/reviews/E3-runner-implementation-record.md
  ```

  本卡写出本文件后该命令返回码为 `0`（见提交后 `git cat-file` 复核）。

**派发与实现卡标识**：本卡 dispatch `dlg-20261002-133618-2d23e7`（`ocgo-bunny-pi`，
verify-mode `docs-only-v1`，base `29d843f`，分支 `card/http-e3-record-261002`）；
被补写的实现卡 dispatch `dlg-20261002-124436-9ed5d9`（Task-Id
`CapsWriter-Offline-with-AI-20261002-20`，该卡 exit 0 但未产出 `report.md`）。