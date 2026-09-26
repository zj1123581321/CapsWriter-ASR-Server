# 交接 Prompt：CapsWriter-Offline 可观测性改进 — 实现任务

## 前置要求

**必须新开一个 git worktree 来执行所有更改，不要在主工作目录上直接改。** 步骤：
```bash
git worktree add ../CapsWriter-observability feat/proxy-status
cd ../CapsWriter-observability
```
所有代码修改、测试、提交都在这个 worktree 里完成。完成后合并回 master。

---

## 背景

CapsWriter-Offline 是离线语音识别工具，C/S 架构 + WebSocket 负载均衡代理。3 台后端（Mac Studio 6017、Mac mini 6017、AMD 6800H Windows 6016），Proxy 在 Mac Studio 端口 6020。

当前问题：Proxy 进程内存里有完整的后端状态信息（健康、任务数、延迟、权重），但无法从外部查看，只能 SSH 翻日志。

## 确定的方案（经 eng review + codex outside voice 评审通过）

### T1 (P1) — Proxy `/status` HTTP 端点

在 Proxy 的 WebSocket 同端口 6020 上，通过 `websockets` v16 的 `process_request` 回调拦截 HTTP 请求：

- `GET /status` → JSON 响应
- `GET /status?html` 或浏览器 `Accept: text/html` → HTML 格式化状态页
- 零新依赖，零新端口

**实现要点：**
- `process_request` 加到 `ProxyServer.serve()` 的 `websockets.serve()` 调用里
- 返回类型是 `websockets.http11.Response`（v16 API，不是旧版 tuple）
- 读取 `self.backends`（`BackendState` 对象列表）的原始字段
- **不显示计算后的 score**，只显示原始字段：`active_tasks`, `healthy`, `avg_latency`, `weight`, `consecutive_failures`, `last_failure_time`, `latency_samples`
- 非 `/status` 的 HTTP 请求返回 `None`，让 websockets 继续正常 WebSocket 握手

**JSON 响应结构参考：**
```json
{
  "backends": [
    {
      "id": "backend-0",
      "url": "ws://mac-studio:6017",
      "healthy": true,
      "active_tasks": 2,
      "avg_latency": 1.23,
      "latency_samples": 15,
      "weight": 1.0,
      "consecutive_failures": 0,
      "last_failure_time": 0.0
    }
  ],
  "active_tasks_total": 3,
  "task_history": {
    "total": 49,
    "completed": 47,
    "failed": 2,
    "recent": [...]
  }
}
```

### T2 (P1) — 任务完成历史 deque

- `collections.deque(maxlen=1000)` 存最近完成和失败的任务记录
- **deque 必须放在 `ProxyServer` 层**（全局唯一），通过参数传给每个 per-client 的 `TaskRouter`
  - 重要：`handle_client()` 为每个客户端连接创建新的 `TaskRouter` 实例（proxy_server.py:60），所以 `TaskRouter` 是 per-client 的，不能把全局状态放里面
- 每条记录包含：`task_id`, `backend_id`, `status`（completed/failed/cancelled）, `duration`, `timestamp`
- 任务正常完成时记录 completed；后端连接失败、客户端中断等情况记录 failed

### T4 (P2) — 日志加日期 + RotatingFileHandler

文件：`core/logger.py`

1. `datefmt` 从 `'%H:%M:%S'` 改为 `'%Y-%m-%d %H:%M:%S'`（第 95 行）
2. `TruncatingFileHandler` 替换为标准 `RotatingFileHandler(backupCount=5)`（第 97-100 行）
3. **删除 `TruncatingFileHandler` 类定义**（第 11-34 行），它变成死代码了

### T5 (P2) — 加 2 个集成测试

文件：`tests/test_proxy_integration.py`

复用现有集成测试模式（起真实 WebSocket 服务）：

1. **test_status_endpoint_returns_backend_info** — 启动 ProxyServer，用 `urllib.request` 或 `http.client` 发 `GET /status`，验证 JSON 包含正确的后端数量、字段完整性
2. **test_task_completion_recorded_in_history** — 完成一个任务后，再查 `/status`，验证 `task_history.total >= 1`

注意：HTTP 测试不能用 `websockets.connect()`，需要用标准库的 HTTP 客户端。

## NOT in scope（明确不做）

- 健康端点侵入 Server/Client 组件
- print→logger 改造（子进程 logging 复杂，独立 PR）
- 结构化日志 / 指标导出
- 主动后端健康探测（已在 TODOS.md）
- 认证（家用 LAN）
- 显示计算后的路由 score（评分公式刚改成 least-connections，score 值不直观）

## 关键代码文件

- `core/proxy/proxy_server.py` — 加 `process_request` + status 处理（主要改动）
- `core/proxy/router.py` — `TaskRouter` 加 deque 参数，任务完成/失败时追加记录
- `core/proxy/backend.py` — `BackendState`（只读，不改）
- `core/logger.py` — datefmt + Handler 类型 + 删除 TruncatingFileHandler
- `tests/test_proxy_integration.py` — 新增 2 个测试
- `config_proxy.py` — 参考后端配置结构

## 当前评分公式（最新）

```python
# router.py:125 — least-connections 策略
def backend_score(self, backend):
    latency = self._latency_for_score(backend)
    return (backend.active_tasks + 1) / backend.weight + latency * 1e-6
```

延迟是 1e-6 级 tiebreaker，路由主要看 `(active_tasks + 1) / weight`。

## 验收标准

1. `curl http://localhost:6020/status` 返回有效 JSON，包含所有后端状态
2. 浏览器打开 `http://localhost:6020/status` 显示格式化 HTML 页面
3. 完成一个任务后，`/status` 的 `task_history` 有记录
4. WebSocket 正常功能不受影响（现有测试全通过）
5. 日志文件首行包含 `2026-` 日期前缀
6. `python -m pytest tests/ -q` 全部通过（包括新测试）

## 运行环境

- conda 环境 `c`，或 `conda run -n c` 执行
- 临时 Python 代码先写到临时脚本文件再运行，用完不删
- 测试：`python -m pytest tests/ -q`（依赖 numpy rich websockets colorama pytest）
