<!-- delegate-outcome: succeeded -->

# E2 聚合修复增量审与真实进程/存储消费者复核

审查风险级别：internal。全量冻结范围 `c1e8808377cf205094711832076f51aebc77ff33..70bbad8e64a3cec2a19e4e9fdf762c7230bc0ee2`；专项增量 `64e78408788832a555351deb45be48541c80205d..70bbad8e64a3cec2a19e4e9fdf762c7230bc0ee2`。结论基于冻结提交、`design.md`/`qa.md` 合同、四组 HTTP 测试及真实子进程/TCP/SQLite 探针，未读取实现方派发报告、推理或对话，也未修改实现、测试、依赖、CI、goal 或协议文件。

failure-visibility: p1-found

## 裁决

发现 1 项 P1 和 1 项 P2。A 轮登记的 4 项 P1、5 项 P2 均由本轮代码或真实消费者测试复验通过；但全量冻结范围仍未满足 R7 的 300 秒无输入空闲要求，不能验收通过。HTTP 默认关闭、M2 goal 仍为 `进行中` 且 `merged_pr: null`。本结论是 review 完成，不代表产品通过，也不授予合并/部署权限。

## P1：请求体无输入时没有 300 秒截止，两个 body 名额可被长期占住

- 合同：`design.md` R7 要求无输入空闲 300 秒，并明确不能让少数半开 body 使后续上传永久无法恢复；QA 第 9 组要求资源边界真实生效。
- 代码：`core/server/http_server.py:124-125` 只设置 2 个 body 名额；`285-301` 在读完整个 body 前一直持有名额，`291-293` 对 `request.content.read()` 无限等待，没有 300 秒读空闲期限。`153` 创建 `AppRunner` 时未设置请求体截止。aiohttp 3.14.3 的实际 `RequestHandler` 默认参数只有 keepalive 超时（3630 秒），不是请求体读空闲超时；`config_server.py:54` 默认监听 `0.0.0.0`，HTTP 沿用该监听地址。
- 真实输入/输出：执行保留探针 `/tmp/http-e2-review-b-261001-process-idle-body-probe.py`，由真实 `CapsWriterServer` 子进程启动 listener，在两条真实 TCP POST 上发送有效 Bearer、JSON 头和 `Content-Length: 2` 后暂停 body。2 秒后第三条 body 请求得到 `HTTP/1.1 429 Too Many Requests`；关闭前两条连接后，相同服务立即接受下一条完整创建请求并返回 201，SIGTERM 退出码为 0。探针没有用任意规模或内存耗尽输入。
- P1 两问：①真实路径会触发：启用 HTTP 的持有者可因弱网/停顿让已建立连接长时间不再发送 body，两个在途上限即被占满；本次在真实进程/TCP 中复现。②后果不可接受：在连接仍存活期间，所有新创建或 PATCH body 都持续 429，HTTP 上传/续传入口不可用，直到对端关闭或系统层断开；只读 GET 仍可用并不能恢复上传。该行为也超过合同的 300 秒界限。
- 当前新增的 `test_body_and_handler_admission_rejects_without_waiting` 证明超限快速 429 和无 semaphore 等待者，但只观察 1 秒并主动关闭 stalled 请求；没有锁住 300 秒截止和恢复。因此该绿测不反驳本 finding。

## P2：并发重复创建的响应状态不符合幂等合同

- 合同：`design.md` R3：相同 key、token 和身份参数的首次请求 201，重复请求 200 并返回同一 upload。
- 代码：`core/server/http_server.py:325-338` 先单独查询 `existing_key_seen`，再调用创建；并发请求可以都先读到“不存在”，之后 `HttpStore.create_upload()`（`http_store.py:416-427,452-459`）虽能依唯一约束返回同一条记录，但响应码仍分别按过期查询结果选择。
- 真实输入/输出：保留探针 `/tmp/http-e2-review-b-261001-idempotency-race.py` 使用真实 HTTP/TCP listener、I/O worker 和 SQLite，并以屏障确保两个同 key 创建请求都先完成“未存在”查询。输出 `statuses=201,201`、`upload_rows=1`、`same_upload=True`。因此没有重复上传记录或 Job，错误只在重复响应码，但仍违反公开合同；实际 SDK 接受 200/201 两种状态，故列 P2，不阻断本轮之外的产品验收判定。

## 专项增量四问（64e784..70bbad）

1. **只修登记问题？** 是。生产增量限于 `app.py`、`http_server.py`、`http_store.py`，对应 A 轮监督、准入、portable write、结果发布、options 与权限项；另补测试和文档。未引入模型 runner、GC 或新协议面。半开 body 无截止和幂等状态竞态属于全量复核发现，不是这轮新增。
2. **新增未经批准抽象？** 未发现。`HttpIoWorker.on_failure` 把已取消等待方之后发生的线程异常交给 `HttpServer`；同一消费者链还处理 route 内未知异常。`_fatal_event` 由这两类生产者设置、由 `serve()` 读取，是从后台错误到进程监督的必要边界。
3. **状态/事实源/fallback 是否无依据增加？** 未发现。错误仍以 SQLite/文件为事实源；没有新增重试或 fallback。`options` 只将缺省/`None` 归一化为空对象，错误类型继续拒绝；terminal result 使用事务更新。
4. **是否留下双路径？** 未发现本轮写入双路径。文件定位写改为 `lseek+write` 并循环处理短写，没有 `pwrite` 或平台 fallback。发现的幂等响应竞态是预查状态与实际创建结果分开计算造成的状态不一致，详见 P2。

## A 轮登记项复验

| 登记项 | 本轮证据与判定 |
|---|---|
| P1-1 HTTP 未知操作只返回 500 | `test_unknown_http_operation_stops_listener_and_exits_nonzero` 对真实 `CapsWriterServer` 子进程发 GET，收到 500 后进程非零退出、listener 关闭；取消等待方后的未知 I/O 错误另由 `test_cancelled_http_handler_io_failure_reaches_process_supervisor` 验证非零退出。已修。 |
| P1-2 handler/body semaphore 会无限排队 | `test_body_and_handler_admission_rejects_without_waiting` 建立 17 条真实 TCP stalled POST，超限收到 429，两个 semaphore 均无 waiter，GET 仍可访问。准入等待队列缺口已修；空闲超时另列当前 P1。 |
| P1-3 `os.pwrite` 不可移植 | 当前 `http_store.py:494-501` 使用 `lseek+write`，循环处理短写并在 offset 提交前 `fsync`；`test_append_uses_portable_positioned_write_and_completes_short_writes` 用真实落盘字节断言验证。Linux 实跑通过；Windows/macOS 未实机验证。 |
| P1-4 WS RuntimeError 被视作正常退出 | `app.py:145-184` 把 listener 任务异常设为非零并向外传播。套件覆盖 WS start RuntimeError；额外真实 `CapsWriterServer` 子进程注入 sender、monitor RuntimeError，两个进程均 exit 1。正常 SIGTERM 子进程仍 exit 0。 |
| P2-1 route 取消提前归还 I/O 名额 | `test_handler_cancellation_and_io_completion_orders_preserve_confirmed_bytes` 直接取消真实 aiohttp server handler，取消先于 I/O 和 I/O 先于取消各执行 5 次，核对独立 SQLite offset、source bytes、冲突 offset 与名额；`test_io_mailbox_refuses_33rd_and_cancel_keeps_slot_until_future_done` 验证 33rd 立即 429、底层 future 完成前名额不归还。已修。 |
| P2-2 result publisher 接受错任务/迟到结果并覆盖终态 | `record_result()` 只接受匹配 `task_id`、`type=file`、空 socket、`owner_kind=http`、严格 `is_final is True`、完整字段及 token/timestamp 数量匹配；仅 QUEUED/RUNNING 能在单事务写结果并转 DONE。`test_record_result_requires_matching_complete_payload_and_preserves_terminal_jobs` 验证错 ID、非 final、缺字段被拒，DONE/FAILED 不被覆盖。新进程结果读取见下方。已修。 |
| P2-3 falsy 错类型 options 被当默认值 | 当前 route 只对缺省或 `None` 传 `{}`；`test_options_missing_and_none_default_but_falsy_wrong_types_are_rejected` 验证缺省/None 接受，数组、空串、false、0 等错类型拒绝。已修。 |
| P2-4 文件权限受 umask 放宽 | POSIX `open()` 后显式 `fchmod`，目录 `chmod 0700`；`test_http_store_files_are_private_under_umask_0002` 在 umask 0002 下核验目录、DB/WAL/SHM、lock、source 为 0700/0600。Linux 通过；Windows ACL 未验证，macOS 未运行。 |
| P2-5 文档完整命令缺 aiohttp/httpx | `docs/development/testing.md` 的首条完整命令现在显式安装 `aiohttp==3.14.3`、`httpx==0.28.1`，与 CI 安装行一致。本轮以相同版本运行四个 HTTP 套件，无 HTTP 缺依赖 skip。已修。 |

## 当前真实消费者验证

- 测试命令：`uv run --no-project --python 3.12 --with numpy --with rich --with websockets --with colorama --with pytest==9.1.1 --with soundfile --with pytest-asyncio==1.4.0 --with aiohttp==3.14.3 --with httpx==0.28.1 python -m pytest tests/test_http_config.py tests/test_http_store.py tests/test_http_file_tasks.py tests/test_http_supervision.py -q -p no:cacheprovider`。结果：`42 passed, 1 warning in 15.56s`，无 skip。
- 真正的 HTTP producer：`tests/test_http_file_tasks.py` 用真实 SDK、aiohttp listener 和 HTTPX 验证请求体原字节落盘、续传文件字节/hash/SQLite offset、六 route、状态、UTC `expires_at` 字符串与 wrong token 404。未知 HTTP/I/O 错误与 SIGTERM 检查由真实 `CapsWriterServer` 子进程覆盖。
- 两个 offset 崩溃窗口由 `test_http_offset_crash_windows_recover_in_new_processes` 在 fsync 后/offset 事务前和 offset 提交后/ACK 前杀进程，再启动全新服务进程核对 HTTP offset 与完整文件字节/hash。结果 producer 由 `test_result_producer_payload_and_done_survive_new_process` 经子进程写出真实 producer JSON，杀掉服务后由全新服务进程 GET 到 DONE 和完全相同的持久 payload。这里是测试协调器注入的结果 producer，不是 ASR 模型或 M3 runner。
- 另跑真实 shutdown 探针 `/tmp/http-e2-review-b-261001-process-shutdown-io-probe.py`：真实 `CapsWriterServer` 收到 SIGTERM 时 worker 中有受控在途写，进程在 0.2 秒后仍存活，等待 fsync/SQLite offset/204 完成后以 0 退出；其后新 `HttpStore.open()` 成功取得目录锁，offset 为 17 且 source bytes 完全匹配。正常关闭的排空与 fatal 异常非零路径因此分别得到验证。
- WS sender/monitor 探针 `/tmp/http-e2-review-b-261001-ws-supervision-probe.py` 在真实监听进程内分别注入 RuntimeError，两者均返回码 1；正常 SIGTERM 已由套件验证为 0。错误原因由退出状态消费，本探针不把退出前的 banner 当结果。
- 真实环境为 Linux 6.8、Python 3.12.3；没有在 macOS/Windows 或真实 ASR 模型上运行。Windows NTFS ACL、跨平台锁原语、真实 runner 与模型资源/质量基线均保持 unknown；没有用 POSIX chmod 推断 Windows ACL，也没有把测试协调器当 runner。
- A 轮 OCR 标记为 `reviewed_fallback`（primary quota exhausted，DeepSeek 完成 5/5 findings）。本轮按同一修复循环约定未重复完整 OCR；仍重新审全量冻结差异并运行本轮真实消费者检查。派发时主干基线因 `gh api request failed` 不可用，继承红未能判定；按卡要求未等待远程 CI。收件箱 issue #40 讨论 tokens 与最终 text 对应关系，属于相邻协议请求，本轮未修改或重开相关协议/PR。

## 接手与现场

本工作区无交接单；派发编号与当前 worktree 相符，起始 `git status` 干净。巡检摘要：`summary: orphan 0 owned 0 unattributable 0 too-new 0 recent-7d 0 stale-over-7d 0 missing_ledger_repos 0`，巡检项未展开，需要时跑 `/worksite-audit`。记忆报告探针返回 `memory_dir_mismatch`（exit 2），表示本机 memory 缓存目录与探针配置不一致；需要重建时按本机 agent-config 的 memory-doctor 指引操作。

## 踩坑

- 幂等竞态探针第一次从主线程直接访问由 I/O worker 创建的 SQLite 连接，触发 SQLite 跨线程错误；该次不作为产品结论。修正为经同一 worker 读库后，真实 HTTP 请求复现两个 201、单条记录。
- WS sender/monitor 子进程探针最初遗漏 `numpy`，在导入模型模块前失败；补齐正式验证依赖后再运行，sender 与 monitor 都实测 exit 1。

## 绕过

- 所有额外探针写在 `/tmp/http-e2-review-b-261001-*.py` 并保留；使用隔离目录、回环 TCP、自有进程和真实 SQLite/文件。没有连接生产服务、读取真实录音或修改仓内实现/测试。
- half-open probe 使用明确的小 JSON body 长度和有效 Bearer，按两个 body 名额复现后关闭 peer 并确认上传恢复；不以理论 OOM、任意连接数或日志文本作结论。

## 与卡偏差

- 全量冻结与增量 SHA、review 输出路径和唯一文件边界均遵守；仅创建本 verdict。没有运行远程 CI、没有合并或部署；主干基线不可用，继承红标为未能判定。A 轮 OCR 已完成，本轮不重复整套扫描。
- 本轮 verdict 有 P1/P2 finding，故 review verdict 为不通过；执行器任务已完成，M2 仍是待验收/未合并。真实平台、ACL 和推理 runner 留作明确 unknown。

## 最贵一步

最费时的是把“SIGTERM 会排空有限在途 I/O”落实到真实服务进程证据：先让 CapsWriterServer 的上传写进入 I/O worker，再发 SIGTERM，随后核对进程仍等待、204 ACK、重启后 SQLite offset/source bytes 以及数据目录独占锁释放。静态阅读 `stop()` 或一次空闲 SIGTERM 都无法证明这条路径。
