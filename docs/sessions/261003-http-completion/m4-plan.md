# M4 补充设计：HTTP 资源边界与清理实施计划

本文只规划，不含实现。基于 `e066930` 的只读代码核查（主脑运行时主干）。三态标注口径：
**代码存在**＝生产代码里确有该逻辑；**测试覆盖**＝本仓有会因改坏它而红的测试；**缺证据**＝只有代码或只有测试名，无真实边界证据。

## 1. R7 逐项映射（生产入口 → 消费者 → 现存测试）

| R7 量 | 生产入口（文件:行） | 真实消费者 | 状态 |
|---|---|---|---|
| 1 GiB/file | `http_store.validate_identity` :824 → 413 | `POST /v1/uploads` → `http_server._create_upload` | 代码存在；测试覆盖（`test_upload_identity_and_limit_validation` L742，413）；1 GiB 真实字节未造 |
| 1 MiB/PATCH | `http_store.append_bytes` :515 | `PATCH /v1/uploads/{id}` | 代码存在；测试覆盖（同上 + `test_negative_matrix_keeps_old_bytes` L504）；缺「恰好 1 MiB+1」边界断言 |
| 64 KiB/read | `http_store.READ_CHUNK_BYTES` :39、`http_server._read_body` :411 | 两条 body 读取路径 | 代码存在；**缺证据**（无测试按 64 KiB 断言分块次数） |
| 16 KiB 小 JSON | `http_server` 导入 :40 | `POST /v1/uploads` | 代码存在；测试覆盖（L764 413）；缺边界等值侧 |
| 16 handler | `http_server._handler_slots` :147 | 全部 route 包装 `_wrap` :312 | 代码存在；测试覆盖（`test_body_and_handler_admission_rejects_without_waiting` L568，17 条真实 TCP） |
| 同时 body 2 | `http_server._body_slots` :148 | `_read_body` | 代码存在；测试覆盖（L568、L1047 半开 body） |
| mailbox 32 | `HttpIoWorker._mailbox` :90、`IO_MAILBOX` | 每个 store 调用 | 代码存在；测试覆盖（L169 第 33 个被拒） |
| 32 未完成上传 | `_check_create_admission` :409 | `create_upload` | 代码存在；测试覆盖（`test_admission_refuses_new_upload_without_touching_old_data` L387） |
| 共享总量/`max_tasks`、WS 预留 2 | `state.count_active_tasks` + `admission_lock` | WS 首帧 + HTTP commit 同一把锁 | 代码存在（M3 已合，`e066930`）；测试覆盖（L325/L1243/L1427/L1464/L1580） |
| 每任务 4 在途段 | `begin_task(state, key, Config.max_inflight_segments)` :464 | `_submit` :508 | 代码存在；测试覆盖（`test_http_file_runner.py`）；缺「4 段在途屏障」独立用例 |
| HTTP 解码器 1 | `HttpFileRunner._run_under_gate` :450 | `_execute` | 代码存在；测试覆盖（`ffmpeg_peak_concurrency`）；缺峰值=1 的反向红验 |
| 300 s 空闲 | `_read_idle_seconds` :397 复用 `CW_UPLOAD_IDLE_SECONDS` | `_read_body` | 代码存在；测试覆盖（L951/L1109 真实屏障）；真实 300 s 时长未跑 |
| 上传 TTL 7 天 | `UPLOAD_TTL_SECONDS` :49，创建 :483 / PATCH :552 续期 | `_row_for_token` 懒过期 :355 | 代码存在（**惰性**：只在请求路径触发）；测试覆盖（`test_expired_upload_is_410_and_metadata_kept` L362）；**无周期清理任务** |
| source 16 GiB 预留 | `_check_create_admission` :414 | create/commit | 代码存在；**零测试**（`SOURCE_RESERVE_BYTES` 全仓无测试引用） |
| DB+WAL+SHM 2 GiB | `_db_bytes` :344 + :417 | create | 代码存在；**零测试** |
| result 64 MiB | `record_result` :745、runner `MAX_RESULT_BYTES` :217 | 结果 sink | 代码存在；测试覆盖（`test_http_file_runner.py` `result_too_large`） |
| 待处理 result 预留峰值 | **不存在** | — | **缺证据**（R7 要求，未实现） |
| physical free 2 GiB | `_free_bytes` :352 + :419 | create | 代码存在；**零测试** |
| 源音频 7 天终态清理 | **不存在**（无周期任务、无 unlink 路径） | — | **缺证据**（M4 主体） |
| `source_available` / 410 | `job_record` :625 / `_row_for_token` :378 | `GET /v1/jobs/{id}` | 代码存在；测试覆盖（L362）；删源后取值无覆盖 |

**结论**：R7 的「请求侧限额」基本落地且有真实 HTTP 证据；缺口集中在三处——
① 三个容量闸（16 GiB / DB 2 GiB / free 2 GiB）**零测试**；② **待处理 result 预留未实现**；
③ **周期清理完全不存在**（上传 TTL 只有惰性过期，源音频零清理）。

## 2. 拆卡建议（每张 diff ≤ 3500 行，TDD）

前置：C1 与 C2 都改 `core/server/http_store.py`，同仓一支笔，物理串行；C2 与 C4 无接口依赖。

### C1 · 容量三闸 + 待处理结果预留（store 层）
- 生产：`core/server/http_store.py`（`_check_create_admission`、新增 `_pending_result_reservation`）；测试：`tests/test_http_store.py`。
- 范围只做「判定」：把 DB guard 改为「已用 DB 字节 + 待处理 Job 数 × 单 Job 结果预留」不超 2 GiB；free 闸用真实 `shutil.disk_usage`（测试用真实 tmpfs/loop 挂载或 monkeypatch 到真实 `os.statvfs`）。
- 不动 HTTP route、不动 runner。
- 命令：`python -m pytest tests/test_http_store.py -q`。
- 预计耗时来源：3 个阈值测试要造大文件（2 GiB 真实字节不现实 → 用「把库真实写到阈值附近」的 monkeypatch 记字节函数 + 1 个真实小规模 free 闸断言）；耗时主要在 ffmpeg/soundfile 无关的纯 SQLite 提交。精确耗时未知。

### C2 · 周期清理任务 + 源音频终态清理（主路径）
- 生产：`core/server/http_store.py`（`cleanup_due()`：返回候选并删源文件）、`core/server/http_server.py`（周期 asyncio 任务，进 `serve()`/`stop()` 监督链）、`core/server/app.py`（装配/停机顺序）；测试：`tests/test_http_file_tasks.py`、`tests/test_http_store.py`。
- 消费方必须是**真实周期任务**（`http_server` 里注册的 loop task），不得只测 `cleanup_due()` 这个库函数。
- 消费方活跃引用保护：清理时必须排除 `runner.active_jobs`（`http_file_runner.py:340`）与 `state.tasks` 非终态记录；这些是真消费方，不是同源计数。
- 命令：`python -m pytest tests/test_http_file_tasks.py tests/test_http_store.py -q`。

### C3 · 清理与容量的端到端用户可见证据
- 生产：无新增（只允许修 C1/C2 暴露的 bug）；测试：`tests/test_http_file_tasks.py`（新增）、`tests/test_e2e_sdk_server.py` / `tests/test_sdk_client.py`（复用 `sdk/capswriter_asr/http_client.py` 真客户端）。
- 证明 `source_available` 由 true→false、过期 410、删源后 `GET /v1/jobs/{id}/result` 仍 200 完整结果、重启后期限不重置。
- 命令：`python -m pytest tests/test_http_file_tasks.py tests/test_e2e_sdk_server.py -q`。

### C4 · 剩余 R7 单位的反向红验与旧 WS 回归
- 生产：无（或仅补 `http_file_runner.py` 的段数/解码器上限回读）；测试：`tests/test_http_file_runner.py`（每任务 4 在途段、解码器峰值=1、`CW_MAX_TASK_SECONDS` 实际采样数拒绝）、`tests/test_segmentation_contract.py`。
- 命令：`python -m pytest tests/test_http_file_runner.py tests/test_segmentation_contract.py -q`。

### 全量命令
`python -m pytest tests/ -q`（与 `.github/workflows/ci.yml` L36 一致）。耗时来源：`test_http_file_runner.py` 真实 ffmpeg 编码四种容器 + 子进程 server 重启用例是主要成本；本机耗时未知，CI 上 ffpmeg 缺失时该文件整体 skip（见 §6）。

## 3. 最小保护形态与不变式轴表

保护面只有三处：`uploads.state/expires_at`、`jobs.state/terminal_at`、`sources/<uuid>.bin`。不新建资源框架、不加新的资源表。

**族 A 上传 TTL**（轴：起算来源 × 期限前后 × 活跃 I/O × 重启）
| # | 组合 | 不变式 |
|---|---|---|
| A1 | 创建 → 未 PATCH → 未过期 | `expires_at = created_at + 7d`，源文件在 |
| A2 | 成功 PATCH → 未过期 | `expires_at = patch 时刻 + 7d`（`http_store.py:552`） |
| A3 | 仅 GET → 未过期 | `expires_at` **逐字节不变** |
| A4 | 越过期限 | state→`EXPIRED`；GET/PATCH/commit 均 410 `upload_expired` |
| A5 | 越过期限 **且有活跃 PATCH I/O** | 该次 PATCH 的 ACK/offset 语义不被清理撤销；清理不得删正在写的文件 |
| A6 | 重启后再看 | `expires_at` 不被重置；重启不复活 `EXPIRED` |
| A7 | 周期清理跑多次 | 幂等；第二次不报错、不重复 unlink |

**族 B 终态源音频**（轴：起算 × 活跃引用 × job 状态 × 重启）
| # | 组合 | 不变式 |
|---|---|---|
| B1 | `DONE` + `terminal_at` 未满 7 天 | 源文件保留，`source_available=true` |
| B2 | `DONE` + 满 7 天 + 无活跃 runner 引用 | 源文件删除；**jobs/results 行不动**；`source_available=false`；结果仍 200 |
| B3 | `FAILED` + 满 7 天 + 无引用 | 同 B2（`error_code` 仍可读） |
| B4 | 满 7 天 + **runner 仍持有该 job**（解码未结束） | 不删；本轮跳过，下轮再判 |
| B5 | `QUEUED`/`RUNNING`（无 `terminal_at`） | **永不删**（重启后已被 `_converge_restart` 置 FAILED，故 B3 随后接管） |
| B6 | 删源后重启 | `source_available` 仍 false，结果仍可领取；不复活文件 |
| B7 | 未知/未登记的 `sources/` 残留文件 | **不删**（R7 明令） |

**族 C 容量**（轴：阈值前后 × 资源种类 × 并发）
| # | 组合 | 不变式 |
|---|---|---|
| C1 | source reservation 恰好等于/超出 16 GiB | `>=` 即 507 `source_reserve_full`，不新建 upload 行、不建空文件 |
| C2 | DB+WAL+SHM 字节跨 2 GiB | 507 `storage_guard_full`；DB guard 含 WAL/SHM 三件套真实 `st_size` |
| C3 | 待处理 Job 结果预留跨 2 GiB | 507，且与 C2 同一口径（见 §5 歧义 4） |
| C4 | `disk_usage().free` 低于 2 GiB | 507 `disk_guard_full` |
| C5 | 并发 8 个 commit 同时越限 | 至少 1 个 507，其余正常；DB 中 Job 行数 == 返回 202 的数量（无凭 ACK 的 Job） |
| C6 | 并发 PATCH 期间跑清理 | 清理不删 A5 保护的文件；ACK 与磁盘字节、DB offset 三者一致 |
| C7 | 清理释放空间后重试 create | 容量闸重新放行（证明闸不是静态开关） |

**显式未决/失败（不得用「查不到」推结论）**：`shutil.disk_usage`、`os.stat` 或 SQLite 查询抛错 → 拒绝受理并报明确 5xx/507，**不得**把异常吞成「有容量」。这条必须写成负向测试（见 T9）。

## 4. 行为测试计划（每条含真实边界与反向红验）

原则：断言必须落在**真实 HTTP 请求 payload、真实源文件字节、真实 SQLite 行、真实时钟与真实屏障**上；结构检查/同源计数不算主证据。

| ID | 不变式 | 真实入口与断言 | 改坏什么会红（须 AssertionError） |
|---|---|---|---|
| T1 | A1/A2 | 真实 `POST /v1/uploads` + 真实 PATCH 字节体 → 直接读 SQLite `expires_at`（非 HTTP 回显） | 把 `append_bytes` 的续期删掉 → A2 断言红 |
| T2 | A3 | 真实 GET 后逐字段比对 `expires_at` 精确值（注入可控时钟，不 sleep 7 天） | 让 GET 走续期分支 → 红 |
| T3 | A4/A6 | 注入时钟越界 → 真实 GET 得 410 `upload_expired`；**关库重开**再得 410 | 重启重建 `expires_at` → 红 |
| T4 | A5/C6 | 屏障卡住 I/O worker 的真实写入 → 触发周期清理 → 释放后核对磁盘字节 + DB offset + 204 ACK | 清理不看活跃写入就 unlink → 字节/offset 不一致红 |
| T5 | A7 | 手动触发两次真实周期任务 | 第二次报错或重复删 → 红 |
| T6 | B1 | 把 `terminal_at` 改到 6 天前 → 跑清理 → 文件仍在、`source_available=true` | TTL 改成按 `created_at` 或无 7 天 → 红 |
| T7 | B2/B3/B6 | 真实容器（ffmpeg 生成）跑完整 DONE 与 FAILED 两条 → 改 `terminal_at` 过期 → 清理 → 源文件消失，`GET result` 仍 200 完整 payload | 清理连 jobs/results 一起删 → 红；跳过清理任务装配 → 红 |
| T8 | B4 | 屏障卡住真实 runner 解码（ffmpeg shim 记录 argv）→ 清理周期到达 → 文件在；释放后再跑一次 → 文件消失 | 清理不查 `runner.active_jobs` → 会在解码中删源，ffmpeg 非零退出 → 红 |
| T9 | C1–C4 精确阈值 | 真实 HTTP create/commit 打到 `阈值-1 / ==阈值 / 阈值+1` 三点，断言 HTTP 状态码**且**断言 SQLite 无新增行、sources 目录字节数不变 | 判据写成 `>` 而非 `>=` → 等值点红 |
| T10 | C7 | 让 free 闸用真实 tmpfs 目录，跑 `df` 取真实 free，填到阈值附近 → 断言 507 → 删临时文件后同一请求 201 | 闸读错目录或忽略真实 free → 红 |
| T11 | C5/C6 并发 | 8 个真实并发 commit（真实 TCP、真实 barrier）→ 数 SQLite `jobs` 行数 == 202 数；并发 PATCH 与清理交错 | 无锁/丢登记 → 行数 > 202 红 |
| T12 | 显式失败 | monkeypatch `shutil.disk_usage` / `os.stat` / `conn.execute` 抛 `OSError`/`sqlite3.Error` → 必须抛错或 5xx/507 | 用 `except: pass` 吞掉 → 请求被放行 → 红 |
| T13 | B7 | 手工放一个未登记的 `sources/junk.bin` → 跑清理 → 文件仍在 | 清理扫目录即删 → 红 |
| T14 | 旧 WS 回归 | `tests/test_backpressure.py`、`test_scheduler.py`、`test_http_release_invariant.py` 全绿 | HTTP 改动破坏 WS 默认 → 红 |

T4/T8/T10/T11 的 producer 素材真实存在：`tests/harness/`（`server.py` 真子进程、`fake_engine.py` 假引擎但真 `Task.data` 消费）、`tests/test_http_file_runner.py` 的 `install_recording_ffmpeg`（真 argv/env 记录）、`sdk/capswriter_asr/http_client.py`（真 HTTPX 客户端）。

## 5. 清理语义与规格歧义

清理**只**允许：① state 已 `EXPIRED` 的登记 partial 源文件；② `COMMITTED` 且 job 终态、`terminal_at` 已满 7 天、无活跃 runner/内存引用的源文件。
清理**禁止**：删 jobs/results/元数据；删未登记残留；因容量不足顺手删任何东西；重启后重置期限。

**歧义 1（partial 源文件是否删）**：`test_expired_upload_is_410_and_metadata_kept` L362 明确断言过期后源文件仍存在；design.md 只说「元数据不自动删」，未说 partial 文件删。最小保守建议：**C2 先只删终态源**，partial 源文件登记为「待定」并在 §8 列为主脑决策项；若主脑批准删 partial，必须新开一步并改写该断言，不得在实现卡里顺手改测试让它变绿。

**歧义 2（7 天起算点）**：用 `jobs.terminal_at`；`QUEUED`/`RUNNING` 无该列 → 永不删（B5）。

**歧义 3（16 GiB 是否随删除释放）**：R7 写「含未确认尾和旧保留源」，口径是**声明长度**而非磁盘占用 → 删源**不释放**预留（C1 不改这行）。

**歧义 4（待处理 result 预留口径）**：R7 只写「预留 result 与 WAL 峰值」，未给数字。最小保守建议：按 `待处理 Job 数 × MAX_RESULT_BYTES(64 MiB)` 计入 2 GiB DB guard，不引入新配置项；该式子让 DB guard 远早于真实字节触发，属保守方向，但需主脑认可这一具体口径。

## 6. M6/M7 环境前提（当前缺口，不得当作已验证）

| 前提 | 现状 | 缺什么 |
|---|---|---|
| ffmpeg 在 CI | `ci.yml` L30 的 pip 列表**不含** ffmpeg；`test_http_file_runner.py` L55 `pytestmark` 整文件 `skipif(FFMPEG is None)` | CI 上 HTTP 解码/资源证据**整体 skip = 假绿**。本卡禁改 workflows → 须主脑在 M6 前单开一张卡给 CI 装 ffmpeg |
| 三平台 runner | 只有 `ubuntu-latest`；Windows/macOS **无 runner** | M7 的三平台真实 ASR 基线**无路径**；本 Linux 假引擎 ≠ 三平台真实 ASR |
| 真实模型与样本 | 仓库无音频 fixture（全仓 `*.mp3/*.m4a/*.opus/*.wav` 为 0）、无模型权重 | M7 的字节/质量/资源基线**缺全部素材**；需主脑提供代表性样本与许可 |
| 无会话裸 shell | CI 走 `python -m pytest tests/ -q` | 需补一次无 env/cwd 依赖的裸 shell 全量跑（本卡未做） |
| systemd 单元 | `deploy/` 只有 `update.sh`/`update.ps1`/`pm2.ecosystem.config.js.example`，**无 systemd unit** | M7 部署文档的 systemd 口径无真实 unit 可核 |
| 可复用脚本 | `tests/harness/{client,server,worker,fake_engine}.py`、`tests/verify_long_upload_keepalive.py`、`tests/test_server_e2e_baseline.py`、`sdk/capswriter_asr/http_client.py` | 够 M6 用；M7 基线脚本**不存在**，需新写 |

结论：M6 在补上 ffmpeg 之前只能证明「限额与清理」，不能证明解码链；M7 的三平台/真实 ASR 基线**当前完全没有环境**，不得用本机假引擎结果顶替。生产发布需单独授权，本卡与 M4 全部增量都不部署。

## 7. 关键决策与已否决方案

决策：(1) 保护面只落在 `uploads.state/expires_at`、`jobs.state/terminal_at`、源文件三者，不建资源表、不建新的清理框架；(2) 容量闸在 store 层判，route 只透传，错误码沿用既有 507/429 不发明新码；(3) 清理必须是**真实周期任务**（`http_server` 监督链内），库函数只作被测对象不作证据；(4) 待处理 result 预留按 `Job 数 × 64 MiB` 保守计入 DB guard，不加配置项；(5) C1→C2→C3 串行（同改 `http_store.py`/同改测试文件），C4 可与 C2 并行。

已否决：M4 前做全协议横向盘点；用结构检查/同源计数当行为主证据；单点内存计数；静默只数内存；自动 retry/重跑；清理里删未登记残留；删 jobs/results；把 16 GiB 预留改成随删除释放；为 7 天起算新增配置项；把 C4 的反向红验推迟到 M6。

## 8. 主脑待决（不自行决定）

1. 过期 partial 源文件是否纳入周期清理（歧义 1）。现有测试断言它存在，默认按「不删」实现。
2. 待处理 result 预留的具体口径（歧义 4）。默认 `Job 数 × 64 MiB` 计入 2 GiB DB guard。
3. 7 天起算是否确认为 `jobs.terminal_at`（歧义 2）。
4. M6 之前是否单开一张卡给 CI 装 ffmpeg（§6 假绿风险）。
5. M7 三平台 runner、真实模型与代表性样本从哪来；在此之前 M7 不得开工。

## 9. 未知项（明确写未知）

- `python -m pytest tests/ -q` 的准确耗时未知（本机未跑全量；主要成本在真实 ffmpeg 编码与子进程重启用例）。
- 2 GiB / 16 GiB 阈值在真实大文件下的实际触发耗时与磁盘占用未知；T9 走阈值三点而非造 16 GiB 字节。
- 三平台真实 ASR 的字节、质量、耗时、CPU/RSS 全部未知（本卡未做任何测量）。