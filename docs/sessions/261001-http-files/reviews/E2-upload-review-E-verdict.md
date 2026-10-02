# E2 最终独立冷审查（冻结 H4）

failure-visibility: p1-found

## 范围与判定

- 对象固定为 `94878f4c587940c9e05754176f0d4e6f65868caf..66f1d63f5ab1d8b0e535f3f93632c421826ad65d`；当前 PR #38 仍须在远端保持 draft/open 且 head 为 `66f1d63f5ab1d8b0e535f3f93632c421826ad65d`。
- **Verdict：P1 found，候选不通过。** 最新输入错误收口仍有一个未覆盖的可触发进程崩溃路径，见 P1-1。
- OCR 产物可读，`status=reviewed`、17 条；以下逐条按本仓风险和实测重新定级。
- Task-Id 与 Fixes-Issue 在任务卡留空；不据此补猜。

## 最近增量四问（`d453fd2b..66f1d63f`）

1. **只修登记问题？是。** 代码仅把 `float(巨大 int)` 的 `OverflowError` 及 `json.loads` 表达式的 `ValueError`/纯 Python scanner `RecursionError` 映射到局部 400；对应测试覆盖溢出和整数字面量上限。
2. **新增未授权抽象？否。** 没有新增 helper、状态、配置或调用层。
3. **无依据增加状态/事实源/fallback？否。** 未新增状态、事实源或 fallback；异常 catch 限在相应表达式，没有改 `_wrap`、I/O done、WS 或 SDK。
4. **留下双路径？否。** 新增输入拒绝路径仍由现有 route/store/error wrapper 传递。

最后提交在 Git 文件名/统计层面仅为文档和进度记录撤回，无代码或测试文件改动；未读取被删除记录内容。

## P1 finding

### P1-1：孤立 Unicode 代理项使单个 create 请求关闭整个服务

- **Spec：** `docs/sessions/261001-http-files/design.md` §R3 第 40、44 行要求 HTTP options 使用受校验语义，create 参数错误返回 400；§R6 第 63 行要求未知任务异常进入监督链并非零退出。
- **代码证据：** `core/server/http_store.py:679-684` 只以 `isinstance(value, str)` 接受 `context` 等字符串；`http_store.py:416` 用 `ensure_ascii=False` 序列化，随后在 `http_store.py:447-452` 绑定 SQLite 时触发编码错误。`core/server/http_server.py:234-246` 将该异常当 fatal；`http_server.py:196-200` 结束 listener，`core/server/app.py:165-185` 让主服务以失败退出。
- **真实探针：** CPython 3.12.3、aiohttp 3.14.3、HTTPX 0.28.1；裸子进程环境、临时数据目录、独立端口、无模型。先后 POST 巨大浮点整数、4301 位整数字面量、合法请求，分别得到 `400 invalid_options`、`400 invalid_json`、`201`；随后发送带 JSON 转义 `options.context="\ud800"` 的字节请求，得到 `500 internal_error`，子进程捕获到 `UnicodeEncodeError` 并以 exit code 1 退出。失败请求未增加 DB 行或源文件。
- **P1 两问：** ①真实使用路径会触发吗？**会，已由完整 CapsWriterServer 子进程的真实 HTTP 请求实测。** 新建上传的 Bearer 是调用者自己选的 capability token，不是服务器端预先认证；裸配置解析实测 HTTP 复用默认 `0.0.0.0` 绑定。部署 ACL/防火墙是否额外限制访问未知。②触发后果可接受吗？**不可接受**：一个 HTTP create 输入使 HTTP 与 WS 共用的服务进程退出，影响其他在途业务。
- **建议：** 在 options 入存储前拒绝不可 UTF-8 编码的字符串并返回 `invalid_options` 400；补一个真实 HTTP 回归断言验证无 fatal、无落盘且后续请求可继续。无需扩大异常 catch。

## 其他差异与接受项

- **P2：并发重复 create 的状态码竞态（OCR #8）。** R3 要求重复 create 为 200。实际 port 隔离探针并发发 64 个同 key create，16 个被 handler 上限接受的请求均回 201，另 48 个回 429；SQLite 只存一条 upload，未出现重复文件/Job。原因是 route 先单独查询是否存在、再调用原子 create；测试只锁顺序重试。任务卡已接受 duplicate 201/只读预查询，本轮记 P2、不要求新机制。并发 commit 的重复状态码未探测。
- **P2：结果文本与 token 不变量未锁。** `docs/reference/protocol.md:90-98` 要求 `''.join(tokens)==text_accu`；`HttpStore.record_result` (`http_store.py:627-633`) 只校验 token/timestamp 数量。`tests/test_http_file_tasks.py:359-362` 的 producer fixture 本身给 `text_accu="你好世界"`、`tokens=["你","好"]`，不符合契约但被持久化/GET 测试接受。该 sink 在本增量仅为 E3 runner 预留，HTTP 正常 commit 仍 503；记 P2，建议 E3 接入时修正 producer fixture 并锁不变量。
- **P2：`job_failed` 的结果错误遗漏已存 error_code。** R3 第 49 行要求 `GET result` 的 409 带已存 `error_code`；`http_store.py:593-599` 只发 `job_failed`，`_error_response` 只输出通用字段。失败 Job 需后续 runner 才能产生；建议 E3 接入前补充响应字段与消费者断言。
- **P2：存储故障状态映射不完整（OCR #10）。** R3 第 45 行要求存储不可读返回 503；`StoreUnavailable` 只由已关闭连接的 property 抛出，普通 `OSError`/SQLite I/O 错误会落到 `_wrap` fatal/500。生产目录的故障触发情况 unknown；记 P2，暂不加兜底重试或 catch-all。

## OCR 17 条逐条裁定

| # | 工具标注 | 本仓判定 | 本仓证据、两问或实测边界 / 建议 |
|---|---|---|---|
| 1 | high double stop | 无 finding | 5 个隔离服务子进程各收一次 SIGTERM，均 exit 0 且 `HttpServer.stop` 只调用一次；正常路径未触发所述并发清理。 |
| 2 | medium double stop | 无 finding | 与 #1 同一路径、同一组实测；重复 stop 的异常链未发生。 |
| 3 | high normal shutdown exit | 无 finding | 同 5 次实测均 exit 0；真实 shutdown 中未见 `_serve_all` 把清理异常升级为 exit 1。 |
| 4 | medium 非 RuntimeError fatal | P3 | P1 探针确以非 RuntimeError `UnicodeEncodeError` 让进程非零退出；R6 只要求关键异常可见且非零，裸 traceback 仍 fail-loud，不构成这里的额外违约。 |
| 5 | high free-space | P2 accepted / E4 | R7 有物理余量合同；只在 create 采样 free bytes，且不按声明大小预留物理容量。目标部署 data dir/free space 未提供，触发规模 unknown；任务卡明确归 E4，本轮不加机制。 |
| 6 | medium directory fsync | P2 / crash type unknown | 新建 source 只 fsync 文件 fd (`http_store.py:435-441`)，没 fsync 父目录。只有断电/内核崩溃可丢目录项；部署事故记录与崩溃类型不可读取/实测，不能据此升 P1。 |
| 7 | medium EXPIRED 文件 | P2 accepted / E4 | 过期只更新状态；`tests/test_http_store.py::test_expired_upload_is_410_and_metadata_kept` 明确断言源文件仍在。实际累积量因生产 data dir unknown；源清理属于 E4。 |
| 8 | medium duplicate 201 | P2 accepted | 真实并发探针 16 个已处理同 key create 全回 201，库里仍仅一条 upload；接受为状态码竞态，不要求本轮增机制。 |
| 9 | low serve annotation / double stop | P3 | `serve -> None` 的注解/文档未反映 fatal 时重抛；异常确实重抛进监督链。double-stop 与 #1 一样未实测触发；只属注释精度。 |
| 10 | medium StoreUnavailable | P2 | 代码/契约差异见上方；生产存储故障未测，unknown，不升 P1。建议仅在之后有界修复已知存储错误映射。 |
| 11 | medium 装配失败清理 | P3 | `prepare()` 对 `HttpStoreError`、`OSError`、`RuntimeError` 关闭 worker，其他 schema/SQLite 异常可绕过显式 close；当前 app 的装配错误直接 fail-fast 退出。真实模型子进程未启动（任务禁止模型），未证实有存活残留，不升 P1。 |
| 12 | medium close 30s | P2 / trigger unknown | `run_sync` 默认 30 秒；在途同步 I/O 超时会让显式收尾失败。真实生产盘上超过 30 秒的 I/O 未测，运行耗时与数据目录 unknown；R6 要等待受限 I/O，建议后续结合真实慢盘证据处理。 |
| 13 | high 启动失败后子进程 | P2 accepted / current-use-unverified | `ProcessManager.start()` 在解析 HTTP 配置前；本地探针按要求未启动模型，无法验证部署模型加载残留。任务卡已接受 trusted-owner 配错启动残留属 P2/current-use-unverified；父进程启动失败仍非零。 |
| 14 | medium chmod 共享目录 | P2 / trusted config | POSIX `open()` 会 chmod 配置的 data dir 与 sources 子目录为 0700；实际 CW_HTTP_DATA_DIR 未提供，是否指向共享目录 unknown。路径是 owner 配置，按 trusted-config 规则不升 P1。 |
| 15 | low aiohttp 版本提示 | P3 | runtime 只检 import，但 requirements-server 和 CI 都 pin `aiohttp==3.14.3`；本探针也实用该版本。非受支持的手工环境才会出现提示与版本不一致。 |
| 16 | high lone surrogate | P1 | 见 P1-1；Q1 已在真实 HTTP 子进程确认，Q2 为全服务进程退出、不可接受。 |
| 17 | low unused import | P3 | `http_server.py` 的 `os` import 无调用，`HttpServer.app` 字段未读；无运行后果和 spec 不变量，低优先级清理。 |

## 全量范围与未知

- 已按配置/默认关闭、鉴权与 ID、create/零写入、PATCH fsync-offset-ACK、commit 唯一 Job/503、GET 状态与错误、idle/限额/背压、I/O 取消/崩溃/重启、WS 并行与关闭、SQLite/文件权限/资源、测试夹具和 producer/跨进程边界检查主 diff。
- 正向证据：CI 显式安装 aiohttp/httpx；config 子进程测试使用裸环境；SDK 上传测试走真实 HTTPX SDK 并逐字节核对落盘；supervision 测试用真子进程覆盖 offset 崩溃窗口和持久结果新进程读取。结果内容契约仍有上面的 false-green fixture。
- `git diff --check 94878f4c..66f1d63f` 无输出（通过）。未运行测试套件或 Gate；未修代码。
- 生产 crash history、真实 HTTP data dir 的容量/慢盘、部署防火墙与实际模型子进程均不可测，相关结论明确保留 unknown。
