# 交接 Prompt — ASR 代理并发路由与动态调度

## 背景

CapsWriter ASR 负载均衡代理已实现并部署（`core/proxy/`），当前在 Mac Studio 上通过 pm2 运行，负载均衡两个 Qwen3-ASR (MLX) 后端：
- Mac Studio: 192.168.31.222:6017
- Mac mini: 192.168.31.207:6017

代理的核心设计：per-task 独立后端 WS 连接，least-loaded（active_tasks 计数）路由。

## 当前问题

下游消费方是串行模式（发一个任务 → 等完成 → 发下一个），导致所有任务都路由到 backend-0，backend-1 从未被使用。日志佐证见 `logs/proxy_latest.log`，所有 task 都是 `backend=backend-0`。

已确认：代理本身支持并发——如果同时收到 3 个任务、只有 2 个后端，会分 2 个到不同后端，第 3 个等第一个释放后执行。问题完全在消费方的串行模式。

## 本次 Session 目标

1. **验证代理并发能力是否到位**：写一个测试脚本，模拟 N 个并发任务同时通过代理，验证：
   - 任务被正确分发到不同后端
   - 结果正确回送到各自的客户端连接
   - 后端挂掉时的行为符合预期
   - 同一 task_id 的所有消息确实到同一后端

2. **评估动态路由策略**：当前路由仅按 `active_tasks` 计数，存在不足：
   - 两台设备算力不同（M1 Max 64GB vs M2 Pro 16GB），相同计数下应优先发算力强的
   - 一个 2 小时文件和一个 5 秒文件都算 1 个 task
   - 不考虑网络延迟差异

   需要讨论和评估：
   - 是否引入 RTF（Real-Time Factor）加权路由？后端完成任务后的 RTF 已在 `BackendState.last_result_time` 跟踪
   - 是否引入设备算力权重（`config_proxy.py` 中配置）？
   - 是否考虑网络延迟（connect 耗时）作为路由因子？
   - 这些改进值不值得做，还是 active_tasks 计数对实际场景已经够用？

3. **评估消费方改造建议**：代理层能为消费方并发做什么准备？比如：
   - 是否需要背压（backpressure）信号？当所有后端都忙时告诉消费方"等一下"
   - 是否需要队列深度限制？

## 关键文件

- 代理代码：`core/proxy/{proxy_server.py, router.py, backend.py}`
- 协议：`core/protocol.py`（AudioMessage / RecognitionMessage）
- 设计文档：`~/.gstack/projects/zj1123581321-CapsWriter-Offline-with-AI/zlx-master-design-20260629-234147.md`
- TODOS：`TODOS.md` 中 "ASR 负载均衡代理" 章节已有 3 个延期项

## 约束

- Server 的 `AudioCache`（`ws_recv.py:27`）是 per-websocket 的，不能多 task 复用一条后端连接
- 不改 Server 代码
- 不用 ping/pong 做健康检查（推理阻塞 event loop）

## 部署信息

代理运行在 Mac Studio（pm2 进程 `capswriter-proxy`，port 6020）。通过 `run_proxy.sh` wrapper 注入 `CW_PROXY_BACKENDS` 环境变量。更新代码后 `cd /Users/zhanglixing/Production/capswriter_proxy && git pull && pm2 restart capswriter-proxy`。
