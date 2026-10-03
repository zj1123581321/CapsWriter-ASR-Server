# HTTP 文件任务 QA 验收矩阵

本矩阵是 E1→E7 的验收契约，同时逐组登记**当前实测到哪一步**（事实截至 M6 commit `30f7474`）。所有隔离测试使用自有 `/tmp`、port 0、测试 PID 和假引擎（`tests/harness/fake_engine.py` 的 `ProgrammableFakeEngine`），解码走真 ffmpeg；不连接生产服务、不读取真实录音。跨进程项必须断言真实 producer 发出的对象或文件字节。

**假引擎 + 真子进程/真解码不等于真实 ASR 识别质量**：真实三平台、真实模型、真实录音的字节与质量基线仍未验证（留给 M7），不得据此宣称 HTTP 已可用或质量达标。

**计数口径（修此前的 7/9 混计）**：在 base `5134720` 上，**7 组整体闭合**（2/4/5/6/8/9/12），1 与 10 组**部分**闭合（各留一条缺口），合计 12 − 7 = **5 条真实缺口**（1/3/7/10/11）；此前文档把 1、10 与 7 组整体闭合混在同一句里写成 9 个组号又同时声称「只有 5 条缺口」，前后矛盾，按上面这句为准。M6 在 `tests/test_http_qa_e2e.py` 新增 5 个测试函数（重采样矩阵 2 个参数，合计 6 条用例）逐条补齐这 5 条。逐组证据、文件:行与反向红验记录见 `docs/sessions/261003-http-completion/m6-qa-evidence.md`。

1. **真实二进制 producer**  
   待测：SDK/CLI 实际请求体是源文件原字节，不是 JSON、Base64 或完整文件入 worker；服务端文件字节、长度和 SHA-256 一致。  
   当前证明：**已闭合**。`tests/test_http_qa_e2e.py:378` 用真实 SDK `submit_file_http` 走完 create→PATCH→commit，抓到的 PATCH body 拼接 == 源文件字节，服务端 `data_dir/sources/{upload_id}.bin` 的 SHA-256/长度 == 源文件（`:434/:442/:448`）。既有抓包侧：SDK→裸 TCP 字节流 `tests/test_http_client.py:144`（PATCH body 逐字节 `:220`）、CLI 子进程 `:413`；真服务端落盘 `tests/test_http_file_tasks.py:205`（`:222-223`）。未知：本组「相反实现→红」未单独跑（只实跑了红验 A–D，见 evidence），真实生产服务与真实录音未测。

2. **受理后脱离连接**  
   待测：客户端拿到 COMMITTED/QUEUED 确认后退出，另一连接用保存的凭据领取完整文本、token 和 timestamp；HTTP 不依赖 socket。  
   当前证明：**已闭合**。`tests/test_http_file_runner.py:341`（mp3/aac/m4a/opus 四种真实容器，提交客户端连接已关闭，另一连接拿到的 payload 与识别子进程真实发出的 Result 逐字段相等 `:365-380`）、`tests/test_http_file_tasks.py:451`（重开服务后领取）；`state.tasks == {}`、`active_http_jobs == []` 见 `tests/test_http_file_tasks.py:441-442`。

3. **幂等创建与提交**  
   待测：创建或最终确认响应丢失后显式恢复，同一 create key 只得到一个 upload/job，实际识别次数为 1；网络库不得自动重发。  
   当前证明：**已闭合**。`tests/test_http_qa_e2e.py:495` 丢掉 commit 的真实 202 响应后走显式恢复，断言被丢弃的 202 响应恰好 1 个（`:516`）、整段流程 POST 恰好 2 次且 `/commit` 恰好 1 次（`:520-521`），SQLite 只 1 条 job、识别子进程收到的段数 == 一次解码的段数。既有：`tests/test_http_client.py:476`（post/patch/commit 三处响应丢失的请求序列）、`tests/test_http_file_runner.py:624`（重复 commit 不重投）、`tests/test_http_store.py:211`。反向红验 D 已实跑（自动重发注入 → `assert 2 == 1`）。

4. **可信续传 offset**  
   待测：连接中断后先查询服务端确认位置，producer 只发送确认位置之后的字节；统计实际未确认重发字节，不把重发伪报为零。  
   当前证明：**已闭合**。`tests/test_http_file_tasks.py:272` 用真 SDK 统计实际未确认重发字节 = size−2048（不是伪报 0）且落盘仍等于源文件；`tests/test_http_supervision.py:503` 两个真实崩溃窗口（`:527-530` 重启后 `confirmed_offset=0` 而磁盘上是未确认尾、SHA 等于尾）；`tests/test_http_store.py:161` 未确认尾被截到 DB offset。

5. **部分上传跨重启**  
   待测：真实服务进程在写入、offset 提交和 ACK 前后退出，重启后已确认前缀、文件身份和 offset 可核对；未确认尾部不能冒充已确认；`QUEUED`/`RUNNING` 重启后失败，partial 与结果保留。  
   当前证明：**已闭合**。`tests/test_http_supervision.py:503`（写入前/offset 前/ACK 前 SIGKILL 后新进程核对已确认前缀、文件身份与 offset）、`tests/test_http_file_runner.py:747/784`（SIGTERM 与 SIGKILL 后重启 → `FAILED[server_restarted]`）、`tests/test_http_store.py:249`；重启后 `received` 为空证明不自动重跑（`tests/test_http_file_runner.py:775`）。

6. **结果跨重启**  
   待测：任务 DONE 后重启服务，新的 HTTP 连接从持久记录领取同一完整结果；源音频过期不影响结果领取。  
   当前证明：**已闭合**。`tests/test_http_supervision.py:577`（旧进程写入的真实 producer payload → kill → 新进程 `GET result` 逐字节相同）；`tests/test_http_cleanup.py:276`（源已删后仍 200，`source_available is False` 且 `result_available is True`，`:488-489`）。worker/timeout 失败接持久 sink 后再释放 owner 由组 11 的 `tests/test_http_file_runner.py:817/1192/1320` 覆盖。

7. **双 owner 并发**  
   待测：真实 WS producer、HTTP producer、worker Queue 和 Result Queue 同时运行；HTTP 空 socket 可处理，断开的 WS 不可继续处理，两个 key 不互相污染。  
   当前证明：**已闭合**。`tests/test_http_qa_e2e.py:114` 让真 WS 识别与真 HTTP 识别**同时**经过同一个真 worker Queue/Result Queue：WS 客户端只收到自己 `task_id` 的结果、HTTP 结果只落 `results` 表（两个 key 不互相污染），只连不发帧的空 socket 期间 HTTP 照常 DONE，断开的 WS 任务从 `state.tasks` 消失且引擎不再收到它的段。共享准入侧既有：`tests/test_http_file_tasks.py:1168/1243/1269/1294/1347/1464`。E1 的 multiprocessing Queue/pickle 门控证据仍在其中。反向红验 A 已实跑（不断连 pop → 目标用例超时红）。

8. **取消与 I/O 交错**  
   待测：在实际写入未结束时取消请求或发起第二次 PATCH，旧 I/O 完成前不释放写保护；两种完成顺序都核对磁盘字节和数据库 offset。  
   当前证明：**已闭合**。`tests/test_http_file_tasks.py:619` 对「取消先/IO 先」各跑 5 轮，每轮独立核对 `confirmed_offset` 与磁盘字节；未完 I/O 期间 mailbox 槽位不释放（32→31），随后 GET/PATCH 都拿到可信 offset（`:680-731`）。取消不释放未完 I/O，无自动重跑。

9. **重复 final 与资源边界**  
   待测：两次 final、旧 offset、超前 offset、不同源文件、空文件、块超限、队列/磁盘额度边界都产生明确错误，旧数据不覆写。R7 全单位：1 GiB 文件、1 MiB PATCH、64 KiB read、16 handler、2 同时 body、32 未完成上传、HTTP 运行 1、共享活动总量 8（跨内存 `state.tasks` 与 SQLite `jobs` 表的 `QUEUED`+`RUNNING`，同一 Job 只数一次；其中 2 个名额恒定预留给 WS，HTTP 最多占 6，超限时 HTTP 报 429 `too_many_jobs`、WS 报 `overloaded`；WS 首帧与 HTTP commit 在同一把共享准入锁内完成「计数 → 判定 → 登记」，登记不得在锁外；停机先在锁内排空并换掉计数来源再拆 worker/存储，事后准入显式失败而不静默只数内存；幂等重放不受预算误拒）、每任务 4 段、64 MiB 结果、16 GiB source 预留、2 GiB DB+WAL+SHM、2 GiB 实际剩余。  
   当前证明：未测物理容量；旧整数默认 WS 分段与背压仍由既有测试覆盖。并发准入已由屏障测试证明：8 个并发 commit 在有锁时共享总量停在上限内、无锁时读同一份空快照导致越限（`tests/test_http_file_tasks.py:1427/1464`）。停机与准入并发已测：stop() 必须先排空在途临界区，事后计数来源显式失败（CounterUnavailable）而非退化为只数内存；幂等重放在预算满时仍拿回同一 Job，而新 upload 仍被 429（`:1538/1565`）。负例矩阵 `:504`、身份与上限 `:742`、handler/body 配额 `:568`。未知：1 GiB/16 GiB/2 GiB 等真实物理上限**未按真实体量压测**，只按缩小常量验证类别与等值边界。

10. **解码与 PCM producer**  
    待测：真实文件解码器的 argv、输入文件和环境可核对；输出是有界 16 kHz mono f32 PCM 段，不把整文件或路径交给 worker；格式矩阵不能用缺依赖 skip 冒充通过。  
    当前证明：**已闭合**。`tests/test_http_qa_e2e.py:597`（2 参数）用真 ffmpeg 转码 44.1 kHz 立体声与 8 kHz 单声道源，各段 `samplerate == 16000`、`data_bytes % 4 == 0`、单段样本数有界（`:624-625`），各段样本数之和 == 独立跑一次真 ffmpeg 得到的样本数，送进 worker 的段字节远小于源文件（`:651-652`）——重采样/降混侧不再是未知。argv/env 逐项核对、峰值解码并发 == 1、无临时 PCM 文件见 `tests/test_http_file_runner.py:341`（`:405/:415-426`）。E1 的固定切点/吸附切点采样点量化、`process_audio_task` 消费实际 `Task.data`、整数默认与 final 剩余段契约仍在原有用例中并保持绿。未知：真实三平台容器与真实 ASR 模型的字节/质量基线未验证；本组用的是假引擎，不是识别质量证据。

11. **整任务失败与监督**  
    待测：worker 中间段/末段失败、解码失败、结果超限、未知 RuntimeError 和正常 SIGTERM 分别验证；失败任务不发布缺段成功，正常停止为零退出，未知异常非零退出。  
    当前证明：**已闭合**。`tests/test_http_qa_e2e.py:667` 单独覆盖**末段（is_final）解码失败**：前几段已产出非 final Result，但 Job 必须 `FAILED[inference_failed]`、`results` 表 0 行、`GET result` 409（红验 C 实跑：注入「缺段也发布」→ 在 `status.state == "FAILED"` 断言处红，`assert 'DONE' == 'FAILED'`）。既有失败形态：中间段失败 `tests/test_http_file_runner.py:491`、解码失败 `:521`、结果超限 `:539`、worker 崩溃 `:1127`、段超时 `:817`、未知后台异常主进程非零退出 `:1429`、SIGTERM 零退出 `:747`、R4 释放顺序 `:880/1192/1256/1320`。旧 WS 错误契约与看门狗仍由既有测试覆盖。

12. **源清理与长期兼容**  
    待测：终态 7 天前、活跃引用期间和重启后都不误删；无活跃引用且到期只清源音频，任务记录/结果仍可读。未登记残留不自动删。旧 WS 协议、health、端口、背压、错误和累计结果全量回归。  
    当前证明：**已闭合**。`tests/test_http_cleanup.py:142`（终态边界 + 活跃引用）、`:276`（周期清理等真实 runner 引用）、`:553`（upload I/O 与清理共用一个 worker，5 轮）、`:778`（停机等在途清理）；`tests/test_http_capacity.py:311/912`（终态源释放、重启释放 pending 但保留 partial、跳过已删终态源）。旧 WS 回归由 `test_server_e2e_baseline.py`、`test_backpressure.py`、`test_health.py`、`test_error_codes_contract.py`、`test_protocol_v2.py`、`test_segmentation_contract.py` 承担。未知：真实三平台部署与真实 ASR 字节/质量基线仍未验证（本矩阵 12 组现在都有真实 producer 断言，但这不等于真实平台已验收）。

每组都要保留 producer fixture、实际 payload、隔离环境和失败原因；单纯在同一进程手造消费侧 dict 不算跨边界证明。对关键断言注入相反实现时必须以 `AssertionError` 失败，ImportError、语法错误或恒真断言不算行为红验。已授权实施，缺 gate 例外不等于生产授权。
