# 背压与上传空闲：跨协程 idle_state_event 竞态加固

> 会话：2026-09-30 排查 VideoTranscriptAPI 生产转录失败（issue #94）后的服务端侧加固
> 状态：待用户点头后拆卡实施

## 背景

生产侧（n305 容器 → Mac Studio 6016）出现长视频转录稳定失败，客户端每 5 次重试都停在
`duration_s=3180.000`。排查结论是**客户端主动关闭**（`capswriter_client` 收发串行 +
`websockets.connect` 用默认 `ping_interval=20/max_queue=16`，上传期间不读服务端结果导致
pong 读不到 → 保活超时 → 客户端以 1011 自杀），已提 VideoTranscriptAPI issue #94 跟踪。

服务端日志全程零报错、`slow_consumer` 计数 0，故**本仓不是本次故障的责任方**。

排查过程中对本仓做了一轮背压/上传空闲机制审计，结论如下。

## 审计结论

### 已排除：裸 PCM 路径缺少背压保护（曾误判）

初读代码时怀疑 `ws_recv.py:569-575` 的裸 PCM f32le 路径只算了一次 `remaining`、
没有压缩路径那样的 `timeout = None if record.backpressured` 判断，因此服务端自身变慢时
会误杀客户端。

**受控实验推翻了该假设**（`/tmp/repro_idle.py`，基于 `tests/test_backpressure.py` 的
`LocalServer` + `BackpressureFakeEngine` 骨架）：

| 场景 | 终态 |
|---|---|
| 背压期间不发帧（control，对应既有测试） | `RECEIVING` |
| 背压期间持续发帧 6 次（treatment，模拟真实上传） | `RECEIVING` |

原因：`_acquire_segment_slot` 的 `finally`（`ws_recv.py:255-258`）对**所有**调用方
统一推迟 `record.idle_deadline`，与调用路径无关；每次成功收帧又会在 `ws_recv.py:665-667`
重置该 deadline。裸 PCM 路径每轮循环都重新计算 `remaining`，保护是生效的。

结论：**不修**。既有测试 `test_backpressure_pauses_upload_idle_timer` 与
`test_compressed_consumer_backpressure_pauses_upload_idle` 已覆盖该语义。

### 确认存在：idle_state_event 的 clear()/set() 跨协程竞态

`_receive_compressed_frame`（`ws_recv.py:485-500`）的接收循环每轮执行：

```python
record.idle_state_event.clear()                                    # :493
timeout = None if record.backpressured else max(0.0, deadline-now)  # :494-496
changed = asyncio.create_task(record.idle_state_event.wait())       # :497
```

而 `set()` 发生在**另一个协程** `_acquire_segment_slot` 内（`:254` 进入背压、
`:260` 退出背压），该协程由 `cache.decoder_task`（`:462-463` 创建的
`_consume_compressed_pcm`）驱动，与接收循环不同协程。

因此存在窗口：接收循环执行完 `clear()`、尚未推进到 `wait()` 时，
`_acquire_segment_slot` 恰好完成一次 `set()`，该标志会被随后的 `wait()` 错过，
`changed` 永不 ready，循环退回 `timeout` 分支 → `TimeoutError` →
`queue_error_and_close(..., 'upload idle timeout')` **误杀正在正常上传的客户端**。

机制层已确证（`/tmp/race_probe3.py`，判据带自检对照）：

```
clear->set->clear 序列:        100/100 次丢失
clear->set (无二次 clear):        0/100 次丢失  <- 判据自检，已知答案应为 0
```

即 `asyncio.Event` 的标志在 `clear()` 后丢失，语义本身无问题，问题在于
`clear()` 与 `wait()` 之间夹着可被其他协程 `set()` 的窗口。

触发条件（三者同时满足）：
1. 客户端走**压缩音频**路径（flac 等，即本仓 SDK `transcribe_file` 的默认上传格式）
2. 该任务触发识别背压（`max_inflight_segments` 默认 4 已满）
3. 背压结束的时刻恰好落在上述窗口内

因窗口极窄，现网未观测到该故障；但它是一颗定时炸弹，且修复成本低。

## 修复方案

**改法**：把「清除旧标志 + 等待」换成不会被同协程外 `set()` 吞掉的顺序——
在 `clear()` 之前先读一次 `backpressured` 作为基线，并让 `wait()` 前的清理由
「取出并清空」语义保证。最小改动是引入一个单调递增的背压版本号，取代布尔 Event
作为唤醒信号。

**不采用的改法**：
- 单纯删掉 `clear()` —— 会导致 `changed` 立即返回，循环空转打满 CPU。
- 给 `clear()` 加锁 —— asyncio 单线程事件循环内无必要，且不解决语义问题。

## 拆卡

本批为单张 M 卡（单文件局部改动 + 回归测试），不触发 DESIGN-note 门禁要求之外的额外流程；
本文档已落盘，随该卡 PR 一并进 git。

### 卡 1：修复 idle_state_event 跨协程竞态并补回归测试

- 改动边界：`core/server/connection/ws_recv.py` + `core/server/state.py`（若引入版本号）
- 完成条件：
  1. 背压唤醒不再依赖「clear 与 wait 之间无 set」的隐式假设
  2. 新增回归测试，构造跨协程 `set()` 落在窗口内的时序，断言不误报 idle timeout
  3. 既有 `tests/test_backpressure.py` 与 `tests/test_protocol_v2.py` 全绿
  4. 新增测试须验证「改坏实现时它会变红」（断言具备约束力）
- 验证入口：`pytest tests/test_backpressure.py tests/test_protocol_v2.py`
