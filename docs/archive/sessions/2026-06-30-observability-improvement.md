## 任务：CapsWriter-Offline 可观测性改进

### 背景

CapsWriter-Offline 是一个离线语音识别工具，C/S 架构 + 负载均衡代理，当前部署了 3 台后端：
- Mac Studio (M1 Max) — MLX Qwen3-ASR，端口 6017
- Mac mini (M2 Pro) — MLX Qwen3-ASR，端口 6017
- AMD 6800H Windows (Radeon 680M) — GGUF Qwen3-ASR，DML+Vulkan，端口 6016
- Proxy 在 Mac Studio 上，端口 6020，pm2 管理

### 可观测性审计结论

整体及格但有短板。已做好的：task_id 端到端贯穿、Proxy 路由决策日志详尽、Server 管线各阶段有日志、TruncatingFileHandler 10MB 轮转。

**需要改进的缺口（按优先级排列）：**

1. **日志无日期（高）** — `core/logger.py` 的日志格式只有 `%H:%M:%S.ms`，跨日排查无法区分日期
2. **无健康端点（高）** — 三个组件都没有 `/health` 或 `/status` HTTP 接口，外部监控无法探测服务存活
3. **print vs logger 混用（中）** — 引擎子进程（如 `core/server/engines/qwen_asr_gguf/inference/asr.py` 第 208-216 行）性能统计用 `print()` 不入日志文件
4. **无历史日志保留（中）** — `TruncatingFileHandler` 截断即丢失，无备份文件，关键错误可能被覆盖
5. **result_processor 无日志（中）** — `core/client/output/result_processor.py` 几乎没有日志，Client 后处理链路是黑盒
6. **无结构化日志（低，当前规模不需要）** — 纯文本，无法被 ELK/Loki 解析
7. **无指标导出（低，当前规模不需要）** — RTF/延迟有计算但无法聚合

### 关键文件

- 日志基础设施：`core/logger.py`
- Proxy：`core/proxy/router.py`、`core/proxy/proxy_server.py`、`core/proxy/backend.py`
- Server：`core/server/app.py`、`core/server/worker/pipeline.py`、`core/server/connection/`
- Client：`core/client/app.py`、`core/client/output/result_processor.py`
- 协议：`core/protocol.py`（AudioMessage / RecognitionMessage，含 task_id）
- 配置：`config_server.py`、`config_client.py`、`config_proxy.py`

### 要求

请先做整体规划，确认改进范围和优先级后再动手。当前规模是 3 台家庭设备 + 个人使用，不需要过度工程化（如 Prometheus/ELK），优先解决实际排查痛点。

---

### Eng Review 结论 (2026-06-30)

**核心需求重定义：** 从7个日志杂活缩聚为"我想随时看到集群状态"。原方案的缺口列表是技术视角的改进清单，但用户真正需要的是一个状态全景图——每台 Server 是否存活、Proxy 在分发什么、最近处理了多少任务。

**确定方案（5 个 Task）：**

1. **T1 (P1)** Proxy `/status` HTTP 端点 — 在 WebSocket 同端口 6020 上，通过 `websockets` v16 的 `process_request` 回调拦截 HTTP 请求。`GET /status` 返回 JSON，`GET /status?html` 返回格式化网页。零新依赖，零新端口。
2. **T2 (P1)** 任务完成历史 `collections.deque(maxlen=1000)` — 记录成功和失败任务。deque 放在 `ProxyServer` 层（全局唯一），通过参数传给 per-client 的 `TaskRouter`。
3. **T3 (P2)** 抽出 `backend_score` 为独立函数 — 评分逻辑从 `TaskRouter` 方法抽到 `BackendState` 方法或模块级函数，`ProxyServer` 和 `TaskRouter` 都能调用。
4. **T4 (P2)** 日志加日期 + `RotatingFileHandler` — `datefmt` 改为 `%Y-%m-%d %H:%M:%S`；`TruncatingFileHandler` 替换为标准 `RotatingFileHandler(backupCount=5)`，删除旧类。
5. **T5 (P2)** 加 2 个集成测试 — `/status` JSON 正确性 + 任务完成历史计数。复用 `test_proxy_integration.py` 模式，HTTP 请求用 `urllib.request`。

**NOT in scope（明确排除）：**
- 健康端点侵入 Server/Client 组件（过度工程化）
- print→logger 改造（子进程 logging 复杂，独立 PR）
- 结构化日志 / 指标导出（当前规模不需要）
- result_processor 日志（调研发现已有 ~20 处 logger 调用，充分覆盖）
- 主动后端健康探测（已加入 TODOS.md）
- 认证（家用 LAN）

**Codex 外部评审吸收的 2 个发现：**
- D7: deque 必须记录失败任务，否则状态页只显示成功掩盖故障
- D8: `backend_score()` 需抽到 ProxyServer 可访问的位置

**数据流：**
```
Browser/curl ──GET /status──> Proxy:6020
                                │
                    process_request 拦截
                                │
                    ┌───────────┴───────────┐
                    │   读取 self.backends   │
                    │   (BackendState 列表)  │
                    │   读取 self.task_history│
                    │   (deque)              │
                    └───────────┬───────────┘
                                │
                    JSON/HTML Response
                                │
                    ┌─────────────────────────────────┐
                    │ {                                │
                    │   "backends": [                  │
                    │     {"id":"backend-0",           │
                    │      "url":"ws://mac-studio:6017"│
                    │      "healthy":true,             │
                    │      "active_tasks":2,           │
                    │      "avg_latency":1.23,         │
                    │      "weight":1.0,               │
                    │      "score":3.69}               │
                    │   ],                             │
                    │   "active_tasks_total":3,        │
                    │   "task_history": {              │
                    │     "completed":47,              │
                    │     "failed":2,                  │
                    │     "avg_duration":1.5           │
                    │   }                              │
                    │ }                                │
                    └─────────────────────────────────┘
```
