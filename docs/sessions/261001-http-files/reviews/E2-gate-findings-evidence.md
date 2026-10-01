<!-- delegate-outcome: succeeded -->
# PR 38 主审 finding 证据与严重度分诊

failure-visibility: na

## 结论

已从 Silo 取回 run#36932561558、attempt 1 的 canonical primary audit，绑定 PR 38、head d453fd2b5c7f2cbbe807bb4c4d486b936cf55b12、job 110605289883、reviewer codex-sub。原始 verdict 为 fail，共 4 条 finding：1 条 major、3 条 minor。独立隔离探针确认 1 条真实 P1：超大整数参数会返回 500 并让 HTTP listener 以 OverflowError 退出；本卡只记录证据，不修代码、不处理 disposition。

当前 gate 的 primary step 9 Run review-primary 失败，quality 的 Tests step 成功；unit run#36921941913 的两个矩阵 job 均成功。现有 Gate failure 是 primary_findings，不是测试红。

## 权威产物及来源绑定

PR 仓库数字 ID 971940657 由 GitHub API 核实。run#36932561558 的 API 元数据为 pull_request、attempt 1、head d453fd2b5c7f2cbbe807bb4c4d486b936cf55b12；job 110605289883 gate / primary 失败，Run review-primary 第 9 步失败。该 run 引用 zlxlabs/gate/.github/workflows/gate-v2.yml@39c889050e6a9b25f868738cdae074475ab1559c。该 workflow 的 Upload canonical primary audit 步骤把以 repository ID、PR head、run ID、attempt 生成的审计文件写入 Silo d14 前缀。

canonical 对象键：
ci-artifacts/d14/971940657/primary-audit-v2-971940657-d453fd2b5c7f2cbbe807bb4c4d486b936cf55b12-36932561558-1/primary-review-audit.json

只读来源为 Silo mcli reader；命令形态为 mcli cat silo/ci-artifacts/d14/971940657/primary-audit-v2-971940657-d453fd2b5c7f2cbbe807bb4c4d486b936cf55b12-36932561558-1/primary-review-audit.json，stdout 写到私有文件后计算 sha256，不回显原始报告。对象大小 10864 bytes；SHA-256 为 ea8146f7be511f1185659cc7ea61d1ad6d3d8fdaddc4451911cac9a410335799。私有完整原始副本路径与 mode 600 读回记录存于派发完整报告。

读取后白名单字段确认 schema_version=2、pr=38、repository/repository_id、head_sha、run_id、run_attempt、job_id、reviewer、verdict 均与目标一致。candidate_commit_sha 为 a1f8bff2924e621143de945ce2f1b37155e25073；GitHub API 显示其父提交恰为 base 94878f4c587940c9e05754176f0d4e6f65868caf 与 head d453fd2b5c7f2cbbe807bb4c4d486b936cf55b12。它是该 pull_request 的合并候选，非 head 错配。

判据先经已知空对象校验：当前 exact prefix 查询 rc=0、1 条 JSON 行、stderr 0 bytes；known-empty attempt 999 查询 rc=0、stdout/stderr 均 0 bytes。同一个 jq 正向谓词在当前对象上通过、在已知空输入上按预期返回 false；query error 与 query not found 分开记录。GitHub artifact 列表为空不作为“无 finding”证据。

## Findings 与两问

### 1. correctness-http-options-overflow-crash

工具标注：severity major，redline 崩溃。来源 finding ID 原样保留。

规范与代码：design.md R3 要求非法 options 在创建受理前按 400 invalid_options 拒绝。core/server/http_store.py:691-695 把数值交给 float(value)；400 位整数会在转换时抛 OverflowError。core/server/http_server.py:234-236 将未分类异常映射成 500 并调用 _mark_fatal；supervision 随后停止 listener。此 finding 指向本 PR 新增的 http_store.py。

第一问，真实当前候选是否触发：是。以 aiohttp 3.14.3 隔离 venv、真实 aiohttp TCP listener、port 0、临时 SQLite/文件目录、假 Bearer 值、无模型音频发送 POST /v1/uploads。请求体为 520 bytes，size_bytes=1、sha256 为 64 个 0、options.seg_duration 为 int("9" * 400)；实际请求体 SHA-256 为 1bf27691aee22d13dda7ca3a39f6de58131e766a4c1f6f349016df81f6c42764。结果是 HTTP 500/internal_error，HttpServer.fatal 与 serve() 退出类型均为 OverflowError。精确 payload 与脚本只保存在私有临时目录；argv、环境和副本路径列在派发完整报告。

第二问，后果是否可接受：否。一个已授权 HTTP 客户端的非法数值参数会停掉 listener，并经 CapsWriterServer._serve_all() 令服务非零退出，违反 R3 的局部 400 拒绝边界。

测试锁定：tests/test_http_file_tasks.py 的 options 用例覆盖类型、合法范围和普通非法范围，没有浮点范围外整数；现有测试不会因该缺陷变红。本地判定：确认 P1；本卡不修。

### 2. reliability-http-capacity-admission-underreservation

工具标注：severity minor，redline none。

规范与代码：design.md R7 要按源声明、数据库/结果预算和物理余量核算，并保留 2 GiB 实际自由空间。core/server/http_store.py:393-402 累加声明源总量并限制为 16 GiB，但物理空间判断只看当前 _free_bytes() 是否低于 2 GiB，没有把本次或既有声明量从实际自由量中扣出。

第一问，真实当前候选是否触发：隔离的真实请求路径可以触发；真实部署是否使用低自由量数据卷未核实。使用临时 SQLite 与 port 0 TCP，令隔离 store 的自由量返回 2147483649 bytes，连续提交两份各 1073741824 bytes 的合法最大 size_bytes 请求；两个实际 114-byte POST 均返回 201，SQLite 有两个 UPLOADING 行，源文件仍为 0 bytes、Job 数为 0。该模拟不填充磁盘。临时卷实测自由量为 88591339520 bytes；QA 明确记载物理容量未测，PR 正文也明确没有生产部署。

第二问，后果是否可接受：低容量真实部署若存在，准入会消耗安全余量，后续写入可能碰到空间不足，与 R7 预留意图不符；当前仅在模拟自由量下验证了准入，未实测物理耗尽或生产卷，也没有静默 ACK 证据。因此不升 P1，记录为 P2 / 当前使用触发未验证。

测试锁定：QA 第 9 组明写“未测物理容量”；现有测试未覆盖真实物理空间预留。本卡不修、不登记外部 disposition。

### 3. testing-http-dependency-skip-masks-failure

工具标注：severity minor，redline none。

规范与代码：tests/test_http_file_tasks.py:30-31 与 tests/test_http_supervision.py:25-26 在模块级调用 pytest.importorskip("aiohttp")；依赖缺失时整个模块会 skip。design.md 声明启用 HTTP 必须满足 aiohttp 运行依赖，QA 要求不能把缺依赖 skip 当成通过。

第一问，当前真实 CI 是否触发：否。PR head 的 .github/workflows/ci.yml 固定安装 aiohttp==3.14.3 再运行 pytest。unit run#36921941913 的两个 matrix job 中“安装依赖”和“运行 pytest”步骤均 SUCCESS；最新 gate run#36932561558 的 quality / Tests 步骤也 SUCCESS。当前裸 shell 确实未安装 aiohttp，但它不是上述 CI 消费环境。

第二问，后果是否可接受：依赖缺失时测试套件会失去 HTTP 覆盖且可能仍以 skip 通过，属于条件性测试空洞；required CI 当前有明确安装步骤，未证实本次 gate 的 tests 被跳过或判红。当前判 P2 / CI 触发条件不成立。现有 importorskip 本身不能锁住“依赖缺失应失败”，但 CI 安装步骤约束了真实消费环境。

### 4. reliability-http-startup-process-leak

工具标注：severity minor，redline none。

规范与代码：core/server/app.py:136-142 先启动 process_manager，再解析 HTTP 配置和调用 HttpServer.prepare()；该初始化段在 start() 的网络循环 try 块之前。start_server.py 直接调用 CapsWriterServer().start()。在初始化抛错前没有 process_manager.stop()。

第一问，当前真实使用是否触发：条件路径隔离复现成功，当前部署触发未证实。使用当前裸 Python（缺 aiohttp）、临时绝对数据目录、显式 CW_HTTP_PORT=45761，调用真实 CapsWriterServer.start()；替代 process_manager 只启动一个 sleep 60 的 fake worker。HttpServer.prepare() 抛 HttpServerError 后 fake worker 仍存活，stop_calls=0；探针确认后立即终止子进程。当前 shell 中 CW_HTTP_PORT 与 CW_HTTP_DATA_DIR 均未设置，PR 明确 HTTP 默认关闭、没有生产部署；QA 中启动失败清理仍待测，systemd cgroup 清理由本卡未核实。

第二问，后果是否可接受：在手工启动且 supervisor 不清理子进程的配置错误场景下，fake worker 可残留；正常 owner 配置应配套 HTTP 依赖，且真实 systemd 清理行为未取证。该 finding 是受信配置/启动失败窗口，真实当前服务后果未证实，判 P2 / 当前使用触发未验证。

测试锁定：tests/test_http_supervision.py 将 process_manager.start/stop stub 为 no-op，不会检出该生命周期问题。

## 范围、结论状态与限制

四条 finding 路径均属于 base 94878f4... 到 head d453fd2... 的 PR 变更；不是已合入 main 的旁支假象。PR 正文锁定 HTTP 默认关闭、写入/受理前验证、无推理 runner 时 commit 返回 503 且不建 Job；不把模型意见外推为完整 HTTP 识别已可用。

主审 finding 与 tests failure 分开：run#36932561558 quality Tests success；失败发生在 primary Run review-primary；aggregator 记录 primary_findings。主干基线 API 在派发时返回失败，继承红状态未能判定；本卡没有生成新的 gate/test 红。

本卡只读该轮 canonical report 并实测其列出的触发路径，没有启动 OCR、新模型 review、全量审查、formal test suite、Gate rerun、PR/Issue/disposition 写操作或生产服务。本卡唯一仓库改动是新增本报告；没有修复 finding。
