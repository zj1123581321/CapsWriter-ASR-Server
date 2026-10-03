# M4 补充设计（修正版）：HTTP 资源边界与清理实施计划

基于 `e066930` 主干只读核查 + master CI run `37113170289` 实测数据。**本文替代上一版全部结论**；上一版在 commit `f11587d`，此处逐条更正。

> **来源与实施状态（写入方：M4-C1 实施分支）**
> 规划来源 commit：`7ab3551`（Task-Id `CapsWriter-Offline-with-AI-20261003-06`）。
> C1（容量三闸 + 结果/WAL 预留）已在本分支落盘，其真实字节证据见
> `tests/test_http_capacity.py::test_result_reservation_covers_real_sqlite_peak`。
> **C2（周期终态源清理）尚未开始**：§3 里的「到期终态源删除」「先 unlink 后记释放」目前
> 只有 `source-presence` 口径这一半被 C1 实现（源被删除后预留确实退出），删除动作本身
> 仍不存在，本文 §5 的 T1–T13 全部未实现，不得当已交付。
> 本卡对本文的更正只限状态码措辞与本状态说明，不改任何已锁定的口径。

## 0. 对上一版的更正（10 条）

| # | 上一版错在哪 | 修正 |
|---|---|---|
| 1 | 断言「CI 未装 ffmpeg、解码证据整体 skip」 | **实错**。`ci.yml` 安装步骤有 `sudo apt-get update && sudo apt-get install -y ffmpeg`（我上次用 grep 只看 pip 行，滤掉了 apt 行）。master run `37113170289`（headSha=e066930）实测 **425 passed, 3 skipped in 228.99s**，ffmpeg 链在跑。所有「装 ffmpeg 前置卡」删除。 |
| 2 | 断言三闸都该用 `>=` 拒收等值点 | **实错**。`_check_create_admission` 的 `declared + size_bytes > SOURCE_RESERVE_BYTES` 与 `free < FREE_SPACE_MARGIN_BYTES` 都让等值合法，与「最多 16 GiB」「至少留 2 GiB」契约一致。见 §2 精确量纲。 |
| 3 | 把「删除源后释放 16 GiB 预留」列进已否决 | **实错**。长期服务会因永不释放的占额静默永久满。到期终态源安全删除后**必须**释放源预留，元数据/结果/外部身份不变。见 §3。 |
| 4 | 把「partial 源文件是否删」列为待主脑拍板 | **已裁决**：保守不自动删 partial 物理文件；但 16 GiB 与物理容量口径**必须覆盖留在磁盘的 partial**，不得靠 `state != EXPIRED` 排除而漏记。终态源按 `jobs.terminal_at` 起算 7 天，不新增配置项。 |
| 5 | 「Job 数 × 64 MiB」当完成 | 不成立——缺 R7 明写的 WAL 峰值，且只覆盖 result。另：capacity 只在 create 读一次 free，后续 PATCH 无预算。修正见 §4。 |
| 6 | T7 写「FAILED 删源后结果仍 200」 | **实错**。`get_result` 对 FAILED 抛 409 `job_failed`（`http_store.py:646`）。DONE→200、FAILED→409+`error_code`，分别锁死。 |
| 7 | T8 用纯 RUNNING 构造活跃引用窗口 | **恒真**。RUNNING 无 `terminal_at`，本就不是候选。真实窗口见 §5 T8。 |
| 8 | 三态表「测试覆盖」隐含「改坏必红已验证」 | 改为「有行为断言」；除已保存的反向证据外不宣称改坏必红。64 KiB/read 等证据下沉到 M6。 |
| 9 | 称 C1/C2「物理必串行」 | 修正：同文件不等于必须串行。并行仅限独立 worktree 内的读与规划；**实现合入按实际数据/接口依赖串行**（见 §6）。 |
| 10 | 称 M7「环境完全不存在」 | 修正。模型/音频不在 git 是正常现实，来自用户本地设备目录。本轮不 SSH、不读录音，只把「已获授权的隔离验证环境与样本」列为待证前提。不新建三平台 CI runner，不为拿三个绿而生产部署。 |

## 1. R7 逐项映射（代码存在 / 有行为断言 / 缺证据）

「有行为断言」只说明仓内有对该行为的断言，**不等于**已保存反向注入的红验证据。

| R7 量 | 生产入口 | 消费者 | 状态 |
|---|---|---|---|
| 1 GiB/file | `http_store.validate_identity`:824 | `POST /v1/uploads` | 代码存在；有行为断言（`test_http_file_tasks` L742/L541） |
| 1 MiB/PATCH | `append_bytes`:515 | `PATCH` | 代码存在；有行为断言（L758） |
| 64 KiB/read | `READ_CHUNK_BYTES`:39、`_read_body`:411 | 两条 body 路径 | 代码存在；**缺证据**（归 M6） |
| 16 KiB JSON | `http_server`:40 | create | 代码存在；有行为断言（L764） |
| 16 handler / body 2 / mailbox 32 | `_handler_slots`:147、`_body_slots`:148、`HttpIoWorker._mailbox`:90 | route 包装 / 每次 store 调用 | 代码存在；有行为断言（L568、L1047、L169） |
| 32 未完成上传 | `_check_create_admission`:409 | create | 代码存在；有行为断言（`test_http_store` L387） |
| 共享总量 + WS 预留 2 | `state.count_active_tasks` + `admission_lock` | WS 首帧 + HTTP commit 同锁 | 代码存在（M3 已合）；有行为断言（L325/L1243/L1427/L1464/L1580） |
| 4 在途段 / 解码器 1 / idle 300s / `CW_MAX_TASK_SECONDS` | `begin_task`:464、`_run_under_gate`:450、`_read_idle_seconds`:397、`_check_samples_limit`:274 | runner / `_read_body` | 代码存在；有行为断言；**改坏必红未逐条验证**（归 M6） |
| 上传 TTL 7 天 | `UPLOAD_TTL_SECONDS`:49、创建:483、PATCH 续期:552 | `_row_for_token`:355 惰性过期 | 代码存在（惰性）；有行为断言（L362）；**无周期任务** |
| source 16 GiB | `_check_create_admission`:414 | create | 代码存在；**漏记**：口径 `state != EXPIRED` 排除了仍在磁盘的 partial（见 §3） |
| DB+WAL+SHM 2 GiB | `_db_bytes`:344 + :417 | create | 代码存在；**缺证据**（零行为断言） |
| result 64 MiB | `record_result`:745、runner:217 | 结果 sink | 代码存在；有行为断言（`result_too_large`） |
| **待处理 result + WAL 预留** | **不存在** | — | **缺证据**（R7 明写，未实现） |
| physical free 2 GiB | `_free_bytes`:352 + :419 | create | 代码存在；**缺证据**；且 PATCH/commit 路径不查（见 §4） |
| 终态源 7 天清理 | **不存在** | — | **缺证据**（M4 主体） |

## 2. 精确量纲：等值侧按额度/余量/槽位三类分开

上一版把三类混成一条 `>=`，是错的。

- **额度类**（允许用满，等值合法）：`已占 + 新增 > 上限` 才拒。→ source 16 GiB、DB guard 2 GiB（C1 加预留后仍是 `>`）。等值点**必须放行**。
- **余量类**（至少保留，等值合法）：`可用 < 需留` 才拒。→ physical free 2 GiB，恰好 2 GiB 合法。
- **槽位类**（占位即满，等值即拒）：`计数 >= 上限` 即拒。→ 32 未完成上传、16 handler、body 2、mailbox 32。

`GET /v1/jobs` / `result` / 幂等重放**不分配新预算**，任何容量状态下都必须可用。

## 3. source-presence 口径（C1 与 C2 共用定义）

- 预留口径 = `SUM(uploads.size_bytes) WHERE 源文件仍在`。当前 `WHERE state != 'EXPIRED'` 把留在磁盘的 EXPIRED partial 排除在外 → **静默漏记**，与「物理容量与 16 GiB 必须覆盖保留的 partial」冲突。
- **释放条件**：到期终态源（`jobs.terminal_at + 7d < now`、job 为 DONE/FAILED、无活跃 runner 引用）被安全删除后，相应 `size_bytes` 必须退出预留；否则长期服务会永久满。
- **记录方式由 C2 决定**（列标记 / 由终态时间与文件存在性联合判定），本设计不强制抽象；不新增资源表。
- **崩溃一致顺序：先 unlink，后提交释放记录。** unlink 成功而记录未提交 → 仍计费（多计，保守方向）；反序（先记释放再 unlink）会漏记，禁止。测试须在两步骤之间注入崩溃，断言重启后**不多计也不漏计**，且外部身份（upload_id/job_id/token/结果）不变。
- partial（UPLOADING→EXPIRED）本轮**不自动删物理文件**；周期任务可持久标 EXPIRED 并让入口返 410。

## 4. C1 容量与结果/WAL 预留（独立可部署增量）

生产 `core/server/http_store.py`；测试 `tests/test_http_store.py` + `tests/test_http_file_tasks.py`。**不改 route、不改 runner、不改 app.py。**

1. **DB guard 补预留**：判定改为 `_db_bytes() + pending_reservation > DB_GUARD_BYTES`（等值放行）。`pending_reservation = 待处理 Job 数(QUEUED+RUNNING) × (MAX_RESULT_BYTES + WAL_HEADROOM)`。`WAL_HEADROOM` 取结果 payload 的 2 倍作为「checkpoint 前 WAL 同时持有旧页与新页」的保守上界，写成模块常量，**不新增配置项**。数据只来自既有 `jobs`/`results` 查询，不开第二本账。
2. **真实字节对照证据**：用缩小后的模块常量（monkeypatch）造接近上限的多个真实结果，跑到真实 checkpoint，比对「真实 DB+WAL+SHM 字节」与「预留上界」，断言**预留 ≥ 真实字节**（上界不吃亏）。不真造 2 GiB。
3. **source 预留改 source-presence 口径**（§3），去掉按状态排除。
4. **PATCH 也查物理余量**：`append_bytes` 落盘前查 `disk_usage().free - (未写入尾 = size_bytes - confirmed_offset) - pending_reservation` 是否仍 ≥ 2 GiB（余量类，等值放行）；不足 → 507，**不写、不 ACK、offset 不变**。commit 的 source 校验前同样查一次。
5. **不挡查询**：幂等重放（重复 commit → 200 同一 Job）、`job_record`（200）、`get_result` 都不加闸。容量满时它们仍按原状态码可用；注意 FAILED 的 result 恒为 409 `job_failed` + `error_code`，DONE 的 result 才是 200 完整结果（与 §5 T7 同一口径）。
6. **查不到≠有容量**：`disk_usage`/`os.stat`/SQLite 抛错必须显式失败（拒绝受理），禁止 `except` 吞成放行。
7. 阈值三点测试（`额度-1 / 额度 / 额度+1`、余量 `<2GiB / ==2GiB / >2GiB`）必须**按 §2 的类分别写**，等值点断言放行。

命令：`python -m pytest tests/test_http_store.py tests/test_http_file_tasks.py -q`；全量 `python -m pytest tests/ -q`。

## 5. C2 周期终态源清理（独立可部署增量）

生产 `core/server/http_store.py`（候选查询 + 释放记录）、`core/server/http_server.py`（周期任务）。**`app.py` 暂不改**：`HttpServer.serve()` 在 `_runner.setup()` 后起周期任务、`stop()` 里取消即可，主脑要求的「先证真实调用方」结论是：`app.py` 只调 `serve()`/`stop()`，现有链能消费，故不改。

清理只允许：到期终态源、且无活跃 runner 引用。禁止删 jobs/results/元数据、禁止删未登记残留（`sources/junk.bin` 必须在）、禁止因容量不足顺手删东西。

**测试计划**（每条含真实入口 + 改坏会红的判据）：

| ID | 不变式 | 真实入口与断言 | 改坏什么会红 |
|---|---|---|---|
| T1 | 上传 TTL：创建起算、PATCH 续期、GET 不续期 | 真实 POST + 真实 PATCH 字节体后直读 SQLite `expires_at`；GET 后逐字段精确比对 | 删掉 PATCH 续期 → T1a 红；GET 走续期 → T1c 红 |
| T2 | 过期限 → 410 `upload_expired`，元数据留 | 注入时钟越界，真实 GET 得 410；关库重开再得 410 | 重启重置 `expires_at` → 红 |
| T3 | 活跃 PATCH I/O 期间清理不删正在写的文件 | 屏障卡住 I/O worker 真实写入 → 触发清理 → 释放后核对磁盘字节 + DB offset + 204 ACK 三者一致 | 清理不看活跃写入就 unlink → 不一致红 |
| T4 | 周期任务幂等 | 手动触发两次真实周期任务 | 第二次报错/重复删 → 红 |
| T5 | DONE 未满 7 天不删 | `terminal_at` 改到 6 天前 → 清理 → 文件在、`source_available=true` | TTL 误用 `created_at` → 红 |
| T6 | **DONE 满 7 天且无引用**：源删、结果仍可领 | 真实 ffmpeg 容器跑完 DONE → 改 `terminal_at` 过期 → 清理 → 源文件消失；`GET /v1/jobs/{id}/result` 仍 **200 且 payload 完整**；`source_available=false` | 清理连 jobs/results 删 → 红；周期任务没装配 → 红 |
| T7 | **FAILED 满 7 天且无引用**：状态与错误码不变 | 造真实 FAILED（解码失败路径）→ 过期 → 清理 → 源文件消失；`GET /v1/jobs/{id}` 仍 `FAILED` + `error_code`；`GET .../result` 仍 **409 `job_failed` + `error_code`**（不是 200） | 删源后把 FAILED 当 DONE 返回 200 → 红 |
| T8 | **真实终态-收尾窗口**：终态已落库、runner 仍有引用时不删 | 用真实 sink + 屏障卡在 `record_result` 已提交之后、`_on_done` 弹出 `self._jobs[job_id]` 之前的真实窗口（`http_file_runner.py` 的 `_execute` finally 仍在 await `decoder.close()`）→ 跑清理 → 文件在；释放后再跑一次 → 文件消失 | 清理不查 `runner.active_jobs` 就在该窗口删源 → 收尾路径报错红 |
| T9 | 崩溃一致释放（§3） | 在 unlink 与释放记录之间注入崩溃 → 重启 → 预留不多计也不漏计；外部身份不变 | 反序提交 → 漏记红 |
| T10 | 释放后容量真的放开 | 过期终态源清理后重发同一 create → 201（源预留已回落） | 释放只改标记不改口径 → 仍 507 红 |
| T11 | 未登记残留不删 | 放 `sources/junk.bin` → 跑清理 → 仍在 | 扫目录即删 → 红 |
| T12 | 查不到≠有容量 | monkeypatch `disk_usage`/`stat`/`execute` 抛错 → 必须报错/507 | `except: pass` → 放行红 |
| T13 | 旧 WS 回归 | `tests/test_backpressure.py`、`test_scheduler.py`、`test_http_release_invariant.py` 全绿 | HTTP 改动破坏 WS 默认 → 红 |

生产者素材真实存在：`tests/harness/server.py`（真子进程）、`fake_engine.py`（假引擎但真 `Task.data` 消费）、`test_http_file_runner.py` 的 `install_recording_ffmpeg`（真 argv/env 记录）、`sdk/capswriter_asr/http_client.py`（真 HTTPX 客户端）。用户可见 E2E（`source_available` true→false、410、删源后领取）**随 C1/C2 实现卡交付，不单开纯验收卡**。

## 6. 合入顺序与并行

C1 与 C2 都改 `http_store.py`，但**不存在接口消费依赖**：C2 只消费「到期终态源」这一既有事实，不调用 C1 的新预留函数。两者可在独立 worktree 内并行规划与实现；**合入按实际数据/接口依赖串行**，顺序 C1 → C2（先有容量口径与余量检查，再让清理去释放它）。这不是一支笔原则的泛化——一支笔只约束同一时刻的写入者。

剩余资源边界（64 KiB/read、4 在途段、解码器峰值=1、idle 300s 等）的反向红验归 **M6**，不在 M4 单开卡；只在 M6 预算不足时另议。

## 7. 环境与待证前提

| 项 | 实测/现状 |
|---|---|
| CI 全量耗时 | run `37113170289`：425 passed, 3 skipped, **228.99s**（另一矩阵 222.47s）。真实 ffmpeg 编码与子进程重启用例是主要成本。3 skipped 的具体身份未取到（CI 未开 `-rs`），**未知** |
| CI ffmpeg | **已装**（apt），解码链在跑。无前置卡 |
| 本机环境 | 有 ffmpeg/ffprobe、**无 aiohttp** → 本地只能收集 361 项，`test_http_file_runner.py` 与 `test_http_file_tasks.py` 在本机**收集为 0**。C1/C2 的 HTTP 层验证须在装了 `aiohttp==3.14.3` + `httpx==0.28.1` + ffmpeg 的环境跑（CI 已具备） |
| 无会话裸 shell | 未跑，**未知**；M6 需补一次 |
| 进程托管 | 以 `deploy/README.md` 的既有方式为准（`update.sh`/`update.ps1`/`pm2.ecosystem.config.js.example`）。不以「无 systemd unit」阻塞 M7 |
| M7 模型/样本 | 不在 git 属正常，来自用户本地设备目录。本轮不 SSH、不读录音。列为**待证前提**：需已获授权的隔离验证环境与代表性样本；不自动新建三平台 CI runner，既有机器按部署约定隔离验证即可；**不得为拿三个绿而生产部署** |
| 生产发布 | 需单独授权；M4 全部增量不部署 |

## 8. 决策与已否决

决策：(1) 额度/余量/槽位三类分别定等值侧，禁止统一 `>=`；(2) source 预留按 source-presence 口径，删除后必须释放，先 unlink 后记释放；(3) partial 不自动删物理文件，但必须计入容量；(4) 终态源按 `jobs.terminal_at` + 7 天，无配置项；(5) 预留上界含 WAL 头寸，常量不加配置，数据只查既有表；(6) PATCH/commit 也查余量，重放与查询不受容量影响；(7) 清理由 `HttpServer.serve/stop` 内的真实周期任务消费，不改 app.py；(8) 两卡实现可并行、合入按依赖串行。

> C1 实测更正（第 5 条的数字）：「WAL 2 倍头寸」经真实 sqlite3 实测**不成立**——
> `page_size=4096`、真实 63.99 MiB 合法完整结果、单次提交的真实增量 134,922,696 B
> （≈2.0105×payload），比 `2 × 64 MiB = 134,217,728 B` 高 704,968 B。所以它是接近的猜测，
> 不能当上界。实现改按 SQLite 页几何算（WAL 每帧 `page_size+24`、溢出页每页净装
> `page_size-4`、同事务非结果页余量、5% 安全系数），生产值 141,902,242 B（2.1145×）。

已否决：全协议横向盘点；结构检查/同源计数当行为主证据；单点内存计数；静默只数内存；自动 retry/重跑；删未登记残留；删 jobs/results；把「额度等值合法」改成 `>=` 拒收；先记释放再 unlink；为 source 可用性新增资源表或通用资源账本；用 pytest fixture/常量 grep 宣称「改坏必红已验证」；把 C1/C2 说成物理必串行；以「CI 缺 ffmpeg」为 M6 前置；以 systemd unit 缺失阻塞 M7；单开纯验收实现卡；为拿三个绿而生产部署。