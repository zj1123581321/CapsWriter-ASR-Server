# HTTP 文件任务 QA 验收矩阵

本矩阵是 E1→E7 的验收契约，不是已执行结果。所有隔离测试使用自有 `/tmp`、port 0、测试 PID 和假引擎；不连接生产服务、不读取真实录音。跨进程项必须断言真实 producer 发出的对象或文件字节。E1 只锁定 owner/key、共享活跃集合和 PCM 分段；下列 12 组多数仍待后续卡实测，不能把本增量写成 HTTP 已可用。

1. **真实二进制 producer**  
   待测：SDK/CLI 实际请求体是源文件原字节，不是 JSON、Base64 或完整文件入 worker；服务端文件字节、长度和 SHA-256 一致。  
   当前证明：无 HTTP listener/SDK；未测。

2. **受理后脱离连接**  
   待测：客户端拿到 COMMITTED/QUEUED 确认后退出，另一连接用保存的凭据领取完整文本、token 和 timestamp；HTTP 不依赖 socket。  
   当前证明：无 HTTP 受理路径；未测。

3. **幂等创建与提交**  
   待测：创建或最终确认响应丢失后显式恢复，同一 create key 只得到一个 upload/job，实际识别次数为 1；网络库不得自动重发。  
   当前证明：未测。

4. **可信续传 offset**  
   待测：连接中断后先查询服务端确认位置，producer 只发送确认位置之后的字节；统计实际未确认重发字节，不把重发伪报为零。  
   当前证明：未测。

5. **部分上传跨重启**  
   待测：真实服务进程在写入、offset 提交和 ACK 前后退出，重启后已确认前缀、文件身份和 offset 可核对；未确认尾部不能冒充已确认；`QUEUED`/`RUNNING` 重启后失败，partial 与结果保留。  
   当前证明：未测。

6. **结果跨重启**  
   待测：任务 DONE 后重启服务，新的 HTTP 连接从持久记录领取同一完整结果；源音频过期不影响结果领取。  
   当前证明：未测。E3 需把 worker/timeout 失败接到持久 sink 后再释放 owner。

7. **双 owner 并发**  
   待测：真实 WS producer、HTTP producer、worker Queue 和 Result Queue 同时运行；HTTP 空 socket 可处理，断开的 WS 不可继续处理，两个 key 不互相污染。  
   当前证明：E1 已用真实 multiprocessing Queue/pickle 锁 HTTP 活跃集合门控与 WS 默认 kind；完整双入口并发仍待 E3/E6。

8. **取消与 I/O 交错**  
   待测：在实际写入未结束时取消请求或发起第二次 PATCH，旧 I/O 完成前不释放写保护；两种完成顺序都核对磁盘字节和数据库 offset。  
   当前证明：未测。取消不释放未完 I/O，无自动重跑。

9. **重复 final 与资源边界**  
   待测：两次 final、旧 offset、超前 offset、不同源文件、空文件、块超限、队列/磁盘额度边界都产生明确错误，旧数据不覆写。R7 全单位：1 GiB 文件、1 MiB PATCH、64 KiB read、16 handler、2 同时 body、32 未完成上传、HTTP 运行 1、共享活动总量 8（跨内存 `state.tasks` 与 SQLite `jobs` 表的 `QUEUED`+`RUNNING`，同一 Job 只数一次；其中 2 个名额恒定预留给 WS，HTTP 最多占 6，超限时 HTTP 报 429 `too_many_jobs`、WS 报 `overloaded`；WS 首帧与 HTTP commit 在同一把共享准入锁内完成「计数 → 判定 → 登记」，登记不得在锁外）、每任务 4 段、64 MiB 结果、16 GiB source 预留、2 GiB DB+WAL+SHM、2 GiB 实际剩余。  
   当前证明：未测物理容量；旧整数默认 WS 分段与背压仍由既有测试覆盖。并发准入已由屏障测试证明：8 个并发 commit 在有锁时共享总量停在上限内、无锁时读同一份空快照导致越限。

10. **解码与 PCM producer**  
    待测：真实文件解码器的 argv、输入文件和环境可核对；输出是有界 16 kHz mono f32 PCM 段，不把整文件或路径交给 worker；格式矩阵不能用缺依赖 skip 冒充通过。  
    当前证明：E1 已用真实 `_validate_segmentation` + `_submit_segments` 锁采样点量化。固定切点 `seg_duration=5.00001`、`seg_overlap=0.5` 产出 352000 字节完整 float32；吸附切点 5.12 s 且 `seg_overlap=0.50001` 产出 359680 字节。`process_audio_task` 消费实际 `Task.data`。整数默认、final 剩余段、旧分段契约仍绿。真实容器解码待 E3。

11. **整任务失败与监督**  
    待测：worker 中间段/末段失败、解码失败、结果超限、未知 RuntimeError 和正常 SIGTERM 分别验证；失败任务不发布缺段成功，正常停止为零退出，未知异常非零退出。  
    当前证明：旧 WS 错误契约与看门狗仍由既有测试覆盖；HTTP 监督与持久失败提交待 E3/E6。

12. **源清理与长期兼容**  
    待测：终态 7 天前、活跃引用期间和重启后都不误删；无活跃引用且到期只清源音频，任务记录/结果仍可读。未登记残留不自动删。旧 WS 协议、health、端口、背压、错误和累计结果全量回归。  
    当前证明：E1 续修维持旧整数默认、并发背压、压缩 idle 与 WS 回归；12 组并未全部实测。真实三平台/ASR 字节基线未验证。

每组都要保留 producer fixture、实际 payload、隔离环境和失败原因；单纯在同一进程手造消费侧 dict 不算跨边界证明。对关键断言注入相反实现时必须以 `AssertionError` 失败，ImportError、语法错误或恒真断言不算行为红验。已授权实施，缺 gate 例外不等于生产授权。
