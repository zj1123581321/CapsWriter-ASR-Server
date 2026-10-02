<!-- delegate-outcome: succeeded -->
# E2 最终边界复核（冻结 H5）

## 结论
verdict: **PASS WITH P2 BACKLOG**；未发现 P1。
failure-visibility: p2-only

- 冻结对象：`94878f4c587940c9e05754176f0d4e6f65868caf..e1fa9796a314f593e149c050cb92bae5639911b9`。
- H0 增量四问对象：`66f1d63f5ab1d8b0e535f3f93632c421826ad65d..e1fa9796a314f593e149c050cb92bae5639911b9`。
- H1 只在受理边界增加了三个 options 字段和两个实测可达 header 的 UTF-8 局部拒绝；删除未使用的 `os` import 与 `HttpServer.app` 字段是无行为净减法。没有新增状态、抽象、fallback、重试或第二条写入路径。
- 真实隔离探针逐一发送 `model`、`language`、`context` 的 `\ud800`，以及原始非法字节的 `Authorization`、`Idempotency-Key`：均为局部 4xx；数据库计数、sources 目录均为零写入；随后合法 POST 返回 201，listener 未 fatal。
- `git diff --check` 通过；固定 Python 3.12、aiohttp 3.14.3、HTTPX 0.28.1 的 HTTP 回归集为 **71 passed, 1 warning**。warning 是 aiohttp `NotAppKeyWarning`，不改变行为结论。
- PR38 当前仍为 open/draft，head 为 `e1fa9796a314f593e149c050cb92bae5639911b9`，base 为 `94878f4c587940c9e05754176f0d4e6f65868caf`。未重跑 Gate；draft 的 primary/resolve/ocr 检查为 skipped，不能当作完整 gate 通过。

## 最近增量四问
1. **是否只修登记问题：是。** 行为改动仅覆盖登记的三个 options 字段与两个 header；两处未使用成员删除不改变事实源或消费者。
2. **是否新增未授权抽象：否。** 只是受理边界的局部 `encode("utf-8")` 守卫。
3. **状态、事实源、fallback 是否无依据增加：否。** H1 没有新增状态、事实源或 fallback。
4. **是否留下双路径：H1 没有新增；全 PR 仍有两处既存双路径，见下方 P2。**

## OCR 候选逐条重判
OCR envelope 为 `status=reviewed_fallback`、`profile=deepseek`、`coverage=complete`，5/5 verified；主腿 timeout，不能把工具 severity 当本仓结论。

| 工具标注 | 本仓判定 | 两问与真实证据 | 建议 |
|---|---|---|---|
| 创建 200/201 双路径：medium | **P2，接受不修** | 第一问：是。真实 listener 的 64 并发创建、20 轮压力实测出现每个 key 多个 201（如 `201:3, 200:61`），但每个 key 仍只有一个上传记录。第二问：错误状态码可见，未造成重复文件、数据损坏、越权或 fatal；低于 internal P1 红线。 | backlog：让权威 `create_upload` 返回 `created`，路由直接据此选 200/201。 |
| commit 202/200 双路径：medium | **P2，接受不修** | 第一问：是。64 个首次并发 commit 实测 `202:2, 200:62`，所有响应的 job_id 相同，SQLite 仍只有一个 Job。第二问：重复受理状态码错误但没有重复 Job、静默成功或数据损坏；低于 P1。 | backlog：让权威 `commit_upload` 返回“本次新建”事实，去掉预读。 |
| IntegrityError fallback 只比较 token：medium | **P2，接受不修** | 第一问：当前真实消费者不可达；单 I/O worker 串行所有 store 调用，第二进程又被 OS 独占锁拒绝；真实并发创建只暴露上一个预读竞态，不进入该回退分支。第二问：若未来破坏单写约束，可能把同 key 不同身份当成同一资源，但当前契约下不是 P1 触发路径。 | backlog：删除不可达回退，或复用完整 token/size/sha/options 身份比较。 |
| offset 竞争错误体缺 confirmed_offset：low | **P2，接受不修** | 第一问：当前真实消费者不可达；单 worker 串行，真实 SQLite busy 探针在 `BEGIN IMMEDIATE` 处失败而非进入 `rowcount != 1` 分支。第二问：只缺恢复提示字段，不改变已确认字节、不产生假 ACK；低于 P1。 | backlog：该分支若保留，应携带事务前可信 offset。 |
| `sqlite3.Error` 未包裹：medium | **P2，接受不修** | 第一问：是。真实损坏 `http.sqlite3` 探针得到 `DatabaseError: file is not a database`，且 `worker_closed False`。第二问：进程仍 fail-fast 非零退出，没有 fallback、静默成功或错误数据发布；影响是诊断包装与线程清理，不是 P1。 | backlog：在 `HttpStore.open()` 统一包装 SQLite 初始化错误，或在 `prepare()` 精确捕获后关闭 worker。 |

上述五条均已按“真实触发方式 / 后果是否可接受”重判；不因 P2/P3 派新机制或阻塞本轮。

## 全量 diff 不变式核对
- **配置与默认关闭**：`resolve_http_settings` 要求端口/绝对数据目录成对出现、拒绝 WS 同端口和非法端口；裸子进程测试覆盖默认关闭与启用失败。
- **认证、token、ID**：Bearer 仅存 SHA-256 指纹，未知 ID 与错 token 同为 404；真实 SDK/TCP 测试未把 token 放入 URL、日志或错误体。
- **创建、输入边界、零写入**：创建先校验身份/options，再独占创建空文件并 fsync，失败参数不产生 Job/文件；非法 UTF-8 探针核对真实 DB/source 终态。
- **PATCH 顺序与取消**：文件写入、fsync、条件 offset 事务、204 ACK 顺序正确；真实 TCP 取消先/后各 5 次，文件字节与 SQLite offset 一致；旧 offset、超前 offset、超块和 busy 均显式失败。
- **commit、唯一 Job、503**：完整长度/hash 核对后才进入事务；无真实推理协调者返回 503 且不建 Job；重复 commit 返回同一 Job。唯一状态码偏差已列 P2。
- **GET、错误、idle、限额、背压**：错误体含 request_id，存储/源数据不回退为空；半开 body 408、handler/body 上限、每块 idle 与背压恢复均有真实 TCP 测试。
- **监督、取消、重启、旧 WS**：未知 HTTP operation、取消后的 I/O 异常、坏目录、端口/目录锁、SIGTERM、WS runtime error 均由真实子进程验证；HTTP 默认关闭且未改 WS 默认入口。
- **SQLite、权限、资源边界**：WAL/FULL/foreign_keys/busy_timeout=0、目录/文件权限、单目录锁、文件与 DB 终态均有测试。资源数字按 R7 起始值实现；物理吞吐与 E3/E4 预留仍按 spec 标为后续未实测，不在本轮外推。
- **测试真实性**：测试使用真实 aiohttp TCP、真实 SDK、真实 subprocess、真实 producer payload、独立只读 SQLite 与实际文件字节；不是同进程手造消费侧 dict。隔离环境缺依赖时曾得到 skipped，随后用文档钉定运行时成功重跑，未将 skipped 伪装成通过。

## 证据边界
- 主干基线作业在派发时不可用，因此无法区分本次与基线同名首失败步骤的继承红/新红；本轮没有据此宣称 CI clean。
- 任务卡给出的 `docs/sessions/261001-http-files/reviews/E2-upload-review-E-verdict.md` 不存在于冻结 head，无法读取或引用其旧结论；本 verdict 的 options/header 结论来自本轮独立真实探针。
- 除上述五条 P2 backlog 外，没有发现会导致数据丢失、静默错误、服务崩溃、越权或他人数据损坏的 P1。

## 踩坑
- 系统 Python 缺 aiohttp，第一次目标测试为 1 skipped；改用项目文档固定的隔离 Python 3.12 环境后才取得有效绿证据。
- 直接从事件循环线程读取 worker 的 SQLite 连接会触发线程归属错误；探针改为通过真实 I/O worker 读取 stats。

## 绕过
- 不采用 OCR 的 severity；对五条候选逐条做真实请求或真实装配探针，再按 internal P1 两问分级。
- 不读取实现者报告、推理或对话；只用冻结提交、spec、消费者代码、测试产物和本轮探针。

## 偏差
- OCR 主腿 timeout，fallback 成功且 coverage complete；这是工具链偏差，不是被审代码的结论。
- 旧 verdict 文件在冻结提交中缺失；因此旧 P1 结论仅作为任务卡背景，不作为本轮证据。

## 最贵一步
重复 20 轮、每轮 64 并发真实 HTTP 创建与首次 commit，最终同时复现了 201/200 和 202/200 两处双路径状态码竞态，并核对了唯一 upload/job 与真实 SQLite 计数。
