<!-- delegate-outcome: succeeded -->

## 审查结论

- 风险级别：internal。冻结范围 c1e8808377cf205094711832076f51aebc77ff33..86be503586dfe44c21a0970e8f3af329c2ae3f96；专项增量 70bbad8e64a3cec2a19e4e9fdf762c7230bc0ee2..86be503586dfe44c21a0970e8f3af329c2ae3f96。
- 结论：无 P1；有一条 P2，见下文。

failure-visibility: p2-only
- A 轮登记的依赖命令问题已补：docs/development/testing.md 显式钉住 aiohttp 3.14.3、HTTPX 0.28.1，CI 也安装 aiohttp。B 轮 idle finding 已落实。B 轮重复 create 并发时同一 upload 可能返回 201 的 P2 按任务卡接受；SDK 成功消费者允许 200/201（sdk/capswriter_asr/http_client.py:324-333,432-451），不要求增加 created 状态。
- OCR：沿用卡面给出的 A 轮 reviewed_fallback，本轮未重复全量 OCR；本次仍独立审查完整冻结范围。

## E2 增量四问（70bb..86be）

1. **是否只修登记 idle 合同：是。** core/server/http_server.py:290-320 将既有 ServerConfig.upload_idle_seconds 用于每次 64 KiB read；超时产生局部 408。http_server.py:154-156,248-255 让 408 完整响应后关闭连接，并将 aiohttp lingering drain 设为 0，解决半开请求在错误响应后继续挂住的问题。
2. **是否新增未经批准抽象：没有新 timer、状态机、配置、重试或通用包装。** _read_idle_seconds 是单一配置消费边界；它只转发现有配置，不保存第二份事实。AppRunner.lingering_time 是框架已有参数，aiohttp 文档说明它控制关闭连接时读取并丢弃剩余客户端数据的最长时间；Response.force_close() 禁用 keep-alive。[aiohttp 3.14.3 Server Reference](https://docs.aiohttp.org/en/stable/web_reference.html)
3. **状态/事实源/fallback：没有新增。** deadline 从既有 CW_UPLOAD_IDLE_SECONDS 读取；只有 body 完整读完后才进入 store。408 不改 confirmed offset、文件或 TTL；没有 retry/fallback/隐式清理。
4. **双路径、合法慢传、WS 回退：未发现。** 只有 _read_body 一处 body reader；每次等待单独计时而非整请求计时。每块间隔低于 idle、总时长高于 idle 的实际 TCP 上传成功。WS 默认行为、端口、health、背压由正式全量 WS 回归覆盖。

## 机械枚举的写读生命周期矩阵

轴从当前 producer/consumer 机械枚举：http_server.py:170-177 六条 route；http_store.py 的 CREATE TABLE、所有 INSERT/UPDATE/COMMIT、os.write/fsync；HttpIoWorker 的 add_done_callback；CapsWriterServer._serve_all/stop 的监督和收尾分支。时间字段来自 http_store.py:266-297 的 schema，而不是从 diff 成功路径反推。

| writer / 动作 | 写入依据 | reader / 消费者 | 读取依据与锁定证据 |
|---|---|---|---|
| create_upload：建私有 source，再插入 upload 行 | token 指纹、create key、size/hash、归一化 options、准入检查；空文件 fsync 后事务提交 | get_upload、PATCH、commit、create admission | _row_for_token/get_upload/append_bytes/commit_upload；test_create_is_exclusive_file_then_zero_offset_commit、test_sdk_upload_reaches_disk_then_commit_is_explicitly_unavailable |
| append_bytes：truncate 到已确认 offset，positioned write、短写循环、fsync，条件更新 offset/更新时间/expiry，返回值才是 ACK | 请求 offset 必须等于当前 confirmed offset，身份/状态匹配，chunk 与文件上限通过 | GET upload、恢复 SDK、commit 完整 hash/length 检查 | test_append_fsync_offset_transaction_and_ack_order、test_append_uses_portable_positioned_write_and_completes_short_writes、test_unconfirmed_tail_is_truncated_to_database_offset、test_resume_after_failed_patch_only_sends_unconfirmed_suffix |
| _read_body：无数据库/文件写入 | 3 个 body route 共享 64 KiB read；每次 read 等待既有 idle；body slot 满立即拒绝 | handler/body semaphore；408 consumer；后续请求 | test_body_idle_timeout_is_408_on_create_patch_commit 每 route 五次；test_two_half_open_bodies_timeout_then_new_requests_succeed；本卡真实进程/TCP 探针：408 完整 body+EOF、首字节/部分停顿、peer 未先关、GET 在两槽占满时成功、之后 POST/PATCH 成功 |
| commit_upload：验证整文件后一个事务更新 upload 并插入唯一 QUEUED job | 已确认 offset=声明长度、真实文件长度/hash 一致、Job 准入且 coordinator ready | GET upload/job/result、SDK 显式恢复 | test_commit_requires_real_inference_coordinator、test_commit_creates_single_job_and_repeat_returns_same、test_job_lifecycle_repeated_commit_and_result_not_ready；无 coordinator 时真实响应 503 且不建 Job |
| _expire_if_due：只将到期 UPLOADING 标为 EXPIRED | expires_at <= time.time()；不刷新期限、不删元数据 | GET upload 与写入/commit 校验；GET job 读 source_available | test_expired_upload_is_410_and_metadata_kept；本卡真实 SQLite 探针证明成功 PATCH 后 GET 不改 updated_at/expires_at |
| _converge_restart：旧 QUEUED/RUNNING → FAILED/server_restarted，保留 DONE 和 partial | 新进程打开同一 SQLite 数据目录 | job 状态/result reader 与 upload 恢复 reader | test_restart_converges_queued_and_running_but_keeps_partial_and_done、test_http_offset_crash_windows_recover_in_new_processes、test_result_producer_payload_and_done_survive_new_process |
| record_result：校验 payload 后，同一事务转 DONE 并插入 result | task id、file/http/空 socket、严格 is_final is True、text/text_accu、token/timestamp 类型与长度、有限数字，且 job 为 QUEUED/RUNNING | job status 和 DONE-only result route | test_record_result_requires_matching_complete_payload_and_preserves_terminal_jobs、test_persisted_done_result_is_served_after_reopen。真实 runner/sink 尚属 E3，本卡不把测试 producer 当 runner 验收 |
| 创建/Job 准入与持久资源 | 32 个 UPLOADING、source 声明量、DB/WAL/SHM 当前字节、物理 free、8 个 QUEUED/RUNNING；IO mailbox 32 | 新 create/commit、已有 GET、store worker | test_admission_refuses_new_upload_without_touching_old_data、test_body_and_handler_admission_rejects_without_waiting、test_io_mailbox_refuses_33rd_and_cancel_keeps_slot_until_future_done。E4 的精确 GC/WAL 全盘账仍未交付，不在本卡冒充已验收 |
| I/O 完成、异常与 shutdown | HttpIoWorker.run shield 住已提交线程任务，done callback 完成后才归 mailbox；未知异常通知 _mark_fatal | HTTP listener fatal event、_serve_all、进程 exit code；SIGTERM 收尾 | test_cancelled_http_handler_io_failure_reaches_process_supervisor、test_unknown_http_operation_stops_listener_and_exits_nonzero、test_websocket_runtime_error_exits_nonzero、test_enabled_http_serves_and_sigterm_exits_zero_releasing_port；真实进程 idle 探针 SIGTERM=0、端口释放 |

### 七项不变式逐条对照

1. **异常与进程生命周期**：代码在 http_server.py:202-246、app.py:70-91,145-185；未知 route/IO/WS 异常非零，SIGTERM 正常零退出。锁定测试见矩阵最后一行及真实进程探针。正常 peer 断开、invalid body 与 408 都是局部 HTTP 错误。
2. **名额、idle、超时与 TTL**：代码在 http_server.py:216-238,294-320；两 body、16 handler、32 mailbox 满时不排无限 waiter。三 route 的 408、两半开恢复、慢传和 TTL 由矩阵第三行测试锁定。生产默认 300 由 test_upload_idle_default_300_reaches_http_reader 的无会话裸进程锁定；未跑 300 秒墙钟。
3. **文件与确认边界**：代码在 http_store.py:148-178,197-225,433-518。文件权限、SQLite WAL/FULL/foreign_keys/busy_timeout=0、portable positioned write、短写、fsync→offset→ACK、未确认尾与重启恢复由矩阵前两行测试锁定。当前证据是 Linux/POSIX；Windows、macOS 实机及 NTFS ACL 未知。SIGKILL 窗口不等于断电耐久证明。
4. **六 route、凭据与幂等**：代码在 http_server.py:170-177,324-413、http_store.py:139-145,411-469,535-599。错误/未知 upload token 同 404，令牌只存 SHA-256 指纹；create 参数 identity/key 不可变，缺失/None options 归一化、错误类型 4xx，SDK 恢复接受 200/201。例外是下列分段范围 finding。没有 coordinator 时 commit 503，不建假 Job。
5. **结果与终态**：代码在 http_store.py:303-316,601-657，测试见矩阵第六行。终态不被迟到结果反转或覆盖；旧 QUEUED/RUNNING 重启失败、DONE/partial 保留。真实 runner 属 E3，未声称已运行。
6. **资源与私有目录**：代码在 http_store.py:33-54,148-154,197-224,384-407、http_server.py:61-110,125-126,294-320。16/2/32/32 在途与会话起点由矩阵锁定；Linux POSIX umask 测试锁定 0700/0600。E4 垃圾回收/WAL 全盘账和 E7 模型基线仍待办，不由本卡代验。
7. **依赖与旧 WS**：CI workflow 和 docs/development/testing.md 显式装 aiohttp 3.14.3/HTTPX 0.28.1；config_server.py 的版本/成对启用与 app.py 默认关闭由 test_http_config.py、test_http_supervision.py 锁定。HTTP 未启用时完整旧 WS/health/端口/背压结果由本次全量测试覆盖。

## Finding：P2

**P2-1：HTTP 上传接受旧 WS 明确拒绝的分段参数。**

- 契约：docs/sessions/261001-http-files/design.md R3 要求 HTTP 参数使用旧校验语义；docs/reference/protocol.md 的既有 WS 参数规则也适用。
- 代码：core/server/http_store.py:669-694 的 normalize_options 只检查 seg_duration/seg_overlap 是有限、非负数字；http_server.py:343-356 随即持久化。它没有执行旧规则：core/server/connection/ws_recv.py:84-106 拒绝 seg_duration < 5，并要求 seg_overlap < seg_duration/2。
- 实测输入/环境：本卡 /tmp/capswriter_http_e2_probe_c_261001.py 在真实 CapsWriterServer 子进程、TCP listener、独立 SQLite/private data-dir 发 POST /v1/uploads，传 seg_duration=0.0, seg_overlap=0.0；消费到 201，SQLite options_json 已保存 seg_duration=0.0。SDK _options 也仅限制数字有限且非负（sdk/capswriter_asr/http_client.py:82-99），因此不是仅手造服务端内部 payload。
- 风险两问：真实合法 API 调用能触发（是）；结果不会静默错写，但无效参数和完整文件先被接收，M3 runner 接入后才可能失败，早期 400 契约与用户成本不可接受。分级 P2，不是 P1。
- 修复方向：在 create 接收边界复用已存在的分段规则并映射为 400；不需要新 validator、状态或 fallback。本卡只登记，不改实现。

## 验证、外部状态与限制

- 正式命令（Python 3.12.3、pytest 9.1.1、aiohttp 3.14.3、HTTPX 0.28.1）：UV_CACHE_DIR=/tmp/capswriter-http-e2-review-c-20261001-uv-cache UV_PYTHON_INSTALL_DIR=/tmp/capswriter-http-e2-review-c-20261001-python uv run --no-project --python 3.12 --with numpy --with rich --with websockets --with colorama --with pytest==9.1.1 --with soundfile --with pytest-asyncio==1.4.0 --with aiohttp==3.14.3 --with httpx==0.28.1 python -m pytest tests/ -q -p no:cacheprovider。结果：**312 passed, 3 skipped, 86 warnings, 125.72s**；3 个 skip 是 ForceAligner 两项及 silero-VAD 资源项，不是 HTTP 缺依赖。另独立真实进程探针全通过，含 0.35 秒 idle 下 0.70 秒慢传、完整 408 body+EOF、后续 404/503、数据未变和 SIGTERM=0。
- PR #38 即时只读快照：gh pr view 38 --repo zlxlabs/CapsWriter-ASR-Server --json headRefOid,state,isDraft,statusCheckRollup；head 与冻结 H0 一致，draft/open，两个 CI checks 均 SUCCESS。未等待 CI。派发基线 gh api 不可用，故“继承红”无法判定；本地全量无新红、当前 H0 CI 无红。
- 平台未知：本机 Linux；未在 macOS、Windows、NTFS ACL 上实机验证。未跑生产 idle 300 秒墙钟，未加载真实 ASR 模型，也未验证真实 HTTP runner/GC；这些仍分别属平台证据、E3/E4/E7 后续范围。

### 踩坑

系统 Python 未安装 aiohttp；因此按项目正式命令用隔离 uv 环境，HTTP 测试没有因缺库跳过。首次临时 probe 的 EOF 判据错误地要求 429 也带显式 Connection: close；修正判据后确认 429 响应完整，408 明确 close 并 EOF。

### 绕过

没有重跑 A 轮 OCR；沿用任务卡给出的 reviewed_fallback 结果。新增实际进程取证写在唯一 /tmp/capswriter_http_e2_probe_c_261001.py，只用私有 data-dir 与动态端口，不改仓库测试/实现。

### 偏差

默认 300 秒只验证了裸环境配置传播，未等待 300 秒；慢传和超时使用 0.35 秒隔离值。真实 runner、垃圾回收和非 Linux 文件 ACL 没有被虚构为已验证。

### 最贵一步

正式全量测试约 126 秒；其余最贵是补做真实 CapsWriterServer 新进程/TCP/SQLite 探针，覆盖 partial body、两半开、TTL、EOF 和慢传。
