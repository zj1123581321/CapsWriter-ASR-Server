failure-visibility: p2-only

# M4-C1 容量增量第二独立审查

## 审查范围与结论

- 固定对象：29194075a088dd00b8b3a3e8d8fc3a752f643f24..f882cd63c9701f7b5bbd0bc274d7b0dc86e08fdc（H1）。只审 C1；没有追 HEAD、修被审代码或访问生产环境。
- 范围内容量实现为 core/server/http_store.py；新增容量测试为 tests/test_http_capacity.py。二者在固定 H1 与原 14eecc70dabd17823f6996dc57c652b25d195dea 相同。审查结论只针对这个固定对象。
- 没有 P1。存在一个 P2 的容量策略风险，另有一项经实测的 P3 元数据边界和一项未量化的低风险扫描成本；两者均不阻塞本轮。无代码修复授权，以下只记证据与待裁决点。

## Findings

### P2 — EXPIRED partial 的不可写尾仍被扣入物理安全余量

- 位置：core/server/http_store.py:426-458、:505-523、:627-651；到期与拒绝路径在 :377-401。
- 不变式/契约：物理余量应预留仍可能写入的源尾、待处理结果与本次承诺，并在此之后保留 2 GiB。代码的 _unmaterialized_source_bytes() 对每个仍有文件的注册源都扣 声明长度 - 实际文件长度，不区分 EXPIRED。同一上传一旦到期，_row_for_token() 会持久标成 EXPIRED 并返回 410；GET、PATCH、commit 均不能再写它。文件若保留，16 GiB 的源声明配额仍应计费；但该源的未来写入尾已经不是可达承诺。
- 真实 producer 证据：独立 TCP 探针创建声明 1,024 B 的 partial、实际保留 73 B，再走真实过期 API 路径；GET/PATCH/commit 均为 410，offset 仍为 73，文件仍为 73 B，jobs 为空。物理余量函数仍将 951 B 作为未物化尾预留。当前隔离宿主机实测 free 为 46,706,479,104 B（余量线 2,147,483,648 B），同一过期源存在时新上传仍为 201；所以当前探针没有发生 507。
- 可触发后果：只要 EXPIRED partial 保留，且 free - 其他可写尾 - pending 结果 - 新承诺 落在 [2 GiB, 2 GiB + EXPIRED 不可写尾)，新 create 或新 commit 会被 507 拒绝，虽然物理余量本身仍至少有 2 GiB。C1 没有删除这些 partial 的路径；仅凭代码可确认这是结构性拒绝条件，当前宿主的 free 值没有触发它。此影响是容量可用性下降，不是数据破坏，故为 P2 而非 P1。
- 契约处置：当前 m4-plan §4.4 写的是所有“未写入尾”都扣减，§4.3 又要求保留 EXPIRED partial；实现与此字面口径一致。若产品意图是只为“未来还会写的尾”预留，则需要另行裁决并更新契约与测试：保留 EXPIRED 文件的完整声明长度源配额，但不再从物理余量扣其不可写尾。该改变涉及容量拒绝语义，本轮不改合同、不改实现，也不自动删 partial。

### P3 — create/PATCH 的 SQLite 元数据事务没有计入自身增长

- 位置：create 的预检查与上传行 INSERT：core/server/http_store.py:525-535、:586-621；PATCH 的物理检查与 offset 事务：:649-680。DB guard 公式为 :492-503，commit 的 Job 结果预留为 :714-717。
- 不变式/契约：若把 2 GiB DB/WAL/SHM guard 理解为写入后的绝对上限，create 与 PATCH 在使用预写快照检查后，还会写 SQLite/WAL/SHM 元数据；当前两条路径没有为自己的事务页预留。m4-plan §4 明确要求 PATCH 检查的是物理余量，DB guard 重点是待处理结果及新 Job，所以这里只能判小幅边界偏差，不能按 OCR 的“PATCH 绕过 DB guard”判为中等级别问题。
- 独立 TCP 证据：临时缩小 guard 后把阈值设成当时的实际 DB+WAL+SHM 字节。create 返回 201 并使实际值从 82,216 B 增到 94,576 B（+12,360 B）；再将 guard 设为 94,576 B，PATCH 返回 204、offset/file 均为 136 B，实际值增到 98,696 B（+4,120 B）。这是一个 SQLite 环境和小型边界样本，不证明所有版本/文件布局都只增加这些字节；没有在 2 GiB 实际数据库或部署数据上量过超额。
- 判级：仅在恰好触边时可观察到小幅 metadata/WAL 超额，实际幅度有限且缺少生产规模证据，不是 P1，也不应为此引入第二账本、缓存或配置。记 P3；若 2 GiB 是严格物理硬上限，应另明确是否要求为每类 metadata 事务留头寸。

### P3 / 接受为实现成本 — 每个非空 PATCH 对历史上传逐行 stat

- 位置：core/server/http_store.py:426-458，调用链经 :651；单 I/O worker 在 core/server/http_server.py:81-105。
- 事实：每个非空 PATCH 都经物理余量计算扫过全部 uploads 并逐文件 stat；create 还会为源声明配额再扫一次。C1 不删除历史上传行，故单次工作随历史行数增长。代码注释已经承认该代价；本轮没有部署数据行数，也没有生产性能采样，不能称为已发生的性能故障。
- 处置：按低风险/至多 P3 记录为已知成本，不要求缓存或第二账本，也不阻塞本轮。历史行规模的真实量未知。

## OCR 候选逐项裁定

OCR 前置扫描覆盖固定 range 中 7 个文件；只有 core/server/http_store.py 被模型选作默认审查路径，容量测试路径被默认过滤，前轮 review/progress 文件明确排除。主模型 MiniMax 在 900.110 秒超时；DeepSeek v4 Flash 备援完成，整体 reviewed_fallback，覆盖 complete，独立 verifier 对 4 条均标 confirmed。原始 envelope 保存在 /tmp/m4-c1-review2-ocr.json，stderr 在 /tmp/m4-c1-review2-ocr.stderr。模型/工具等级只是输入；以下为本仓复核后的判级。

| OCR 标注 | 代码事实与复核 | 本仓判定 |
|---|---|---|
| High：任何 FileNotFoundError 都释放上传声明预留，未检查 state。 | 代码确实无条件 continue。但本次范围内没有删除活动源文件的生产调用方：仅 create 失败时清理尚未登记的文件；安全终态源删除只在测试模拟 C2。已缺失的文件不占物理空间，PATCH 无 O_CREAT 会失败，commit 完整性核验返回 422；不会形成仍可写的隐藏尾承诺。该场景需要外部手动删源或存储丢失，依卡面要求未删除保留源做破坏性复现。 | 不接受为 High/P1/P2 容量 finding。源丢失时注册行与“safe terminal deletion”的关系仍是边界未知；没有证据表明当前 producer 会制造该状态，且不属于 C1 的可达正常路径。 |
| Medium：PATCH 不做 DB guard，也不预留事务自身页面。 | PATCH 按 m4-plan 明确检查物理余量；测试锁定真实 TCP 边界、offset/file 无变化和并发串行。独立 DB guard 探针证实在等值边界 PATCH 自身能增加 4,120 B。 | 仅将自身小幅元数据增长记录为上面的 P3。OCR 提议简单添加 _check_storage_guard() 不能阻止等值边界写入，须明确事务预留契约才有意义。 |
| Medium：create 对 INSERT 前的 DB/WAL/SHM 快照检查，不预留上传行增长。 | 真实 TCP 等值探针见上：201 后增长 12,360 B。当前 DB guard 测试锁定“超额拒绝”和等值放行，但没有锁定新增 upload 行的自身页增量。 | P3 小幅 metadata 边界，不升 P1/P2；有限样本不当普遍上界。 |
| Low：全历史行扫描与逐个 stat。 | 调用链与代码注释确认；卡面/仓库是内网单机单用户，未取得实际历史行数或生产耗时。 | 低风险已知成本，不新建状态或账本；不因理论规模要求实现。 |

P1 两问：第一问要求真实使用方式下触发；本轮在真实 TCP 临时数据目录量到了过期请求均 410、当前主机 free 约 46.7 GB 且新 create 仍 201。历史环境没有触发 P1 后果的证据，低余量的结构边界只作为 P2 可用性条件。第二问中，拒绝少量新 create 会影响容量可用性，但没有不可接受的数据丢失或结果错误；因此没有候选通过 P1 两问。没有访问部署机或生产目录。

## C1 不变式证据矩阵

| 不变式 | 代码位置 | 行为测试/真实探针 | 尚不能证明的部分 |
|---|---|---|---|
| 16 GiB source 声明预算覆盖保留的 UPLOADING、EXPIRED、COMMITTED 源；未确认尾不以 offset 抵扣；删去的终态源释放。 | _iter_present_sources / _reserved_source_bytes：http_store.py:426-449；create 入口：:525-535。 | test_source_reserve_charges_uploaded_expired_and_committed_sources（tests/test_http_capacity.py:233）、test_source_reserve_counts_declared_size_not_confirmed_offset（:289）、test_source_reserve_releases_deleted_terminal_source（:311）、test_restart_releases_pending_but_keeps_partial_and_skips_deleted_terminal（:912）。 | C2 清理不是本轮实现；测试在删除之后模拟其第一步。本轮未做 active source unlink 破坏性试验。ENOENT 对非终态行的语义见 OCR 裁定。 |
| 2 GiB DB/WAL/SHM guard 按实际占用加 QUEUED/RUNNING 结果峰值及本次新 Job 预留；终态 Job 将 pending 变为实测字节。 | 页几何预留：:467-490；_check_storage_guard：:492-503；新 Job 检查：:714-717；结果和 DONE 原子提交：:834-893。 | test_pending_jobs_reserve_result_and_wal_peak_and_commit_refuses_beyond（tests/test_http_capacity.py:367）、test_terminal_job_releases_pending_reservation_into_measured_bytes（:408）、test_failed_job_releases_pending_reservation_and_keeps_409（:430）、test_result_reservation_covers_real_sqlite_peak（:451）、test_result_reservation_holds_while_wal_accumulates（:498）；独立 SQLite 探针两条 DONE、两条 FAILED 分批结束，SQL pending 计数 4→0，实际 DB/WAL/SHM 总量 506,336→798,856 B，actual + pending × 独立预留 单调不增。 | SQLite 峰值来自当前 Python/SQLite 构建与有限容量样本；不是各版本、所有文件历史或任意事务形态的普遍上界。生产 cap 探针实测 67,108,827 B payload、page_size 4096，DB/WAL/SHM 增长 134,930,912 B，高于 payload×2 713,184 B，但低于独立页几何预留 141,902,242 B；这是单构建样本。create/PATCH 的少量元数据边界单列 P3。 |
| 物理余量扣除全部已承诺未物化尾、pending 结果/WAL、新承诺后仍不少于 2 GiB；create、PATCH、新 commit 检查，余量等值合法。 | 公式和边界：:505-523；create：:525-535；PATCH 落盘前：:627-681；commit 源核验前：:698-718。 | test_physical_margin_thresholds_on_create_patch_and_commit（tests/test_http_capacity.py:527）、test_physical_margin_accounts_every_committed_tail_not_only_own（:588）、test_physical_margin_does_not_double_charge_written_source_bytes（:616）、test_physical_margin_counts_unconfirmed_written_tail_as_materialized（:649）、test_disk_usage_failure_is_not_treated_as_available_capacity（:674）；两个真实 TCP 并发测试 :825、:869 各连续重复 5 次，均为 2 passed。另见过期 partial 的 951 B 不可写尾 P2 策略点。 | 实际 disk free 与用户部署时状态会变化；本轮只量本地临时数据目录，不访问生产。主机当前 free 约 46.7 GB，所以该过期 tail 样本没有导致当前 507。 |
| 507 拒收不留下新行/源字节/Job 或 offset 变化；容量满仍可幂等重放与读旧结果；DONE 200 完整结果、FAILED 409 原 error_code。 | create 准入在建文件和插行前：:525-535；PATCH 在文件写入前：:649-652；commit 在建 Job 前：:714-743；读状态和结果：:746-780。 | test_capacity_full_still_replays_commit_and_serves_old_reads（tests/test_http_capacity.py:715）、test_create_guard_edges_have_no_side_effect（:784）、上述物理余量阈值与真实并发测试；独立 TCP 满源额 create：首次 201、同身份重放 200 且相同 upload、不同新 key 为 507 source_reserve_full，无第二行/文件。 | 独立红验仅覆盖一条物理余量保护的被审路径，不等于穷举所有分支；无 HTTP capacity 文件 skip。 |
| 终态 pending 释放与 SQLite 落库一致；重启将 pending 收敛为 FAILED，不留永久预留；保留源继续计费。 | 重启收敛：:339-352；pending SQL 只计 QUEUED/RUNNING：:481-490；终态事务：:815-893。 | DONE/FAILED 释放测试 :408、:430；restart 测试 :912；独立分批真实 SQLite 探针覆盖不 checkpoint 时的 DB/WAL/SHM 变化。 | 多 Job 已实测但有限；不能从 4 个小结果推导所有长期 SQLite/WAL 状态。 |
| 检查与写入在单受监督 I/O worker 串行，网络事件循环不执行 stat/fsync；不加缓存/第二账本/配置/retry/fallback。 | worker 固定单线程：core/server/http_server.py:81-105；store 同步操作通过 worker：:216-237；容量检查只在 store 准入函数内。 | test_two_real_tcp_commits_race_for_last_storage_capacity（capacity 测试 :825）断言真实 store 区间不重叠且只有一个 Job；test_two_real_patches_race_for_physical_margin（:869）断言双请求不能超卖。 | 并发是两条真实 TCP 请求的窄探针，不扩展为全服务压测。此次未新增代码/helper/常量、缓存、账本、配置或 retry/fallback。现有容量 helper 直接服务本算法：source 遍历被声明额与物理尾两个公式共用；DB guard 和物理余量分别被 create/commit（物理余量也被 PATCH）调用。 |

## 测试、红验与基线

- Python 3.12 全量套件，websockets==15.0.1：446 passed、3 skipped、149 warnings，249.74 s。Skip 为两个 ForceAligner backend/model 缺失和一个 silero-VAD/onnxruntime 缺失；HTTP capacity 文件未 skip。
- Python 3.12 全量套件，websockets 最新解析版本 17.1：446 passed、相同 3 skipped、149 warnings，228.75 s。每轮 446 passed 是同一组 tests/，不是 CI 环境 gate 的替代。
- 已按要求对被审物理余量保护做独立红验：在 H1 临时 scratch worktree 注入 if available > FREE_SPACE_MARGIN_BYTES:，目标真实 TCP 测试于 assert refused.status_code == 507 触发 AssertionError，确认断言能识别退化；scratch worktree 已由 helper 清理且注册项不存在。脚本保留在 /tmp/m4_c1_red_verify.py。
- 基线 gh api 在派发时不可用；继承红无法判定。当前两轮全量本地套件无失败；红验的预期 AssertionError 单独记录，不算实现红。没有用 draft CI 绿色代替 SUCCESS 检查，本轮不创建/更新 PR，也不声称完整 gate 状态。

## 四问

### 踩坑

第一次从 /tmp 启动临时 DB guard producer 时忘了设置 PYTHONPATH，出现 ModuleNotFoundError: No module named 'core'；加上 PYTHONPATH=$PWD 后同一 probe 正常运行。此为探针启动环境问题，不是仓库实现失败。OCR 主模型超时后备援成功；必须读取最终 JSON，不能把初始空 stdout 当 clean。

### 闸与绕过

对照 source quota、DB guard、物理余量和 job slot 四种闸：source 与物理余量是在真实 TCP create/PATCH/commit 路径检查；pending result 通过 SQL 状态计数，在 create 与新 commit 检查；共享 slot 由共同预算入口约束。test_two_real_tcp_commits_race_for_last_storage_capacity 和 test_two_real_patches_race_for_physical_margin 实测单 I/O worker 内区间不重叠。没有发现绕过 source、物理余量或 job slot 的正常 API 路径；过期尾造成的是额外拒绝而非超卖。DB metadata 自身边界仅 P3，见上文。

### 卡面偏差

卡面固定对象、C1 范围、禁止生产/修代码、两个产物路径与 commit+push 均遵守。全量套件的本地依赖 Skip 如上；dispatch baseline 不可用导致 inherited red 未能判定。主干/PR 完整 gate 状态留给主脑消费；未开 PR/ready/merge/deploy。

### 最贵一步

固定环境两轮全量 tests/ 套件是最贵步骤：分别 249.74 秒和 228.75 秒。OCR 主 leg 另耗时 900.110 秒超时，fallback 404.380 秒完成，但每轮全量套件的墙钟时间更长。SQLite 尾段探针没有触发 checkpoint，另用大结果实测有 checkpoint 配置的 SQLite 版本；二者都只代表所用构建，不外推成所有环境证明。

## 后续单一动作

本卡审查已结束；Pi 主脑消费本 verdict 与完整报告，并另行决定是否裁决 EXPIRED 源尾的物理余量口径。无需在本卡分支追加代码或扩范围。
