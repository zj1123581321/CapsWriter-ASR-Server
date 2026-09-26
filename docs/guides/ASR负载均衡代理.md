# ASR 负载均衡代理

当有多台机器都在运行 CapsWriter Server 时，可以启动 ASR WebSocket 代理，把下游客户端连接到代理端口，由代理按任务把音频分发到不同后端。

## 适用场景

- 批量文件转录，需要同时利用多台 Mac / PC 的 ASR 算力。
- 下游客户端已经按 `AudioMessage` / `RecognitionMessage` 协议接入，只想通过改连接地址扩容。
- 后端 Server 地址固定，暂不需要运行中动态增删机器。

## 启动步骤

1. 在每台后端机器启动普通 CapsWriter Server，例如监听 `6016`：

```bash
python start_server.py
```

2. 在代理所在机器编辑根目录 `config_proxy.py`：

```python
class ProxyConfig:
    listen_addr = "0.0.0.0"
    listen_port = 6020

    backends = [
        ("ws://mac-studio.local:6016", 2.0),
        ("ws://mac-mini.local:6016", 1.0),
        ("ws://remote-tailnet.local:6016", 0.3),
        {"url": "ws://pc.local:6016", "weight": 1.0},
    ]

    max_connect_failures = 3
    probe_interval = 30.0
    log_level = "DEBUG"
```

`backends` 仍兼容纯字符串地址，默认 `weight=1.0`。通过环境变量 `CW_PROXY_BACKENDS` 配置时，可用 `url|weight` 格式，例如 `ws://mac-studio.local:6016|2.0,ws://mac-mini.local:6016|1.0`。低带宽远程后端建议先降权，例如 Tailscale 3Mbps 链路可从 `0.3` 起步观察。

3. 启动代理：

```bash
python start_proxy.py
```

4. 下游客户端把连接地址从单台 Server 改成代理地址：

```python
URI = "ws://<proxy-host>:6020"
```

代理协议完全兼容现有服务端。客户端仍然发送 `AudioMessage`，接收 `RecognitionMessage`，不需要额外握手或代理专用字段。

## 路由规则

- 每个新的 `task_id` 会创建一条独立的后端 WebSocket 连接；同一任务的后续音频帧继续走该连接。
- 不同任务按 `(active_tasks + 1) / weight` 评分选择后端；活动任务较少或配置权重较高的后端优先。评分相同时，代理按每个后端最近被选中的顺序轮转。
- `weight` 必须大于 `0`，用于表达后端相对算力。`avg_latency` 是处理延迟 EWMA，保留作诊断日志和状态显示，不参与当前路由评分；端到端延迟也只供诊断。
- 首帧指定模型或编码时，只会选择健康且满足协议版本、模型或编码要求的后端。
- 收到后端最终识别结果（`is_final=true`）后，代理关闭该任务的后端连接并释放负载计数。

之所以每个任务独立连接，是因为 Server 端音频缓存绑定在每条 WebSocket 连接上，不按 `task_id` 隔离。代理不会复用后端连接。

## 协议 v2 与 /health

- 代理启动时并每隔 `probe_interval` 秒探测所有后端的 `/health`，默认 30 秒，单次 HTTP 请求最多等待 5 秒。
- HTTP 200 且返回含整数 `protocol_version` 的 JSON 时按该版本记录；旧服务端的 426、无效响应按 v1 记录。HTTP 503 标记为不健康；HTTP 探测连接失败时再检查 WebSocket，连通则按 v1 记录。
- 首帧带 `encoding` 的任务只分配给健康、协议版本至少为 2 且支持该编码的后端；没有候选时代理发 `no_backend` 错误并以 close code 4000 关闭连接。不带该字段的任务仍可走健康的 v1/v2 后端。
- `GET /health` 返回代理协议版本、Git SHA、健康 v2 后端支持编码的并集及后端版本信息；没有健康 v2 后端时返回 HTTP 503。`/status` 的 JSON 和 HTML 也显示每个后端的协议版本与 Git SHA。
- 每个任务的上行队列最多缓存 8 帧，队列满后代理暂停读取客户端。下行直接 `await client_ws.send`；协议 v2 每条连接只有一个活动任务，因此发送等待会把背压传回后端，不需要额外的 256 帧下行队列。

## 故障处理

- 代理不使用 ping/pong 健康检查，避免后端推理阻塞事件循环时误判。
- 新任务连接后端失败会累加 `consecutive_failures`。
- 连续失败达到 `max_connect_failures` 后，该后端标记为 unhealthy，后续新任务不会再分配给它。
- 后端健康状态由后台定期探测更新；探测恢复成功后重新参与路由，不会只因冷却时间经过就自动恢复。
- 没有可用健康后端时，新任务无法分配，会向客户端报告失败。
- 任务进行中后端断开时，代理关闭客户端连接；v1 不缓存音频，需要下游重新发起该任务。

## 状态查看

代理在同一个监听端口提供只读状态页，不需要额外端口或依赖：

```bash
curl http://localhost:6020/status
```

`GET /status` 默认返回 JSON，包含每个后端的原始状态字段：`active_tasks`、`healthy`、`avg_latency`、`weight`、`consecutive_failures`、`last_failure_time`、`latency_samples`，以及最近 1000 条任务完成历史的统计和最近记录。状态接口不展示路由计算后的 `score`。

浏览器访问 `http://localhost:6020/status`，或请求 `GET /status?html`，会返回格式化 HTML 页面，便于直接查看后端健康、负载和近期任务完成情况。

## 日志

代理复用项目的日志系统，日志文件为：

```text
logs/proxy_latest.log
```

日志会记录新任务路由到哪个后端、后端连接失败次数、任务完成时的推理延迟和端到端延迟、任务释放后的活跃任务数、路由 `score`、后端 `avg_latency`、异常 latency 和 cooldown 恢复事件。

日志时间戳包含日期和时间，例如 `2026-06-30 12:34:56.789`。日志文件按大小轮转，保留 5 份备份。

## 验证

单元与轻量集成测试：

```bash
python -m pytest tests/test_proxy_*.py tests/test_logger.py -q
```

真实端到端验证时，先启动至少一台后端 Server 和代理，再让验证脚本连接代理：

```bash
python scripts/_verify_dictation.py --server ws://localhost:6020
```

并发路由回归验证可直接运行内置 mock 后端脚本：

```bash
python scripts/_verify_proxy_concurrent.py
```
