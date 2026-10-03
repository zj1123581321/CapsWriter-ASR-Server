# M4-C1 容量与结果预留独立首审

审查风险级别：internal。固定范围：`e066930ef38aabe8e5051c9256463646f62a7186..14eecc70dabd17823f6996dc57c652b25d195dea`。本轮只判 C1 容量与结果预留；C2 周期源清理、M6/M7 不视为已实现。未读实现方 report、stdout、会话或推理材料；未追后续 HEAD。

failure-visibility: p2-only

## 结论

固定范围中未确认 P1。发现两个可复现的 P2 容量口径缺口；另有一个非阻塞的线性扫描性能风险。按本仓 internal 判级均不阻塞本轮合并，但不得把本 verdict 写成“无问题”。

## Findings

### P2-1：PATCH 与创建事务的 SQLite 增量没有完整纳入 DB guard

位置：`core/server/http_store.py:651`（PATCH 只检查物理余量后继续写源文件和 offset 事务）；创建入口在 `:586` 检查的是写入前的 DB/WAL/SHM 快照，没有预留新增 uploads 行自身的 SQLite/WAL 页。

违反不变式：#2 要求 DB+WAL+SHM 实际文件占用、全部 QUEUED/RUNNING 结果预留和新增 Job 预留之和不超过 2 GiB。`append_bytes` 的容量满拒收只看 `_check_physical_margin()`；随后它执行文件写入、fsync 和 `confirmed_offset` 更新事务，未查 DB guard。创建入口虽调用 `_check_storage_guard()`，但检查值不含本次 INSERT 的 SQLite/WAL 增量；额度等值放行后，该事务可把文件占用推过 guard。

真实证据：隔离 HTTPX→aiohttp TCP 探针把 DB guard 缩至实际 DB/WAL/SHM 字节减 1。真实 PATCH 返回 204、source 写入 4096 字节且 offset 到 4096；DB/WAL/SHM 从 94,576 增到 98,696 字节，仍高于 94,575 的 guard。独立 SQLite 创建探针把 guard 设为写入前的实测值 82,216；等值创建被放行后，DB/WAL/SHM 增至 94,576。没有制造 2 GiB 文件。

本仓判级：P2。正常 HTTP 写入路径可触发，且局部证据是实际 producer 与真实 SQLite；实测越界是 SQLite 元数据/WAL 增量，未观察到数据损坏，物理余量闸仍独立工作，因此不升 P1。当前测试 `test_create_guard_edges_have_no_side_effect` 锁住写前等值放行，却没有断言写后实际 DB/WAL/SHM 仍在 guard 内。

### P2-2：任意状态的 ENOENT 都释放源声明预留

位置：`core/server/http_store.py:436-440`。`_iter_present_sources()` 只查询 `source_name,size_bytes`，对任何记录的 `FileNotFoundError` 都 `continue`，没有检查 upload/job 是否为可安全释放的终态。

违反不变式：#1 只允许安全删除的终态源释放预留。活动 UPLOADING 记录的源文件若意外缺失，也会被按“已安全删除”处理；真实查询错误虽会抛出，但 ENOENT 本身不足以证明清理安全。

真实证据：隔离真实 HttpStore 探针创建声明长度 3,840 字节的 UPLOADING 记录，删除其源文件后读取状态仍为 UPLOADING，而 `_reserved_source_bytes()` 从 3,840 变为 0。C1 测试只验证删除 DONE 源后的释放，没有覆盖缺失的活动源。

本仓判级：P2。需要本地文件被外部删除或存储丢失才触发；源已经缺失，旧上传本身不可继续，但容量账会把仍登记的非终态声明整体放出。没有代码删除文件/记录/结果，也没有数据毁损证据，故不升 P1。

### P2-3：每次 PATCH 扫描并 stat 全部历史 upload 源

位置：`core/server/http_store.py:436-441`、`:451-458`、`:505-515`、`:651`。每个非空 PATCH 都经 `_unmaterialized_source_bytes()` 遍历 uploads 并对每个存在源执行 stat；create 的源预留与物理余量分别扫描，历史终态记录不会在 C1 中删除。所有操作共用一个受监督 I/O worker，因此扫描时间会阻塞同线程上的其它存储请求。

违反的目标：这是新增实现的性能风险，不违反容量数值本身。数量会随长期保留的上传记录增长；目前没有真实部署数据目录可供量最大行数/耗时，故只按 P2 记录，不推测为 P1，也不要求在本卡引入缓存或第二账本（卡面明确禁止）。

## OCR 工具意见对照

- OCR envelope：`reviewed`，profile `minimax`，model `MiniMax-M3.1-Flash-Preview`，reason `primary_selected`，coverage `complete`，findings 共 3 项。工具回传文本在展示时被截断；可核对 status/reason 和已显示的意见，但第一项 finding 的工具严重度无法从回传中确认，不在此猜测。
- OCR 对 PATCH/DB guard 的意见由上述真实 HTTP 与 SQLite 探针确认，本仓判 P2。
- OCR 对全量 stat 的性能意见是可触发的结构性风险，但没有生产数据量和实际耗时；本仓判 P2、非阻塞，不引入缓存/第二账本。
- OCR 对 EXPIRED 未写尾永久占用物理余量的意见，工具标为 medium。按本卡不变式 #3，物理余量必须扣除“全体未物化源尾”，且 #1 明确保留 EXPIRED partial 的声明预留；因此该意见不能作为违反当前锁定契约的代码 finding。它暴露的是契约的潜在误拒：EXPIRED 已不可继续写入，若仍要求长期扣其未写尾，过期 partial 可令真实磁盘仍有余量时持续返回 507。此处记为需主脑裁决的规格风险，不擅自改写 C1 契约。

## 六项不变式逐条核验

1. **源预留**：存在的 UPLOADING/EXPIRED/COMMITTED 源按声明长度计费，尾不看 offset；非 ENOENT stat 错误上抛，真实测试覆盖 EXPIRED partial。未通过：见 P2-2，ENOENT 不验证终态安全条件。
2. **DB/WAL/SHM 及结果预留**：新 Job 受理时计入 QUEUED/RUNNING 预留；DONE/FAILED 与 pending 释放有真实库测试；重启收敛 FAILED 后 pending 清零。真实 SQLite `page_size=4096`、64 MiB 近上限 JSON 实测提交增量 134,889,984 B，预留 141,902,242 B，低于预留 7,012,258 B；16 MiB 多结果与关闭 checkpoint 的 WAL 累积探针也通过。该证据只覆盖真实测到的状态，不将有限样本写成全历史状态证明。SQLite 官方文件格式说明支持页内/溢出页模型：[SQLite Database File Format](https://www.sqlite.org/fileformat.html)。`RESULT_COMMIT_EXTRA_PAGES=32` 的“实测 ≤20 页”仍是有限样本；所有历史 b-tree 状态的严格最坏页数未独立证明。另见 P2-1。
3. **物理余量**：create/PATCH/new commit 均在相应源/任务写入前查询余量；检查用真实文件长度计未物化尾，写入尾不重复收费，其他上传尾入账；额度与余量等值放行。真实 TCP 两并发 PATCH/commit 的拒收均未落盘、未推进 offset/Job。EXPIRED 尾的永久扣减是否应继续属于规格风险，见 OCR 对照。DB guard 的写后增量见 P2-1。
4. **507 与既有查询**：创建拒收无 upload 行/源文件，PATCH 拒收无源字节/offset，commit 拒收无新 Job；容量满时 commit 重放及旧 Job/result 查询保持 200，DONE result 可读、FAILED result 为 409 + 已存 error_code。create 幂等优先于容量检查已由代码顺序确认，但未单独跑满容量 HTTP 重放用例。
5. **终态与重启释放**：`record_result` 在同一 SQLite 事务中写 result 与 DONE，单 I/O worker 经 `HttpServer.record_result` 调用；失败终态更新与 pending 查询共用 jobs 状态，重启将旧 QUEUED/RUNNING 改 FAILED。真实结果提交前后测量 DB/WAL/SHM；未见静默预算释放窗口。
6. **串行与线程边界**：HTTP route 只把写入送入 I/O worker；真实 TCP 并发测试记录 store 区间、DB/offset/文件副作用，五轮各两用例全部通过。新增扫描/PRAGMA/stat 均在 store worker 内；本轮未发现缓存表、第二账本、新配置、retry/fallback。

## 验证与范围限制

- `uv run --no-project --python 3.12 --with numpy --with rich --with websockets --with colorama --with pytest==9.1.1 --with soundfile --with pytest-asyncio==1.4.0 --with aiohttp==3.14.3 --with httpx==0.28.1 pytest tests/test_http_capacity.py -q`：`20 passed in 2.14s`，无 skip。
- 同一独立命令将测试文件替换为 `tests/test_http_store.py`：`16 passed in 1.15s`，无 skip。
- 两个真实 TCP 竞争用例（commit 与 PATCH）各运行 5 次，每次 `2 passed`，共 10 次执行全部通过。
- 真实 SQLite 输出另见全量报告：64 MiB 近上限结果、16 MiB 多结果默认 checkpoint、256 KiB 多结果 WAL 累积的实际 DB/WAL/SHM 字节及预留比较。
- 基线红验：`scripts/git/scratch-worktree.sh` 从 `e066930...` 创建临时树，复制新增容量测试文件，运行 `test_physical_margin_thresholds_on_create_patch_and_commit`。结果为预期 `AssertionError`（基线拒绝不了 margin+size−1 的 create），不是导入或依赖错误；scratch helper 已清理临时树。
- `git diff --check HEAD^ HEAD`：退出码 0。
- 未跑全仓测试、CI 或生产服务；PR #60 仍为 draft，主审尚未执行，远程 CI 结论由主脑消费。派发时 `gh api` 基线失败，因此继承红无法判定。

## 评审四问

- **踩到的坑**：`-q` 隐藏真实 SQLite 测量行，需对窄用例加 `-s`；OCR 首次输出超出工具展示上限，不能把截断文本当完整 envelope。
- **闸与绕过**：结果预留的当前实测覆盖有余量，但 PATCH/创建的自身 DB 页增量未被计入；活动源 ENOENT 被误当作安全释放。两者均有隔离真实证据。
- **与卡面的偏差**：没有改实现，只新增 verdict/progress；C2、M6/M7 与生产环境留在本轮范围外。OCR 的 EXPIRED 尾意见与本卡锁定的“全体未物化尾”冲突，作为规格风险上报而非擅改结论。
- **最贵的一步**：OCR 主腿等待 772.565 秒；真实 SQLite 与小常量 HTTP 探针本身均在隔离目录完成。

## 接手与状态

- pickup：当前工作树无交接单；无接手锚点。archive 摘要：`summary: orphan 0 owned 0 unattributable 0 too-new 0 recent-7d 0 stale-over-7d 0 missing_ledger_repos 0`，巡检项未展开，需要时跑 `/worksite-audit`。
- memory 探针：报告不可用（`memory_dir_mismatch`）；未将本机恢复命令写入审查产物。
- gh 收件箱 5 个开放 issue；#57 标题涉及设计文档引用未入库临时脚本，但本轮指定的 design.md/qa.md 无 `tmp_*.py` 引用；其余 issue 与本容量范围无直接关联。
