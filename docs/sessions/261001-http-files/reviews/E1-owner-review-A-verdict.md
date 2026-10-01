# E1 owner 与共享 PCM 分段独立审查

审查对象固定为 `9e40df201e9e3cdf4dad18deec7629974b3d6fb3..d090b7d238fe82d8f0303d6e3d304a8f38da1f10`，risk-tier 为 internal。

failure-visibility: p2-only

## 结论

本轮审查完成。结论为通过，发现一项 P2：合法浮点分段参数可使 WebSocket producer 生成非 float32 对齐的 PCM 段，识别任务随后以可见错误失败。按审查纪律，此项不阻塞合并，可登记后续修复。

未发现 owner 三元键串扰、HTTP 结果走 WebSocket 出站、缺 sink 时静默丢结果、或 E1 默认启用 HTTP 业务入口的问题。

## P2 finding：PCM 字节量化破坏 float32 对齐

- **违反约束**：任务卡不变式 3（完整 float32 采样点、长度与 stride 均 4 字节对齐，合法有限参数保持旧采样量化语义）及不变式 4（旧 WebSocket 合法输入不回退）；对应 `design.md`「PCM 分段契约」。
- **位置**：`core/server/segmenter.py:198-212`；真实 producer 调用在 `core/server/connection/ws_recv.py:154-178`；float32 消费在 `core/server/worker/audio.py:25-26`。
- **真实输入与环境**：Python 3.12.3；使用合成的 16 kHz 单声道 PCM，不读真实录音。通过当前 `_validate_segmentation` 校验，并调用真实 `_submit_segments` producer。命令：`PYTHONPATH="$PWD" python3 /tmp/http-e1-owner-review-a-dlg-20261001-031536-b8a5aa-probe.py`。
  - 固定切点模式：`seg_duration=5.00001`、`seg_overlap=0.5` 被接受；输出段为 352001 字节（`% 4 == 1`）。旧实现按采样点量化会输出 352000 字节。
  - 默认切点吸附模式：`seg_duration=5.0`、`seg_overlap=0.50001` 被接受；以既有 CutFinder 合法的 32 ms 帧边界切点 5.12 秒验证 producer，输出 359681 字节（`% 4 == 1`）。该用例仅替换切点 finder 以隔离量化路径，没有加载模型。
  - `process_audio_task` 对上述 producer 输出抛出 `ValueError: buffer size must be a multiple of element size`。`TaskHandler` 会把推理异常转成 `inference_failed` 结果，因此这会失败一个合法请求，但不会产生错误成功文本或崩溃 worker。
- **本仓 P1 两问**：①会触发：服务协议和 SDK 都接受浮点 `seg_duration`/`seg_overlap`，SDK 将传入值原样写入 WebSocket 帧，服务端校验也接受上述数值；探针实跑真实 producer。②后果不可接受为正常功能缺陷，但没有数据损坏、静默错结果、worker/server 崩溃或越权；该任务以可见错误失败，不命中 internal 的 P1 红线，定为 P2。
- **处置**：建议后续将秒数先按旧语义量化为采样点，再乘 float32 stride；切点 stride 与 overlap bytes 都必须按采样点换算。此轮不改被审实现。

## 已审实现与不变式

- `Task`/`Result` 默认 `owner_kind='ws'`；owner ID 从 `socket_id` 或 HTTP `task_id` 派生，统一 key 为 `(owner_kind, derived_owner_id, task_id)`。状态、WS 接收/发送、worker session、调度、超时监控均已按三元 key 走；仓内未发现仍按旧二元任务 key 读取共享状态的其它生产消费者。
- 主进程 `ProcessManager` 创建并持有 Manager，传入 `start_worker`、`RecognizerWorker` 和 `TaskHandler`；worker 的实际 multiprocessing fixture 传递 `Task`/`Result` 对象并断言字段。HTTP owner 缺少活跃集合时 fail-fast；失活 HTTP 任务不会继续推理，WS 仍按 socket 活跃集合清理。
- 父进程结果分发对 HTTP 要求显式 sink；sink 缺失会抛错，不写 WS outbound。HTTP sink 成功返回后才确认段结果并转移终态、释放 owner。
- 已对照旧 WS producer 中 `round(seconds * SAMPLE_RATE) * BYTES_PER_SAMPLE` 的采样点算法，并审阅新实际 producer、CutFinder 调用、最终段与 overlap/offset 路径。除上述 P2 外，没有发现切段路径的新增 owner 或 WS 默认语义回退。
- `design.md`/`qa.md` 保持 E1→E7 七项路线、HTTP 关闭状态与失败/保存承诺；E1 状态为进行中，E2–E7 为未开始。

## 未验证范围与后续接线

- 未运行完整 pytest/CI；本轮只运行上述隔离 producer/consumer 探针，并静态审阅新增跨进程 fixture 与既有 WS 消费者。
- 本机只有 Python 3.12.3，没有更低解释器可做真实消费环境验证；仓库文档建议服务端使用 Python 3.12，但没有声明服务端最低版本。SDK 的 `>=3.10` 只约束 SDK。新增共享模块没有增加第三方依赖；较低服务端运行时兼容性仍未验证。
- E2/E3 的 store 与 HTTP runner 尚未实现，未作为当前缺陷。本轮看到的 HTTP 成功结果路径先等待 sink，再确认段/释放 owner。E3 需另行把 worker 进程失败、超时等 `fail_active_tasks` 路径接到持久终态提交，再释放活跃 owner；当前 E1 没有持久 store，故此为后续接线义务。
- OCR `ocr-review` 已按冻结 SHA 与 2549 字节中性摘要启动；运行 10 分 08 秒仍只有 stderr `OCR failover progress: leg=primary event=start`，stdout 文件为空，没有 status/reason/comments envelope。为避免无限等待，停止挂起进程；OCR 结果状态未能判定，不宣称已审过或 skipped。摘要、stdout 和 stderr 保留于 `/tmp/http-e1-owner-review-a-dlg-20261001-031536-b8a5aa-spec-summary.md`、`/tmp/http-e1-owner-review-a-dlg-20261001-031536-b8a5aa-ocr.stdout.json`、`/tmp/http-e1-owner-review-a-dlg-20261001-031536-b8a5aa-ocr.stderr.txt`。完整独立审查不因 OCR 缩小。
