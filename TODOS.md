# TODOS

延期工作清单。每项含背景与起点,便于将来接手。

## MLX Qwen3-ASR 引擎(qwen_asr_mlx)

### [ ] MLX 原生段级时间戳减载 Aligner
- **What:** 调研 `mlx-qwen3-asr` 的段级时间戳能否满足字幕精度;若可,让 `qwen_asr_mlx` 声明 `TIMESTAMPS` 能力,省掉外挂 `ManagedAlignerProxy`。
- **Why:** 初版文件转录靠外挂 Aligner 补字级时间戳,多一次模型加载/对齐开销;MLX `Session.transcribe(return_timestamps=True, return_chunks=True)` 本身能返回 `result.segments`(段级 `text`/`start`)。
- **现状/起点:** 初版决策为 `[ASR, PUNC]` + 外挂 Aligner(与 `qwen_asr_gguf` 一致),因为段级 ≠ 字级、直接暴露会破坏 `text_accu`(字级 token 去重)。改造前需先确认 MLX 段级时间戳能否细化到字级或可接受降级。
- **Depends on:** qwen_asr_mlx 引擎已上线并稳定。

### [ ] MLX 听写续写 context 支持(上游已确认支持)
- **What:** 在 `decode_stream` 里把 `task.context` 通过 `transcribe(audio, context=context, ...)` 传入,与 `qwen_asr_gguf` 的听写续写行为对齐。
- **Why:** GGUF 版在 `decode_stream` 里用 `context` 构造 prompt(`core/server/engines/qwen_asr_gguf/asr_engine.py:67-73`)做分段续写;MLX 初版丢弃了 `context`,相对 GGUF 是行为回归。
- **现状/起点:** **已真机核实 `mlx-qwen3-asr==0.3.5` 的 `Session.transcribe` 签名含 `context: str = ''` 参数,接线只需一行**(`if context: t_kwargs["context"] = context`)。初版按评审决策仍不接(与 paraformer/sensevoice 同级,客户端音素 RAG 热词兜底),留作 v2 增量。需补一条 context 透传的单测。
- **Depends on:** 无(上游已支持);仅需评审放行扩大初版范围。

## ASR 负载均衡代理 (core/proxy/)

### [ ] 端到端延迟用于路由
- **What:** 将端到端延迟（代理发送→收到结果）替代推理延迟用于 EWMA 路由评分。
- **Why:** 端到端延迟自然包含网络传输开销，让路由自动感知远程后端的真实代价。
- **现状/起点:** v3 已收集端到端延迟诊断日志。问题：流式音频发送时长包含在端到端延迟中，不同时长任务产生不可比较的 EWMA 样本。需要从"最后一条音频发出"开始计时，或按音频时长归一化。
- **Depends on:** v3 诊断日志数据验证端到端延迟的可用性。

### [ ] 远程后端并发上限 (max_concurrent_tasks)
- **What:** 给每个后端加 `max_concurrent_tasks` 配置，超限时跳过。
- **Why:** 低带宽远程后端即使 weight 降低，LAN 后端全忙时仍可能被过量分配，导致 3Mbps 链路饱和。硬性并发上限是直接防护。
- **现状/起点:** 评分公式已修复零分陷阱，weight 是当前主要杠杆。如果观察到 weight 不足以防止远程后端过载，再实现此项。
- **Depends on:** v3 部署后观察实际路由行为。

### [x] 设备算力权重路由 ✅ 2026-06-30
- **已完成:** 权重路由+unhealthy cooldown 恢复+v3 网络感知诊断。commit c3d8386, 700e9b5, 4be000f。
- 评分公式: `(active_tasks + 1) / weight`，平局时 round-robin 轮转（`BackendState._rr_counter` ClassVar）
- latency 已从评分公式中移除（avg_latency 是绝对处理耗时，不是设备速度指标，用它做 tiebreaker 会饿死处理长音频的快设备）。EWMA 仍保留用于诊断日志和 /status 端点
- config_proxy.py 支持 `(url, weight)` tuple 和环境变量 `url|weight` 格式
- unhealthy 后端 cooldown 60s 后自动恢复；全部 unhealthy 时降级路由

### [ ] 后端中途故障音频重发
- **What:** 代理缓存当前任务的音频数据,后端断开时自动重发到其他后端,而非让客户端重新发起。
- **Why:** v1 不缓存音频,后端中途挂掉时文件转录需用户重新拖入文件。对 1 万文件批量场景,手动重试不可接受。
- **现状/起点:** `_client_to_backend` 直接转发不缓存。需在 `TaskSession` 中维护 `sent_messages: list` 缓存已发消息,故障时重放到新后端。内存开销约 5MB/分钟音频,需评估。
- **复杂度注意(Codex review 2026-06-30):** 重发比预期复杂——需给后端发新 task_id(避免 Server 端 AudioCache 冲突),收到结果后将 task_id 重写回原始值再转发客户端;部分结果已发送时需处理去重;缓存内存需设上限(建议 100MB/task)并定义降级行为。建议在权重路由和 RTF 路由落地后再实现。
- **Depends on:** 权重路由 + RTF 路由先落地。

### [ ] 空闲 healthy 后端的主动探活
- **What:** 对仍显示 `healthy=True`、但一段时间没有接到任务的后端，也做轻量 connect+close 探活，而不是只探已经 unhealthy 的后端。
- **Why:** 空闲时段若某台后端悄悄挂了，要等到下一次任务路由到它才会变红，`/status` 会先显示一段时间假绿。
- **现状/起点:** `d088b8f` 已实现 unhealthy 后台探活（轻量 WS connect+close，指数退避 60s→120s→300s 封顶），路由失败连续 3 次才标 unhealthy。缺口只剩：healthy 且无流量的后端不会被探。仍不要用 ping/pong（推理时会堵 event loop）。
- **Depends on:** 现有 `_health_probe_loop` 与 `/status`。

### [ ] 动态后端添加/移除
- **What:** 支持运行时通过 API 或信号添加/移除后端,无需重启代理。
- **Why:** v1 需要改 `config_proxy.py` 后重启。设备上下线频繁时不方便。
- **现状/起点:** `ProxyServer.backends` 是启动时固定的列表。可加 UDP 控制接口(类似客户端的 `udp_control`)或 HTTP API。
- **Depends on:** 代理已上线并稳定运行。

## 运维告警

### [ ] 告警接入全机通道（D11）
- **What:** proxy 轮询各后端 /health，异常时走全机告警通道。
- **Why:** 服务端 fail-fast 退出后只有守护进程重启，无人知晓。
- **现状/起点:** 带静默期告警三件套（持续故障周期重发、发送路径失败即报错不吞、自检 selftest），/health 契约见 docs/reference/protocol.md §5。
- **Depends on:** 服务端 /health（T6）与 proxy /health（T13）合并。

## GGUF 尾段复读幻觉防护

### [ ] 检测并处置 GGUF 复读幻觉
- **现场证据:** 2026-09-26 Windows GGUF 实测，一段约 56 秒的尾段出现一次复读幻觉，单段产出 519 字；对齐后的时间戳延伸到音频总长之外（297 秒，对应音频 231 秒）。相同输入连续再跑两次均未复现，旧版本处理同一输入也正常。
- **待决问题:** 检测阈值（每秒字数）是多少；检测命中后返回 `inference_failed` 还是采取其它处置；时间戳超过段尾是否应作为断言失败。
- **范围:** 本卡只记录问题，不实现防护。
