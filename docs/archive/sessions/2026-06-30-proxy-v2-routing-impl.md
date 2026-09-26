# 交接 Prompt — ASR 代理 v2 路由改进实现

## 背景

CapsWriter ASR 负载均衡代理 v1 已上线（`core/proxy/`），经过完整 review pipeline（office-hours → CEO → eng → Codex）。本次 CEO review（SELECTIVE EXPANSION）确定了 v2 改进范围，需要在新分支上实现。

**CEO Plan 完整文档：** `~/.gstack/projects/zj1123581321-CapsWriter-Offline-with-AI/ceo-plans/2026-06-30-proxy-concurrent-routing.md`

## 重要：使用 Git Worktree

**所有代码更改必须在新的 git worktree 中执行。** 不要在主工作目录修改代码。

```bash
# 1. 在主 repo 创建新分支
cd /home/zlx/projects/oss/CapsWriter-Offline-with-AI
git branch feat/proxy-v2-routing

# 2. 创建 worktree
git worktree add /tmp/capswriter-proxy-v2 feat/proxy-v2-routing

# 3. 在 worktree 中工作
cd /tmp/capswriter-proxy-v2
```

完成后在 worktree 中提交，回主 repo 合并或 PR。

## 实现任务（按优先级排序）

### T4 (P1) — 后端健康恢复 cooldown
- **文件：** `core/proxy/backend.py`, `config_proxy.py`
- **改动：**
  - `BackendState` 加 `last_failure_time: float = 0.0` 字段
  - `config_proxy.py` 加 `cooldown_seconds: int = 60`
  - `select_backend()` 中检查 unhealthy 后端是否已过 cooldown，过了则同时重置 `consecutive_failures=0` 和 `healthy=True`
  - 全部后端 unhealthy 时，降级选 cooldown 中 `active_tasks` 最少的 + WARNING 日志
- **测试：** 新增测试验证 cooldown 恢复 + 全部 unhealthy 降级

### T5 (P1) — weight > 0 启动校验
- **文件：** `core/proxy/proxy_server.py`
- **改动：** `build_proxy_from_config()` 中校验所有 weight > 0，不合法 raise ValueError
- **测试：** 测试 weight=0 时启动报错

### T1 (P1) — weight 字段
- **文件：** `core/proxy/backend.py`, `config_proxy.py`
- **改动：**
  - `BackendState` 加 `weight: float = 1.0`
  - `config_proxy.py` 的 backends 改为支持 weight 配置（可用 tuple `(url, weight)` 或 dict）
- **测试：** 现有 backend 测试适配

### T2 (P1) — 权重路由
- **文件：** `core/proxy/router.py`
- **改动：** `select_backend()` 评分从 `active_tasks` 改为 `active_tasks / weight`
- **测试：** 验证不同 weight 的后端获得不同的任务分配

### T3 (P1) — processing_latency 路由
- **文件：** `core/proxy/router.py`, `core/proxy/backend.py`
- **改动：**
  - `BackendState` 加 `avg_latency: float = 0.0`（EWMA, alpha=0.2）和 `latency_samples: int = 0`
  - `_backend_to_client` 中解析 RecognitionMessage 的 `time_submit`/`time_complete`，计算 `processing_latency = time_complete - time_submit`
  - 异常值（<0 或 >60s）记 WARNING 并排除
  - 冷启动（前 3 次）不参与路由评分
  - `select_backend()` 最终评分：`active_tasks / weight / latency_factor`（latency_factor 默认 1.0，有数据后用 EWMA）
- **测试：** 冷启动、正常反馈、异常值排除

### T7 (P2) — 可观测性日志
- **文件：** `core/proxy/router.py`, `core/proxy/backend.py`
- **改动：** 确保所有新功能的日志格式统一：
  - 路由日志包含 `score=X`（而不只是 active_tasks）
  - 定期输出各后端 avg_latency
  - cooldown 恢复事件记 INFO
- **测试：** 日志格式检查

### T6 (P1) — 并发测试脚本
- **文件：** `scripts/_verify_proxy_concurrent.py`
- **改动：** 写一个端到端并发测试脚本，验证：
  1. N 个并发任务被路由到不同后端
  2. 结果正确回送到各自的客户端连接
  3. 同一 task_id 所有消息到同一后端
  4. 后端挂掉时行为符合预期
- **注意：** 需要 mock WS 服务器模拟 ASR 后端，或依赖真实环境运行
- **参考：** `scripts/_verify_dictation.py` 的结构

### T8 (P2) — 消费方并发改造指南
- **文件：** `docs/消费方并发改造指南.md`
- **内容：**
  - 当前串行模式的瓶颈分析
  - 并发改造方案（多线程/多进程/asyncio）
  - 示例代码
  - 注意事项（task_id 唯一性、连接管理、错误处理）

### 最后 — 提升 CEO Plan 到 repo
- 将 `~/.gstack/projects/.../ceo-plans/2026-06-30-proxy-concurrent-routing.md` 复制到 `docs/designs/proxy-concurrent-routing.md`

## 关键约束

1. **不改 Server 代码**（`core/server/` 不动）
2. **不改 Client 代码**（`core/client/` 不动）
3. **不改协议**（`core/protocol.py` 不动）
4. **零额外依赖**（只用 websockets + 标准库）
5. **所有现有测试必须继续通过：** `python -m pytest tests/ -q`

## 验证清单

```bash
# 在 worktree 中运行
python -m pytest tests/ -q                                           # 全量测试
python -m pytest tests/test_proxy_backend.py tests/test_proxy_router.py -v  # proxy 测试
scripts/_verify_proxy_concurrent.py                                  # 并发验证（需代理+后端运行）
```

## 部署信息

代理运行在 Mac Studio（pm2 进程 `capswriter-proxy`，port 6020）。
- Mac Studio: 192.168.31.222:6017（M1 Max 64GB, weight=2.0）
- Mac mini: 192.168.31.207:6017（M2 Pro 16GB, weight=1.0）

更新代码后：`cd /Users/zhanglixing/Production/capswriter_proxy && git pull && pm2 restart capswriter-proxy`

## Review 状态

- CEO Review: CLEARED（本次）
- Eng Review: 需要重新跑（scope 扩展后过期）
- 建议实现完成后跑 `/plan-eng-review` 再合并
