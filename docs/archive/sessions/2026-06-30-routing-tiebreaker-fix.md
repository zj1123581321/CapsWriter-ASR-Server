# 交接 Prompt：路由 Tiebreaker 彻底修复

## 问题

负载均衡的路由评分公式反复出 bug，每次修一个又冒出新问题。历史：

1. **v1 零分陷阱** — `active_tasks * latency / weight`，`active_tasks=0` 时 score=0，延迟信号完全失效。Codex 发现。
2. **v2 延迟压制** — 修成 `(active_tasks+1) * latency / weight`，但异构设备的 latency 方差 10-300x，导致所有任务堆积到延迟最低的单台后端，其他设备闲置。3 路并发测试发现。
3. **v3 当前问题** — 改成 least-connections `(active_tasks+1) / weight + latency * 1e-6`，解决了并发场景，但低并发（任务一个一个来）时三台都 `active_tasks=0`，主项 `(0+1)/1=1` 完全相同，决策完全靠 `latency * 1e-6` tiebreaker。而 `avg_latency`（= `time_complete - time_submit`）不代表设备快慢，它取决于音频时长——处理了长音频的快设备反而 latency 更高，tiebreaker 输了，被饿死。

**实测数据（2026-06-30）：**
- Mac Studio（M1 Max）：RTF=0.035（最快），但 avg_latency=132.4s（处理了长音频） → tiebreaker 总输
- Mac mini（M2 Pro）：RTF=0.066，avg_latency=64.4s
- AMD 6800H（Radeon 680M）：RTF=0.111（最慢），avg_latency=51.4s → tiebreaker 总赢
- 结果：Mac Studio 连续 5+ 分钟没接到任务，AMD 反而一直在接

## 根因

**`avg_latency` 根本不适合做路由决策依据。** 它是推理绝对耗时（秒），不是设备速度指标。同一台设备处理 3 秒音频和 30 分钟音频，latency 差 600 倍，但 RTF 可能一样。用它做 tiebreaker 等于随机惩罚/奖励后端。

更深层的问题是：**评分公式承担了两个互相矛盾的职责**：
1. 均衡负载（看 active_tasks）— 这个工作正常
2. 偏好快设备（看 latency）— 这个一直在出 bug

## 要求

**彻底解决，不是打补丁。** 需要从第一性原理重新设计 tiebreaker 策略，确保：

1. **并发场景（多任务同时进来）**：任务均匀分散到所有后端 — 当前 least-connections 主项已解决
2. **低并发场景（任务一个一个来）**：所有后端轮流接任务，不因 latency 偏差而饿死任何一台
3. **异构设备**：快设备自然会更快处理完释放 slot，并发时会自动多接任务，不需要 tiebreaker 额外偏向
4. **不引入新的 footgun**：方案要简单、可预测、不随运行时数据波动

**建议方向（仅供参考，请独立评估）：**
- 去掉 latency tiebreaker，改用 round-robin 或随机打破平局
- 或者 tiebreaker 用 RTF（= latency / audio_duration）而不是 latency 原始值
- 或者 tiebreaker 用 `last_result_time`（最久没出结果的优先）

**必须新开 worktree：**
```bash
git worktree add ../CapsWriter-routing-fix fix/routing-tiebreaker
```

## 关键文件

- `core/proxy/router.py` — `backend_score()`、`_latency_for_score()`、`_peer_median_latency()` 是核心
- `core/proxy/backend.py` — `BackendState` 数据结构、`record_processing_latency()`、EWMA 逻辑
- `tests/test_proxy_router.py` — 路由测试（需要加低并发 tiebreaker 场景的测试）
- `tests/test_proxy_backend.py` — 后端状态测试
- `config_proxy.py` — 后端配置（weight）

## 当前评分公式（要改的）

```python
# router.py:123-125
def backend_score(self, backend: BackendState) -> float:
    latency = self._latency_for_score(backend)
    return (backend.active_tasks + 1) / backend.weight + latency * 1e-6
```

`_latency_for_score()` 返回 `avg_latency`（EWMA），冷启动回退 `_peer_median_latency()`。

## 验收标准

1. **低并发测试**：三台都空闲时，连续 10 个任务不能全压到同一台后端
2. **并发测试**：3 个任务同时进来，应分到 3 个不同后端
3. **异构设备**：不同 weight 的后端，高 weight 后端在并发时获得更多任务
4. **稳定性**：不因运行时 EWMA 数据波动导致路由策略突变
5. 现有测试全部通过：`python -m pytest tests/ -q`
6. CLAUDE.md 中的评分公式文档同步更新

## 环境

- conda 环境 `c`，或 `conda run -n c` 执行
- 临时 Python 代码先写到临时脚本文件再运行，用完不删

## GSTACK REVIEW REPORT

| Review | Trigger | Runs | Status | Findings |
|--------|---------|------|--------|----------|
| Eng Review | `/plan-eng-review` | 4 | CLEAR | 3 issues, 0 critical gaps |
| CEO Review | `/plan-ceo-review` | 2 | CLEAR | mode: HOLD_SCOPE |
| Outside Voice | codex | 5 | ISSUES | 9 findings, 2 accepted |

**CODEX:** 发现 9 个问题，接受 2 个（weight 验证移至 __post_init__、日志文案更新）。其余为已决策项或超出范围。

**CROSS-MODEL:** Claude 和 Codex 在核心方案上一致：移除 latency tiebreaker，改用 round-robin。Codex 额外提出 weighted round-robin 但经评估 weight 不等时偏向高权重是预期行为。

**VERDICT:** ENG CLEARED

Eng Review 关键决策：
- 评分公式简化为 `(active_tasks+1) / weight`，移除 latency 成分
- 平局用 `BackendState._rr_counter` (ClassVar) 做 round-robin 轮转
- weight 验证从 backend_score() clamp 改为 BackendState.__post_init__ 校验
- 删除 `_latency_for_score()` 和 `_peer_median_latency()` 死代码
- 保留 avg_latency/record_processing_latency 用于诊断日志和 /status
- 4 个新测试覆盖 round-robin 行为，删除 6 个废弃 latency 测试

NO UNRESOLVED DECISIONS
