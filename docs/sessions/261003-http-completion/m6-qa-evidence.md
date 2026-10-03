# M6：HTTP 十二组边界 QA 覆盖表与证据

本文件是 `docs/sessions/261001-http-files/qa.md` 十二组的**当前已执行事实**，
不是计划。原 qa.md 写在 C2 之前，把 12 组几乎全部记成「未测」；C2 固定实现
（base `5134720`）已经带上大量真实跨进程测试，本表逐条核对哪些**已由既有断言证明**、
哪条关键不变式**仍缺**、本卡新增哪条测试。

统一约定（全表适用，不再逐行重复）：

- 隔离：全部 port 0、`tmp_path` 自有数据目录、`multiprocessing`/`subprocess` 真子进程，
  不连生产、不读真实录音。识别引擎一律是子进程里的 `ProgrammableFakeEngine`
  （`tests/harness/fake_engine.py`），但**解码是真 ffmpeg**（`/usr/bin/ffmpeg`，PATH 实测存在）。
- 跨边界断言取自真实 producer 侧：`tests/harness/worker.py:run_recording_worker` 在
  队列两侧记录**子进程真实收到的 Task** 与**真实发出的 Result**
  （`received_task_record`/`result_payload` 直接读对象字段，不接受测试手造 dict）；
  文件侧断言直接读 `data_dir/sources/*.bin` 字节与 `sha256`；SQLite 侧断言走只读连接。
- 反向红验要求：每条关键新断言至少有一次「注入相反实现 → 目标 AssertionError」的 scratch
  红验记录，见本文件末「红验记录」。

## 覆盖表（12 行）

| 组 | 已有测试（文件:行） | producer 级别 | 关键判定断言 | 缺口（base `5134720`） | 本卡新增 |
|---|---|---|---|---|---|
| 1 真实二进制 producer | `test_http_client.py:144`（SDK→裸 TCP 抓包）、`test_http_client.py:413`（CLI 子进程→裸 TCP 抓包）、`test_http_file_tasks.py:205`（真服务端落盘） | 真 SDK 真 CLI 真裸 TCP 字节流 + 真服务端落盘 | PATCH body 逐字节等于源文件切片（`test_http_client.py:206-211`）；`sources/*.bin` 字节与 SHA 等于源文件（`test_http_file_tasks.py:222-223`）；`confirmed_offset == size_bytes` | 抓包只到假 TCP 端点，落盘侧只到 `commit` 前；**没有「同一次真实上传」的抓包字节与落盘 SHA 端到端对照** | ✅ `test_http_qa_e2e.py::test_real_sdk_upload_bytes_match_server_disk_sha`（已跑绿） |
| 2 受理后脱离连接 | `test_http_file_runner.py:341`（四种真实容器，提交客户端已关闭，另一连接领 DONE+完整结果）、`test_http_file_tasks.py:451`（重开服务后领取） | 真 SDK + 真 HTTP listener + 真 worker 子进程 | 上传客户端 `submit` 返回后连接已关；`state.tasks == {}`、`active_http_jobs == []`；另一连接拿到的 payload 与子进程发出的 Result 逐字段相等（`:365-380`） | 无（组内已闭合） | — |
| 3 幂等创建与提交 | `test_http_client.py:476`（post/patch/commit 三处响应丢失 → 显式恢复，断言真实请求序列）、`test_http_file_tasks.py:236`、`test_http_file_runner.py:624`（重复 commit 不重投）、`test_http_store.py:211` | 裸 TCP 抓包 + 真服务端 + 真解码 | 恢复后请求序列恰为 `["GET"]`（commit 丢失）/ `["GET","PATCH"]`…；重复 commit 后 `len(tasks)` 不增、jobs/results 各 1 行 | **没有端到端「commit 响应真的发出又被丢掉 → 显式恢复 → 识别恰好 1 次」**：既有两条分别覆盖「客户端不自动重发」与「服务端不重投」，识别次数没有在真实服务+真引擎上一起数 | ✅ `tests/test_http_qa_e2e.py::test_lost_commit_response_recovers_with_exactly_one_recognition`（已跑绿） |
| 4 可信续传 offset | `test_http_file_tasks.py:272`（真 SDK 统计实际重发字节 = size−2048，落盘仍等于源文件）、`test_http_supervision.py:503`（两个真实崩溃窗口的确认前缀/未确认尾）、`test_http_store.py:161`（未确认尾被截到 DB offset） | 真 SDK 真实请求 + 真 kill 进程 + 真文件字节 | 重发字节数按实际 PATCH body 求和，不是假设为 0（`test_http_file_tasks.py:307-310`）；重启后 `confirmed_offset=0` 而磁盘上是**未确认尾**、SHA 等于尾（`test_http_supervision.py:527-530`） | 无（组内已闭合；未把未确认尾伪报为 0 的形态有断言约束） | — |
| 5 部分上传跨重启 | `test_http_supervision.py:503`（写前/offset 前/ACK 前 SIGKILL 后新进程）、`test_http_file_runner.py:747/784`（SIGTERM 与 SIGKILL 后重启 → `FAILED[server_restarted]`）、`test_http_store.py:249` | 真实独立服务进程 + 真 SQLite | 重启后 `QUEUED/RUNNING → FAILED[server_restarted]`、partial 前缀保留、**重启后 `received` 为空证明不自动重跑**（`test_http_file_runner.py:775`） | 无（组内已闭合） | — |
| 6 结果跨重启 | `test_http_supervision.py:577`（真实进程产出 producer payload → kill → 新进程读到逐字节相同 payload）、`test_http_cleanup.py:276`（源已删后 `source_available=False` 仍可领结果） | 真实独立进程 + 真实 SQLite 结果行 | 新进程 `GET result` == 旧进程写入的 producer payload 逐字段；源删后 200 且 `source_available is False`（`test_http_cleanup.py:488`） | 无（组内已闭合） | — |
| 7 双 owner 并发 | `test_http_file_tasks.py:1168/1243/1269/1294/1464`（WS 首帧与 HTTP commit 共用准入锁，**但不挂 worker、无识别**） | 真 WS 首帧 + 真 HTTP + 真 SQLite（无推理） | 内存 `state.tasks` 全空而 DB 有 6 行 `QUEUED` 时第 7 个 commit 必须 429 且不留 Job 行 | **没有任何测试让真 WS 识别与真 HTTP 识别同时经过同一个真 worker Queue/Result Queue**：key 不污染、空 socket 下 HTTP 仍可用、断 WS 后不再处理这三点全未测 | ✅ `tests/test_http_qa_e2e.py::test_real_ws_and_http_share_one_worker_without_key_pollution`（已跑绿） |
| 8 取消与 I/O 交错 | `test_http_file_tasks.py:619` | 真 TCP route task 取消 + 真 I/O worker + 真 fsync/SQLite | 「取消先/IO 先」各 5 轮，各自独立核对 `confirmed_offset` 与磁盘字节；未完 I/O 期间 mailbox 槽位不释放（32→31）、随后 GET/PATCH 都拿到可信 offset（`:680-731`） | 无（组内已闭合） | — |
| 9 重复 final/offset/源身份/空/块限/队列/磁盘/共享准入/幂等 | `test_http_file_tasks.py:504`（负例矩阵）、`:742`（身份与上限）、`:568`（handler/body 配额）、`:1347/:1427/:1464`（共享准入屏障）、`:1538/:1565`（幂等重放 vs 新建）、`test_http_capacity.py:527/588/649/784/825/869`、`test_http_release_invariant.py:663/802` | 真 HTTP 真 TCP 真 SQLite 真 ffmpeg/真实 fsync 读数（配额按缩小常量，类别与等值边界不变） | 被拒路径不产生 Job 行、不改旧字节、不改 `confirmed_offset`；容量满仍能重放 commit 与读旧结果 | 无（组内已闭合） | — |
| 10 解码与 PCM producer | `test_http_file_runner.py:341` 参数化 mp3/aac/m4a/opus（真 ffmpeg 编码 + 真 ffmpeg 解码） | 真 ffmpeg 包装脚本记录真实 argv/env 再 exec 真 ffmpeg | argv 逐项等于 `["-nostdin","-hide_banner","-loglevel","error","-i",<服务端自己的源>,"-ar","16000","-ac","1","-f","f32le","pipe:1"]`（`:415-426`）；段覆盖样本数 == 独立跑一次真 ffmpeg 得到的样本数（`:405`）；峰值并发解码 == 1；无临时 PCM 文件 | **矩阵里所有源都是 16 kHz 单声道**：没有「44.1 kHz 立体声 / 8 kHz 单声道」这类必须重采样+降混的输入，因此「有界 16 k mono f32 producer」的重采样侧未验证 | ✅ `tests/test_http_qa_e2e.py::test_resampled_sources_produce_bounded_16k_mono_f32_segments`（2 个参数各跑绿） |
| 11 整任务失败与监督 | 中间段失败 `test_http_file_runner.py:491`；解码失败 `:521`；结果超限 `:539`；worker 崩溃 `:1127`；段超时 `:817`；未知后台异常 `:1429`；SIGTERM 0 退出 `:747`；R4 释放顺序 `:880/:1192/:1256/:1320` | 真 worker 子进程崩溃 / 真 ffmpeg 非零退出 / 真 Result 溢出 | 任一失败路径：`state=FAILED` + 对应 `error_code`、`results` 表 0 行、`GET result` 409 `job_failed` 带 `error_code`；未知异常主进程非零退出；SIGTERM 退出码 0 | **「末段（is_final）解码失败」没有单独测**：既有失败注入固定在中间调用（第 2 次），末段失败时前面各段已产出非 final 结果，不发布缺段成功这一形态未验证 | ✅ `tests/test_http_qa_e2e.py::test_final_segment_failure_fails_job_without_publishing_partial`（已跑绿） |
| 12 源清理与长期兼容 | `test_http_cleanup.py:142`（终态边界 + 活跃引用）、`:276`（周期清理等真实 runner 引用）、`:553`（upload I/O 与清理共用一个 worker，5 轮）、`:778`（停机等在途清理）、`test_http_capacity.py:311/912`；旧 WS 回归由 `test_server_e2e_baseline.py`、`test_backpressure.py`、`test_health.py`、`test_error_codes_contract.py`、`test_protocol_v2.py`、`test_segmentation_contract.py` 承担 | 真 store 真 SQLite 真文件系统；旧 WS 用真 WS server + 真 worker 子进程 | 7 天前终态且无引用才删源、任务记录与结果仍可读、`source_available=False`；未登记残留不删；旧 WS 累计结果/背压/health/错误码全量回归 | 无（组内已闭合） | — |

## 本卡新增测试的不变式清单（逐条对应代码位置）

1. **同一次真实上传的字节链**：`submit_file_http` 真实发出的 PATCH body 序列拼接后
   == 源文件字节，且服务端 `sources/{upload_id}.bin` 的 SHA-256/长度与源文件一致。
   反向红验：把 SDK 的 `content` 换成 JSON/Base64 包装 → 落盘 SHA 不等，AssertionError。
2. **丢响应后只识别 1 次**：commit 的真实 202 响应被丢弃后，客户端走显式恢复路径，
   实际网络请求序列 == `["GET", ...]`，SQLite 只 1 条 job，识别子进程收到的段数
   == 该 Job 一次解码的段数（不翻倍）。
3. **WS/HTTP 同 worker 不串扰**：同一个识别子进程同时收到 `owner_kind="ws"`（带真实
   `socket_id`）与 `owner_kind="http"`（`socket_id=""`）的段；WS 客户端只收到自己
   `task_id` 的结果，HTTP 结果只落 `results` 表；断开的 WS 任务从 `state.tasks` 消失且
   不再产生引擎调用，同期 HTTP Job 仍 DONE；空 socket（只连不发帧）期间 HTTP 照常。
4. **重采样仍是有界 16 k mono f32**：44.1 kHz 立体声与 8 kHz 单声道源经真 ffmpeg 后，
   各段样本数之和 == 独立真 ffmpeg 测得的样本数，`samplerate==16000`、
   `data_bytes % 4 == 0`、单段样本数 ≤ 段预算，且送进 worker 的字节数远小于源文件字节。
5. **末段失败不发布缺段成功**：`is_final` 那次解码抛错时，子进程已发出前几段的非 final
   Result，但 Job 必须是 `FAILED[inference_failed]`、`results` 表 0 行、`GET result` 409。

## 红验记录（scratch，脚本保留在 /tmp/m6-red/ 与 tests/red_*.py，均不入库）

| 编号 | 注入的相反实现 | 目标用例 | 实测结果 |
|---|---|---|---|
| A | `core/server/connection/ws_recv.py` 断连 `finally` 里注释掉 `state.tasks.pop` / `pending_segments.pop`（旧行为：断连任务残留） | 组 7 | `AssertionError: 等待「断开的 WS 任务从 state.tasks 移除」超时 (30.0s)`，`1 failed`；撤销注入后绿 |
| B | `core/server/http_file_runner.py` 的 ffmpeg argv 去掉 `-ar 16000`（不重采样） | 组 10（两个参数） | `AssertionError: assert 882000 == 320000`（44.1k 立体声）、`assert 160000 == 320000`（8k 单声道），`2 failed`；撤销后绿 |
| C | `HttpFileRunner.fail_job` 先 `record_result` 发布一段「正文」再失败（缺段成功） | 组 11 | `AssertionError: assert 'DONE' == 'FAILED'`，`1 failed` |
| D | SDK `_request` 在 `AsrError(connection_lost/timeout)` 上自动重发一次（网络库自动重试） | 组 3 | `AssertionError: assert 2 == 1`（真实 commit 请求被发了两次），`1 failed` |

红验 A 之前先暴露了一个**恒真断言**：断连检查写成 `key[1] != "ws-drop"`，而
`TaskKey = (owner_kind, owner_id, task_id)`，`key[1]` 是 socket_id 恒不等于 task_id，
注入 A 也不变红。已改为 `key[2]` 并补显式 `== []` 断言，重跑 A 才拿到目标红。

## 未验证（明确留给 M7 / 后续）

- 真实三平台部署、真实 ASR 模型、真实字节/质量基线：本卡全部用假引擎 + 真解码，
  **不得**据此宣称 HTTP 已可用或质量达标。
- 1 GiB / 16 GiB / 2 GiB 等物理容量按缩小常量验证类别与等值边界，未按真实上限压测。
- 原主干 `2919` 那次并发 commit 读超时的根因仍未定位，见
  `docs/sessions/261003-http-completion/fix55-merged-acceptance-evidence.md`。