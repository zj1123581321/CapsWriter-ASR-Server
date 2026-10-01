# E2 上传确认、持久状态与真实后台监督：独立裁决

审查对象：PR 38 冻结全量 `c1e8808377cf205094711832076f51aebc77ff33..64e78408788832a555351deb45be48541c80205d`，HEAD=`64e78408788832a555351deb45be48541c80205d`。本审查只核对冻结代码、中性设计/QA 合同和隔离真实 producer 探针；未读取实现方报告或私有对话。

failure-visibility: p1-found

## 裁决摘要

发现 **4 项 P1、5 项 P2**。目前不满足 R6、R7 和跨平台文件 API 合同，不建议合并。该裁决不是要求提前实现 E3 推理 runner 或 E4 清理器。审查没有修改实现、测试、CI、GOALS 或其它跟踪文件。

P1 的两问：每项都违反已冻结合同中明确的监督/准入/平台支持要求；且真实故障可分别造成服务进程继续以零退出码存活、持有效 token 的客户端占满 handler 并让后续请求无限等待、Windows 首次写块直接失败，或 WebSocket 监听失败被报告为正常退出。这些是本卡中足以阻止合并的实际运行结果。

## P1：合并前需修复

### P1-1 HTTP 请求内部异常只返回 500，进程不进入失败态

- 合同：R6 要求关键未知 operation 错误进入真实监督链并使进程非零退出，不能只写一个无人读取的 fatal 字段。
- 位置：`core/server/http_server.py:204-220` 将异常保存到 `self.fatal` 后只回 500；文件中没有消费者读取 `fatal`。HTTP task `serve()` 仍挂起（`171-188`），`app.py:154-168` 只监督 task 是否结束。
- 真实输入与输出：隔离真实 `CapsWriterServer` 子进程中，将 `/v1/jobs/{id}` 查询注入一个 `RuntimeError("probe_unknown_operation_failure")`。两次真实 HTTP GET 均收到 `HTTP/1.1 500`，进程两次请求后仍存活；随后正常 SIGTERM 得到退出码 0。stderr 有错误日志，但主监督没有据此退出。
- 后果：存储/SQLite/内部操作发生未知故障后，服务仍对外存活，监控进程码和 listener 状态无法识别已发生的 fatal 故障。

### P1-2 并发上限只限制同时执行，不限制等待队列

- 合同：R7 明确要求超过 handler/body 限额时以 429/507 拒绝，不能无限排队等待。
- 位置：`core/server/http_server.py:122-123,204-207` 在两个 `asyncio.Semaphore` 上直接等待；超限没有拒绝路径。
- 真实输入与输出：在临时目录启动真实 TCP listener，使用带 Bearer token、有效 `POST /v1/uploads` 头部但暂不发送 2 字节 JSON body 的 17 条独立连接。观测到 `handlers_free=0`、`handler_waiters=1`、`body_free=0`、`body_waiters=14`；未发送任何响应，第 17 条连接也未收到响应。前两条 handler 卡在 body 读取，后续连接继续积压等待 semaphore。
- 后果：持有效 token 的客户端只需保持连接不发 body，便可耗尽 handler 容量并让后续请求排队。当前实现没有合同要求的快速拒绝或有界等待。

### P1-3 Windows 不支持 `os.pwrite`，上传写入会在首块失败

- 合同：R1 明确保留 Linux/macOS/Windows 文件 API 支持；不能由 Linux 运行结果外推 Windows。
- 位置：`core/server/http_store.py:476` 无条件调用 `os.pwrite`。
- 证据：Python 3.12 官方 `os.pwrite` 文档标注该函数仅在 Unix 可用：[Python 3.12 `os.pwrite`](https://docs.python.org/3.12/library/os.html#os.pwrite)。仓库 `docs/maintainers/operations.md:8` 将 Windows 列为当前服务部署平台。首个 PATCH 执行该行时，Windows 没有此 API，会抛 `AttributeError` 并返回 500；这一步之后的 fsync、offset 事务和 204 ACK 都不会发生。
- 平台边界：本次真实执行环境是 Python 3.12.3 / Linux 6.8 x86_64；没有在 Windows 或 macOS 上运行。此裁决依据 API 的官方平台可用性及代码调用，不把 Linux 探针冒充 Windows 实测。

### P1-4 WebSocket listener 的 `RuntimeError` 被外层当正常停止

- 合同：R6 要求未知 RuntimeError 不能被吞，真实监听失败必须非零退出；正常主动停止才允许 0。
- 位置：`core/server/app.py:137-144` 捕获所有 `RuntimeError`，仅当 `exit_code` 非零才重新退出；`_serve_all()` 在 `154-168` 将 WS task 异常重新抛出，但没有设置 `exit_code`。注释所称“事件循环被 stop() 主动打断”的条件并未被判断。
- 真实输入与输出：真实服务子进程中令 `SocketManager.start()` 抛 `RuntimeError`，得到 `START_RETURNED exit_code=0` 且子进程 `returncode=0`，没有非零退出或 traceback。
- 后果：WS 监听初始化/运行时未知错误可以被进程管理器记录为干净完成，与合同区分的正常 SIGTERM 和真实异常退出相反。

## P2：当前可触发缺口，低于本卡阻断线

### P2-1 I/O future 被 route 取消时提前归还 mailbox 名额

- 位置：`core/server/http_server.py:83-92` 在提交线程任务并 `shield` 等待后，`finally` 无条件释放 mailbox；future 实际完成后才会由 `94-101` 从 `_pending` 移除。取消等待方不会停止线程里的 I/O。
- 探针：使用真实 `HttpIoWorker`、SQLite、文件及屏障，取消先于 I/O 和 I/O 先于取消两种顺序各执行 5 次。已确认源字节及 offset 均正确；在这个单线程串行 worker 探针中没有观测到字节覆盖。取消后 mailbox 空闲名额为 1、pending 为 2；连续保持一个屏障并提交更多操作时，pending 实测到 33，超过 `IO_MAILBOX=32`。
- 限定：当前 aiohttp 3.14.3 的 `handler_cancellation_default=False`，普通客户端断开不会默认取消 server handler；已有取消测试取消的是 HTTPX 客户端 task，不等价于 server handler 取消。仍存在显式 task cancellation 等真实取消路径下的在途名额缺口，因此列 P2，不把未观察到数据覆盖说成已发生。

### P2-2 结果发布函数可将无效/迟到 payload 标为 DONE 并覆盖结果

- 位置：`core/server/http_store.py:577-597` 对结果执行 `INSERT OR REPLACE`；DONE 更新只筛 `state != DONE`，不验证完整 final 标志或任务身份。外键会限制 job_id 必须存在，但 FAILED 可被该更新改为 DONE；DONE 再调用时 jobs 行保持 DONE，results payload 仍被替换。
- 探针：真实 SQLite store 调用结果 producer，payload 含错误 task 标识和 `is_final=false`，数据库仍有 DONE 与可读结果；再调用一次覆盖已有 DONE payload。
- 限定：当前 E2 没有实际 runner 调用此函数，不能将未来 runner 不存在算作本卡缺陷。风险是在 E3 消费当前 producer 时可伪 DONE/改写终态结果；所以是合同缺口 P2，不虚构当前 HTTP 外部可触发路径。

### P2-3 空数组 `options` 被静默归一化成默认参数

- 位置：`core/server/http_server.py:303-308` 创建上传时使用 `payload.get("options") or {}`，导致空数组/空字符串等 falsy 错类型变成合法空对象，绕开 `normalize_options()` 的类型拒绝。
- 探针：真实 HTTP 请求 JSON `{"options":[]}` 得到 201 并按默认 options 创建记录。
- 后果：与 R2“错 type 不得以 falsy 默认代合法参数”冲突，输入错误被静默接受。正常对象和 `None` 默认合同无需改动。

### P2-4 新 HTTP 数据目录和 SQLite 文件权限受 umask 影响而非私有

- 位置：`core/server/http_store.py` 的数据目录、SQLite 和 source 文件创建流程没有显式限制目录/DB mode。
- 探针：当前 shell umask `0002` 下使用真实 store 创建目录及数据，得到 data dir `0775`、sources `0775`、lock `0664`、SQLite `0644`、source 文件 `0600`。DB 和目录可被同机其它用户读取/遍历。
- 限定：本机 internal、单用户定位降低实际暴露面；但 R7 明确要求 HTTP source/DB 私有权限。此探针证明结果受环境 umask 影响，未声称所有默认 umask 都产生同一 mode。

### P2-5 文档测试命令没有声明新增 HTTP 测试依赖

- 位置：`docs/development/testing.md:3-7` 的“完整验证命令”未安装 `httpx` 与 `aiohttp`；该文档第 31 行另说明 HTTP 测试需要两包，但没有把依赖补进完整命令。CI `.github/workflows/ci.yml:30` 显式安装 `httpx==0.28.1` 和 `aiohttp==3.14.3`。
- 真实消费结果：按文档依赖说明在隔离环境对 `tests/test_http_file_tasks.py --collect-only` 执行，因 `ModuleNotFoundError: httpx` 退出；精确依赖环境下目标五个 HTTP 测试套件为 `51 passed in 17.63s`。
- 后果：按文档执行的本地验证无法收集 E2 测试，而 CI 依赖齐全会绿，文档与真实 CI 消费环境不一致。CI 不会因此假绿：工作流确实安装了两个依赖。

## P3 / 不采取行动

OCR 标记的未使用 `os` import 是 P3，无运行后果，不单独要求修改。其他审查未发现应为理论畸形输入新增 fallback、重试或防御层的理由。

## 可靠确认与重启窗口核验

- `append_bytes()` 先按数据库 offset 截断未确认尾部、写文件并 `fsync`，再以条件 SQLite 事务确认新 offset；事务成功后 route 才返回 204（`http_store.py:450-494`、`http_server.py:337-343`）。
- 故障注入 SQLite `BEGIN IMMEDIATE` 失败后，真实 PATCH 返回 500，offset 仍为 0、文件留有 32768 字节未确认尾；杀死进程并由新服务进程重开后读 DB offset 0，重传后返回 204，最终源文件字节与原始输入完全一致。
- 另在 offset commit 完成、HTTP ACK 发出前 SIGKILL。新进程 GET 读到 offset `16384/16384` 与完整文件，旧客户端未收到 ACK。这证明确认已落盘时恢复基于数据库前缀，而不是声称旧客户端收到了响应。
- 恢复实验跨真实 server process、SQLite 和文件；不能用同进程新连接或重建 listener 代替。结果完整性只证明已确认上传源，不证明未来模型 runner。

## 重启、真实消费者与边界

- 使用真实 SDK `submit_file_http` 和 HTTP server 完成一次测试协调器控制下的 Job/result，第一服务进程被 SIGKILL，第二服务进程 GET 读回 DONE 与完整结果；6144 source bytes/hash、offset 和 final payload 相符。协调器开关是测试注入，不是当前生产 runner；当前 `inference_available` 默认 false，真实 commit 应明确 503，这符合 E3 尚未实现的合同。
- 真实部署环境仅验证 Linux 6.8 x86_64、Python 3.12.3；Windows/macOS 未运行。Windows `pwrite` 是基于官方平台 API 声明和代码调用的静态结论。
- 没有声称实测真实 ASR 解码、E3 runner 并发或 E4 GC/全盘物理容量核算；这些不改变已经存在的 HTTP、SQLite、文件写入和进程监督缺陷。
- 真实生产服务未触碰。隔离探针使用自有临时目录、loopback/临时端口和独立子进程。

## OCR 与独立复核

OCR 结果 envelope 为 `reviewed_fallback`，原因是 primary quota exhausted、DeepSeek 备份成功；覆盖 complete，5/5 findings 已逐项独立复核。OCR 不是裁决来源：fatal HTTP、WS 退出码和准入队列由本地真实 consumer 探针确认；mailbox 和 result producer 由真实 worker/store 探针确认；`os.pwrite` 由 Python 官方文档与部署平台文件交叉核对。未复核的模型视角或未执行平台保持为上文明确的 unknown。

## 验证情况

- 目标五个 HTTP 套件：`51 passed in 17.63s`，使用 CI 对齐的 `httpx==0.28.1`、`aiohttp==3.14.3`。
- PR 38 当前仍为 draft；`gh pr view` 可见两个单元测试检查均为 SUCCESS。卡面另记 hosted verify 为 294 passed / 3 model skips；本审查没有重新读取原始 job 日志，因此不将该计数作为独立复验结果。draft 上没有执行或宣称通过集中 primary gate。
- 最终本文件按派发验收命令检查：`test -s docs/sessions/261001-http-files/reviews/E2-upload-review-A-verdict.md`。

## 踩坑

- 文档中的依赖说明与 CI 实际依赖不同；按文档隔离收集测试时先因缺少 `httpx` 失败，随后按 CI 精确版本重跑，区分了文档可复现性和 CI 结果。
- `fatal` 字段及错误日志看起来像监督信号，但只有真实子进程退出结果能证明 supervisor 消费了它；HTTP 与 WS 分别通过真实请求/启动失败验证。

## 绕过

- 全部探针在 `/tmp/http-e2-review-a-261001-*.py` 保留，使用真实 HTTP/TCP、SQLite、文件与子进程；不改仓库测试或产品实现。对失败和 crash 窗口使用新服务进程验证持久状态。
- 对 cancellation 明确分别测试两个完成顺序，各 5 次；并检查 aiohttp 的默认 handler cancellation 行为，避免把客户端取消测试误当服务端 handler 取消。

## 偏差

- 审查范围和冻结 SHA 符合任务卡；只新增 verdict。仅 Linux 做真实运行，macOS/Windows 与真实推理模型未执行，结果未外推。派发时主干基线 CI 记录不可用，继承红无法分类；本次仍核对到 PR 当前两个单元测试状态。

## 最贵一步

- 最耗时的是为“代码里写了 fatal/监督”建立真实进程结论：分别启动隔离服务子进程，实际发送 HTTP、注入未知异常、等 listener 就绪并检查进程退出码；文件确认则再跨 SIGKILL 和全新服务进程核对 SQLite offset 与 source bytes。静态阅读无法替代这些 producer 到 consumer 的路径。
