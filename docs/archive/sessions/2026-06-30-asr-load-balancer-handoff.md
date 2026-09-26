# 交接 Prompt — CapsWriter ASR 负载均衡代理实现

请在新的 git worktree 中执行所有代码更改。先创建 feature 分支和 worktree：

```
git worktree add ../CapsWriter-proxy-worktree -b feat/asr-load-balancer
```

然后 `cd ../CapsWriter-proxy-worktree` 在 worktree 中工作。所有文件创建、编辑、测试都在 worktree 里完成。完成后我会在主仓库 merge。

---

## 任务

为 CapsWriter-Offline 实现 ASR 负载均衡 WebSocket 代理。设计文档已经过 office-hours + CEO review + eng review（含 Codex cross-model 验证），所有架构决策已锁定。

**设计文档路径**：`~/.gstack/projects/zj1123581321-CapsWriter-Offline-with-AI/zlx-master-design-20260629-234147.md`
实现前必须先读这个文件，它是唯一的真相源。

---

## 核心架构决策（已锁定，不要重新讨论）

1. **Per-task 独立后端 WS 连接**：Server 的 `AudioCache`（`ws_recv.py:27`）是 per-websocket 的，不按 task_id 隔离。每个新 task_id 必须建一条独立的后端 WS 连接，任务完成后关闭。不能用连接池/复用连接。

2. **Least-loaded 路由**：纯按 `active_tasks` 计数选后端，v1 不用 RTF 加权。

3. **健康检查用连接失败计数，不用 ping/pong**：Server 推理时可能阻塞 event loop，ping/pong 会误杀忙碌后端。连续 3 次 connect 失败标记 unhealthy。

4. **max_size=None**：与 Server 保持一致（`server_manager.py:69`）。

5. **日志复用 core/logger.py**：INFO+DEBUG 分级，写入 `logs/proxy_latest.log`。

---

## 要创建的文件

```
config_proxy.py                # 配置文件（根目录）
start_proxy.py                 # 启动入口
core/proxy/__init__.py
core/proxy/proxy_server.py     # 主服务：WS 监听 + 客户端连接处理
core/proxy/router.py           # TaskRouter: per-task 路由 + 后端连接管理
core/proxy/backend.py          # BackendState: 后端状态跟踪
```

不修改任何现有文件。

---

## 关键数据流

```
Client (1 persistent WS)           Proxy                    Backends
─────────────────────────          ──────                   ────────
task1 AudioMessage ──────────────► [解析 task_id, 新 task]
                                   [least-loaded → Server A]
                                   ──── new WS ──────────► Server A
task1 AudioMessage ──────────────► ──── same WS ─────────► Server A
task1 is_final ──────────────────► ──── same WS ─────────► Server A
                                   ◄─── RecognitionMessage  Server A
◄─────── RecognitionMessage ────── ──── close WS ────────  (释放)

task2 AudioMessage ──────────────► [解析 task_id, 新 task]
                                   [least-loaded → Server B]
                                   ──── new WS ──────────► Server B (不同后端!)
```

---

## 协议参考

- `core/protocol.py`：`AudioMessage`（client→server）和 `RecognitionMessage`（server→client）
- AudioMessage 关键字段：`task_id`, `source`, `data`(base64), `is_final`, `time_start`, `seg_duration`, `seg_overlap`
- RecognitionMessage 关键字段：`task_id`, `is_final`, `text`, `text_accu`, `tokens`, `timestamps`
- WS 连接参数：`max_size=None`, `ping_interval=None`（与 Server 一致）

---

## asyncio 并发模型

- 每个客户端连接 1 个主 asyncio task：读客户端消息 → 按 task_id 路由
- 每个活跃 task 2 个 asyncio task：
  - `client_to_backend`: 同 task_id 的消息转发到后端
  - `backend_to_client`: 后端结果转发回客户端
- 连接关闭清理：后端断开 → cancel task pair → 更新 active_tasks

---

## 测试要求

单元测试 + 集成测试全覆盖：

**单元测试**（`tests/test_proxy_*.py`）：
- BackendState: active_tasks 增减、consecutive_failures 计数、healthy 状态转换
- TaskRouter: least-loaded 选择、task_id 亲和性、tie-break、all unhealthy 拒绝
- 消息解析: AudioMessage task_id 提取、RecognitionMessage is_final 检测

**集成测试**：
- 用 `scripts/_verify_dictation.py --server ws://localhost:6020` 通过代理验证端到端

---

## 验收标准

1. 下游客户端只改 addr:port 就能接入代理
2. 同一 task_id 的所有消息到同一个后端
3. 不同 task 可以路由到不同后端（核心价值）
4. 后端挂了能被检测到，新任务不发到已挂后端
5. 日志清晰显示路由决策和后端状态
6. `python -m pytest tests/test_proxy_*.py -q` 全部通过

---

## 已知限制（v1 不做）

- 不缓存音频数据（后端中途故障时文件转录需用户重新发起）
- 路由仅按 active_tasks 计数（不考虑设备算力差异）
- 不支持动态添加/移除后端（需改配置重启）
