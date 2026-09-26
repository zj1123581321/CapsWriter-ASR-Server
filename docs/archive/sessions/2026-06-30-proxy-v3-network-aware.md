# 交接 Prompt：ASR 代理网络感知路由 v3

## 任务

实现 ASR 代理网络感知路由 v3。已通过 CEO review + Eng review + 4 轮 Codex outside voice 对抗审查，方案已锁定。

**请先创建新的 git worktree 和分支来执行所有更改，不要在 master 上直接改动：**

```bash
git worktree add ../CapsWriter-proxy-v3 -b feat/proxy-v3-network-aware
cd ../CapsWriter-proxy-v3
```

## 背景

ASR 负载均衡代理（`core/proxy/`）的评分公式 `active_tasks * avg_latency / weight` 有一个数学 bug：当 `active_tasks=0` 时评分为零，延迟信号完全失效，空闲的远程后端总会被选中。此外需要为远程后端（Tailscale 3Mbps）添加诊断数据采集，用于后续路由优化。

原始方案（端到端延迟替代推理延迟做路由）被 Codex 否决——流式音频时长污染 EWMA 样本，不同时长任务产生不可比较的延迟。调整为：修 bug + weight 杠杆 + 诊断先行。

## 实现任务（~50 LOC，按顺序执行）

### T1 (P1): 修复评分公式零分陷阱

`core/proxy/router.py:backend_score`（当前第 119 行附近）

```python
# 现有:
return backend.active_tasks * latency / backend.weight
# 改为:
return (backend.active_tasks + 1) * latency / backend.weight
```

### T2 (P1): 端到端延迟诊断日志

`core/proxy/router.py`:

1. `TaskSession` dataclass 加字段 `start_time: float = 0.0`
2. `_open_task_session` 方法入口（`connect_backend` 之前）设 `start_time = time.time()`
3. `_backend_to_client` 方法中，在 `is_final` 分支（`close_session` 之前）计算并记录：
   ```python
   end_to_end = time.time() - session.start_time
   logger.info(
       "任务完成: task_id=%s backend=%s inference_latency=%.3f end_to_end=%.3f active_tasks=%s",
       task_id, session.backend.id, <inference_latency>, end_to_end, session.backend.active_tasks
   )
   ```
4. 推理延迟仍从 `_record_backend_latency`（解析 RecognitionMessage）获取，喂 EWMA 用于路由。端到端延迟仅日志，不喂 EWMA。
5. 注意：端到端日志只在 `recognition_task_id(raw_message) == task_id and is_final` 时记录，不要对每条消息都记录。

### T3 (P2): 延迟上限 60→300

`core/proxy/backend.py:record_processing_latency`（当前第 52 行附近）

```python
# 现有:
if not math.isfinite(latency) or latency < 0 or latency > 60:
# 改为:
if not math.isfinite(latency) or latency < 0 or latency > 300:
```

### T4 (P2): config_proxy 示例更新

`config_proxy.py` — 默认 backends 注释中加远程后端低 weight 示例：

```python
backends = _env_backends('CW_PROXY_BACKENDS', [
    ("ws://127.0.0.1:6016", 1.0),
    ("ws://127.0.0.1:6017", 1.0),
    # ("ws://remote:6016", 0.3),    # Tailscale 3Mbps — 降权
])
```

### T5 (P1): 测试更新

`tests/test_proxy_router.py`:
- 新测试：`active_tasks=0` 时 `backend_score` 不为零（验证 +1 修复）
- 新测试：高 latency 低 weight 后端在空闲时仍不被优先选中（对比 LAN 后端）

`tests/test_proxy_backend.py`:
- 更新上限测试：边界值 299（接受）、300（接受）、301（拒绝）
- 现有测试中引用 `> 60` 的断言改为 `> 300`

### T6 (P2): TODOS.md 更新

在 `## ASR 负载均衡代理` 节下添加：

```markdown
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
```

## 验证

```bash
python -m pytest tests/test_proxy_backend.py tests/test_proxy_router.py tests/test_proxy_integration.py -q
```

全部通过后，提交并创建 PR merge 回 master。

## 关键约束

- 不改 `_record_backend_latency` 的签名和 EWMA 喂入逻辑（推理延迟仍用于路由）
- 端到端延迟仅作诊断日志，不喂 EWMA、不影响路由决策
- 不实现 source-aware 路由（代理仅用于批量转录，听写直连 Server 不走代理）
- 不实现 max_concurrent_tasks（defer，先观察 weight 效果）
