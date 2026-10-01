# E1 owner 修复增量审与旧 WebSocket 消费者反向终审

审查风险：internal
全量冻结对象：`9e40df201e9e3cdf4dad18deec7629974b3d6fb3..cfc32fc5a815bca17998a48d0f55acfecc81217f`
专项增量：`d090b7d238fe82d8f0303d6e3d304a8f38da1f10..cfc32fc5a815bca17998a48d0f55acfecc81217f`
failure-visibility: p2-only

## 结论

未发现 P1。记录两项 P2：共享分段器在 WebSocket 背压生效前批量持有整帧切段结果；HTTP 缺少活跃集合或结果 sink 时的 fail-fast 负例没有回归断言。当前 HTTP listener、store、runner 尚不存在，未据此报告当前 HTTP 业务故障，也未宣称完整 HTTP 功能通过。

## H0..H1 四问

1. **是否只修已登记 finding：是。** 专项范围只把 `_cut` 的 stride/overlap 字节计算恢复为先 `round(seconds * SAMPLE_RATE)` 再乘 `BYTES_PER_SAMPLE`，并补浮点生产者、消费者和 QA 记录；对应前轮登记的 float32 样本量化问题。
2. **是否新增未经批准抽象：否。** 增量没有新增 helper、状态或依赖；`PcmSegmenter` 属全量对象既有结构，且是 E1 已批准的共享分段契约。
3. **状态、事实源、fallback 是否无依据增加：否。** 增量没有新增状态或 fallback/retry；四舍五入仍是唯一采样点量化来源。
4. **是否留下双路径：否。** 固定切点和吸附切点都调用同一 `_cut` 算法，不存在按小数或最终段另走字节算法。

## 全量反向审查与 findings

### P2-1：背压名额申请前批量物化整帧切段

- **违反不变式：** 任务卡第 4 条及 `design.md` PCM/背压契约：WebSocket 每任务最多 4 段在途，背压期间不能让合法输入绕开有界 PCM 与实际资源约束。
- **位置：** `core/server/segmenter.py:84-104,117-133` 先把全部 ready 段累积到列表；`core/server/connection/ws_recv.py:162-178` 收齐列表后才逐段调用申请 semaphore 的提交函数。
- **实测输入与环境：** Python 3.12.3；使用当前真实 `ws_recv._submit_segments`、`PcmSegmenter._cut`，单任务 semaphore 设为 1，以 asyncio Event 观察第二次申请名额的时点。输入为服务端允许的最大解码 PCM 帧 67,108,864 字节、合法 `seg_duration=5.0`、`seg_overlap=2.499`、固定切点。保留探针：`/tmp/http-e1-review-b-dlg-20261001-042135-0e8205-backpressure_probe.py`。输出：第二次申请前已切 208 段，段 payload 合计 99,826,688 字节（约 95.2 MiB），队列只有 1 个 Task，缓冲剩 548,864 字节。说明等待名额前已有远多于 4 段的 PCM 副本驻留。
- **消费边界校准：** 当前 SDK 源码 `_RAW_FRAME_SECONDS=60`、压缩 chunk 为 256 KiB，明显小于服务器 64 MiB 解码帧上限；普通 SDK 输入不会触发最大放大。但服务端协议实际接受不超过 64 MiB 的自定义 WS 帧，探针使用的是合法上限，不是任意无界输入。最大任务数为 8，不能据此推断 OOM。
- **本仓 P1 两问：** ①真实入口可触发：服务端 WS 接受这类帧，探针实走当前 producer。②后果是否不可接受：未观测到结果错误、静默丢失或进程崩溃；单帧切段副本仍有 64 MiB 输入上限，当前官方 SDK 负载远低于此值，且没有真实部署并发/RSS 数据证明会耗尽资源。故定 P2，建议让切段与逐段提交交错，使申请名额约束生成的在途段数量。

### P2-2：HTTP 缺失共享状态与 sink 的 fail-fast 负例未锁定

- **违反不变式：** 任务卡第 1、2 条及 `design.md` R4：共享 HTTP 活跃集合不可得、HTTP 结果没有注入 sink 时必须明确失败，不能成功返回或静默丢结果。
- **位置与测试：** 当前 guard 在 `core/server/state.py:214-217,232-240`、`core/server/worker/task_handler.py:165-174`、`core/server/connection/ws_send.py:153-165`。`tests/test_owner_ipc.py` 覆盖未知 owner kind、HTTP 不活跃任务丢弃及 sink 注入后的成功路径；没有覆盖活跃 HTTP 结果遇到 `http_result_sink=None`，也没有覆盖 HTTP owner 遇到 `active_http_jobs=None`。
- **环境与实际触发：** 这是可用当前对象直接构造的 fail-fast seam；但本冻结对象没有 HTTP listener/runner，当前正常服务没有 HTTP producer 能触发它。静态路径明确抛 `RuntimeError`，没有看到把无 sink 结果当成功的分支。
- **本仓 P1 两问：** ①当前真实服务入口不能触发缺失 HTTP wiring 的业务路径，HTTP 入口留给后续里程碑。②现有代码在缺状态或 sink 时 fail-fast，未发现错误成功或数据静默丢失。按测试覆盖缺口记 P2；建议为两种缺失依赖各加一条负例，保持返回码/任务释放行为可核对。

## 真实消费者、IPC 与兼容性证据

- owner/key：`Task`、`Result` 默认 `ws`；HTTP owner 由空 `socket_id` 与 `task_id` 派生；状态、会话、超时、清理和结果派发使用三元 key。未知 owner kind 与 HTTP 不活跃队列项不会转成 WS。
- 必要调用链：`ProcessManager.start` 创建并传递 manager list；`worker/__init__.py:start_worker` 传给 `RecognizerWorker`；后者注入 `TaskHandler`；`TaskHandler`/`TaskPipeline` 用 owner key 建会话；Result 保留 owner；父进程 sink 成功后才 ack/完成。`ProcessManager.stop` 调用 Manager 的 `shutdown()`。没有 HTTP listener/store/runner，不把后续 E3 监督与持久化说成当前已完成。
- float32：新增回归经真实 `_validate_segmentation`、`_submit_segments` 得到实际 `Task.data`，核对完整字节、offset、overlap、固定/吸附切点及 final 剩余段，再由 `process_audio_task` 的 `np.frombuffer` 消费。IPC 文件另以真实 multiprocessing Queue/pickle 核对 Task/Result 的 owner、socket、任务 id、final 与字节 payload；两组 fixture 各自覆盖 producer/consumer 和跨进程序列化。
- WS 清理/错误：`ws_send` 只对 WS 使用 socket 出站队列；迟到 HTTP result 不会伪装成 WS；WS disconnect 清理限定 `owner_kind='ws'`。`audio_decoder`、idle、slow consumer、断开等待名额、timeout/error 路径均有现有回归。
- 文档：完整检查 R1–R7、QA 与 E1–E7 milestone。设计/QA 明确 E1 只交付底座、12 组中多数待后续测试；E1 仍为进行中，E2–E7 未开始，没有把路线图写成完整 HTTP 通过或生产可用。

## 实测命令

- `python3 -m pytest -q tests/test_shared_segmenter.py tests/test_segmentation_contract.py tests/test_segmenter.py tests/test_owner_ipc.py`：37 passed，1 skipped，6 warnings；skip 对应 `test_segmenter.py` 中 Silero-VAD 模型或 onnxruntime 不可用的 `skipif`，非 HTTP 验收通过。
- `for run_index in 1 2 3 4 5; do python3 -m pytest -q tests/test_backpressure.py; done`：五轮各 7 passed（每轮约 26.4 秒），每轮有 multiprocessing fork DeprecationWarning；无失败。
- `python3 -m pytest -q -rs tests/test_e2e_sdk_server.py tests/test_server_e2e_baseline.py tests/test_server_headless.py tests/test_port_restart.py tests/test_health.py`：23 passed，18 warnings。
- `python3 -m pytest -q -rs tests/test_audio_decoder.py tests/test_error_contract.py tests/test_error_codes_contract.py`：27 passed，31 warnings。
- `git diff --check 9e40df201e9e3cdf4dad18deec7629974b3d6fb3..cfc32fc5a815bca17998a48d0f55acfecc81217f` 报告 QA 新增行尾空白；这些是 Markdown 两空格硬换行，不是运行测试失败，审查未改动禁改文件。

## 未验证与不作出的结论

- 本机运行时为 Python 3.12.3；服务端文档推荐 3.12，但没有找到服务端最低 Python 版本声明。SDK `>=3.10` 只约束 SDK。本轮没有 Python 3.10 等真实消费环境可作 base/H1 对照，因此旧服务端运行时兼容性 unknown，不能宣称已通过或发生回退。
- 测试使用假识别引擎；未加载生产模型，模型推理结果/性能/质量 unknown。没有连接生产服务、读取真实录音或运行完整 CI。
- OCR：按任务卡所述，上轮 wrapper 已运行 600 秒仍无 envelope 后停止；本轮不重复无界等待。OCR 状态仍为 unknown，未标为 reviewed 或 skipped。独立源码与消费者审查已完成，不能将其描述为 OCR 完整覆盖。
- 主干基线 `gh api request failed`，同作业同首个失败步骤是否为继承红无法判定。
