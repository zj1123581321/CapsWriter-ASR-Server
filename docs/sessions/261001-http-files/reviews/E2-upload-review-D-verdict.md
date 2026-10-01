<!-- delegate-outcome: succeeded -->

# E2 HTTP 文件上传生命周期最终复核

failure-visibility: p2-only

## 结论

冻结对象为 94878f4c587940c9e05754176f0d4e6f65868caf..d453fd2b5c7f2cbbe807bb4c4d486b936cf55b12，当前 HEAD 与 H0 一致。审查 verdict 为 P2-only，无 P1。发现一处受理顺序偏差：无效分段参数仍会先触发一次幂等键数据库查询，随后才被共享规则拒绝；实测响应保持 400 invalid_options，没有文件、上传行或任务行写入。该顺序违反本卡明确要求，记录为 P2；本卡禁止改实现，因此未修改。

## 冻结范围与父提交来源

- 相对公共基线的完整变更量为 3,491 行新增、8 行删除，共 3,499 行。合入节点 574a1d48292ef6bd7de22552379f2875b042f7b9 的第二父提交为 94878f4c587940c9e05754176f0d4e6f65868caf；git merge-base 也返回该 SHA。
- 574a1d4..d453fd2 只改 3 个文件：core/server/http_store.py、tests/test_http_file_tasks.py、tests/test_http_supervision.py，新增 89 行、删除 86 行。生产增量只有 normalize_options 调用既有规则；其余是旧测试的必要骨架复用和拒写/合法边界扩展。
- 86be503 及 923d86c/5edc3f8 所在的 E2 实现链是合并前父线；94878f4 是另一父线上的公共共享分段规则。它们不是本轮第三份实现或本轮新增量。
- 四问：①改动限于登记的 R3 接入及已有多处消费者的测试骨架复用；②未增加状态、缓存、fallback、接口或第二条规则路径；③参数经原类型、有限值及默认归一化后传给 core.server.segmenter.validate_segment_params，但 route 的幂等键预查询早于调用，见下方 P2；④无上限模型与合法边界通过，拒绝样本返回明确 400 且不写状态，WS 调用仍读同一函数。

## P2：校验前读取幂等键

- 契约：本卡要求 R3 共同校验在任何文件、数据库或幂等键比较之前生效。
- 代码路径：core/server/http_server.py 的 _create_upload 先执行 SELECT 1 FROM uploads WHERE create_key=?，然后才调用 HttpStore.create_upload；core/server/http_store.py 在 create_upload 开头归一化并调用共享 validator。
- 真实输入及证据：通过真实 aiohttp POST 对已存在 key 提交 seg_duration=4，观测到次序 route_lookup → create_upload/normalize_options。响应为 400 invalid_options；uploads 仍 1 行、jobs 仍 0 行，已有文件及 offset 未变。
- P1 两问：真实请求会触发（是）；触发后造成的结果可接受（是：该读取结果不会返回给客户端、不改变状态，仍由 canonical validator 拒绝）。因此按 P2 记，不扩大为 P1，不在本 review 卡内改生产代码。

## 生命周期写读依据

| 写入动作 | 写入值与条件 | 读取方及判据 |
|---|---|---|
| 启动恢复 | _converge_restart 将 QUEUED/RUNNING 任务置 FAILED，写 server_restarted 和 terminal_at；不重放、不改 partial 文件和 DONE | job_record/get_result；test_restart_converges_queued_and_running_but_keeps_partial_and_done |
| 到期收敛 | _expire_if_due 仅把已到 expires_at 的 UPLOADING 行置 EXPIRED，不删除源文件 | GET upload 与容量计数；test_expired_upload_is_410_and_metadata_kept |
| 新建上传 | 先规范化参数，再查 store 内幂等键/容量；独占创建 <upload_id>.bin、POSIX 0600、fsync，随后事务插入 UPLOADING 与 offset=0 行 | GET upload、后续 PATCH/commit；test_create_is_exclusive_file_then_zero_offset_commit、test_repeated_create_key_is_idempotent_and_conflict_never_overwrites |
| PATCH 确认 | 按 DB 已确认 offset 截断尾部，短写循环写原始 bytes、fsync，再以旧 offset 为条件更新 confirmed_offset/updated_at/expires_at 并 COMMIT；COMMIT 后才发 204 和新 offset | GET upload、续传、commit hash 校验；store fsync/短写/SQLite busy 用例，取消×2序×5、crash offset 前后用例 |
| 显式 commit | 仅完整 offset、长度/hash 一致、容量允许且真实 coordinator ready 时，同一事务更新 upload、插入唯一 QUEUED job | GET job/result；无真实 coordinator 返回 503 且无新 job；重复 commit 返回同一 job |
| result 发布 | 只接受完整契约且 job 为 QUEUED/RUNNING；同一事务写 DONE 与完整 JSON payload | GET job/result 只读持久态；终态覆盖和真实 producer 重启探针 |
| 准入计数 | 不维护镜像计数；直接读取 uploads/jobs 行数、声明字节总量、数据库字节数及实际可用空间 | test_admission_refuses_new_upload_without_touching_old_data、超限请求和 HTTP 429 用例 |
| I/O 与关闭 | route 取消不取消受 shield 保护的 I/O future；名额仅在 future done callback 归还；未知 I/O 异常进 supervisor；stop 等 I/O worker 关闭后停 loop | mailbox/取消顺序、cancelled I/O fatal、unknown HTTP/WS fatal、SIGTERM 0 和端口释放用例 |

机械枚举覆盖 6 个 route：create、get upload、PATCH、commit、get job、get result；请求体读取点为 create JSON、PATCH binary、commit 空 body。所有 SQL 写入为 schema 初始化、重启/到期/append/commit/result 的 uploads/jobs/results 更新插入及相应事务；schema 时间列全量为 uploads 的 created_at/updated_at/expires_at，jobs 的 time_start/time_submit/time_complete/created_at/started_at/terminal_at。行计数只出现在容量准入、统计与测试断言，未发现持久 count 镜像。源文件来源是 uploads.source_name 指向的 <upload_id>.bin；result 来源是 jobs 关联的 results.payload。

## 真实验证及边界

- 隔离 Python 3.12 环境，使用项目测试文档中的依赖版本，HTTP config/store/file/supervision/shared-segmenter 五个完整相关模块：62 passed，0 skipped。覆盖 port 0 真实 aiohttp、TCP/SQLite/文件、默认 300 秒配置传递、body idle、两种 offset 崩溃窗口、取消×2序×5、SIGTERM/端口释放。
- 额外执行 pipeline final、真实 fake-worker WebSocket file-result、SDK client：36 passed。其中 SDK 的真实 HTTP 文件上传用例确认原始 binary bytes、offset 与无 coordinator 时 503。
- 无模型加载的消费者对照中，实际调用 WS _validate_segmentation 与 HTTP normalize_options：Qwen GGUF/MLX 合法 74/1 同为接受、越 80 秒预算同为拒绝；SenseVoice/FunASR Nano/Paraformer 的 100/1 均同为接受。十进制、下限和 snap 半开预算也由 HTTP 测试覆盖。
- 真实 publisher 探针由生产 TaskHandler.handle_audio_task → TaskPipeline → Result 产生 15 字段 dataclass（注入无模型 fake engine），经 HttpStore.record_result 持久化；另一个真实 HTTP server 进程 GET 得到逐字段相同结果，原始 26-byte 文件、upload ID、job ID 和 DONE 状态均一致。它证明真实生产流水线与存储消费者契约，不代表真实模型推理。
- 新增测试骨架逐段核过实际内容：两个 offset 标记仍分别为 fsync 前与 offset commit 后；等待各自进程标记、kill、客户端无成功 ACK、线程 join 均保留。SIGTERM helper 使用对应进程对象；端口释放断言接收对应测试自己的端口。cancel-first/io-first 每种 5 次、创建 helper 仍断言 201 且保留响应 body。
- 判据负样本：六条 route 的实际计数为 6；人为把预期改为 7 会判不同；有效但查无内容返回 rg exit 1，非法查询返回 exit 2。官方 goals 索引只读 check 在 H0 退出 0。
- OCR：仅扫本轮 574a..d453 小增量；envelope 为 status=reviewed、coverage=complete、0 findings；内部 verifier 子步骤 skipped 是因为无 finding 可验证，不是 OCR 未扫描。
- CI run 36921941913 的两个单元测试 job 均 SUCCESS，head 为 H0；draft PR38 的 run 36921943099 中 primary/OCR/resolve_advisory 均 SKIPPED，聚合任务名为 gate (draft)。PR 当前确为 draft，所以这不是完整 gate 通过；通知 job 也 skipped。派发时主干基线 API 不可用，继承红无法判定。
- Linux/POSIX 当前环境通过；Windows、macOS、Python 3.9、NTFS ACL、真实模型部署、systemd 消费环境及默认 300 秒墙钟等待均未实测。E3 runner、GC、WAL 空间完整账本仍属后续阶段，本结论不宣称完成。

## 踩坑

首轮误用系统 Python 运行测试，因缺 websockets/aiohttp 导致 1 fail、2 skip；依项目测试说明换到隔离依赖环境后完整相关模块为 62 pass、0 skip。首轮结果不用于代码判定。

## 绕过

未加载模型、未读取生产环境文件；用临时目录、port 0、无模型 fake engine 和真实 TaskHandler/TaskPipeline 取证。未读实现报告、实施推理、预算顾问或主脑停派账本。

## 偏差

唯一 review finding 是校验前幂等键 SELECT 的 P2 顺序偏差。没有改实现或增加测试。CI draft 的 skipped 不是 gate pass；未测平台及模型状态均保持 unknown。

## 最贵一步

为真实 HTTP/SQLite/进程边界安装隔离依赖并跑 62 个生命周期相关用例，再用真实 TaskHandler 产出 payload、重启 HTTP consumer 验证持久结果；这是确认 producer 到最终 reader 没有经过手造近似 payload 的关键步骤。
