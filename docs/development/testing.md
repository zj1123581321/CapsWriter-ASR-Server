## 测试

正式测试位于 [`tests/`](../../tests/)。完整验证命令：

```sh
uv run --no-project --python 3.12 --with numpy --with rich --with websockets --with colorama --with pytest==9.1.1 --with soundfile --with pytest-asyncio==1.4.0 --with aiohttp==3.14.3 --with httpx==0.28.1 python -m pytest tests/ -q -p no:cacheprovider
```

- **服务端与协议**：测试真实 WebSocket 接收、解码、分段、背压、错误帧和任务终态；`tests/harness/` 提供假引擎及服务端夹具。
- **Proxy**：覆盖后端配置、版本与编码路由、并发任务和连接生命周期。
- **SDK**：`tests/test_sdk_client.py` 与 `tests/test_e2e_sdk_server.py` 覆盖文件客户端 API、编码和端到端调用。
- **无头导入**：`tests/test_server_headless.py` 在禁用 tkinter 的子进程中导入 server、proxy 与 server app。
- **引擎集成**：需真实模型或平台依赖的测试在资源缺失时按用例标记跳过。

## 真实服务验证脚本

- `scripts/_verify_dictation.py`：连接运行中的服务端，验证音频任务与识别结果。
- `scripts/_verify_file_transcribe.py`：验证文件任务返回真实字级时间戳并生成 SRT。
- `scripts/_baseline_asr.py`：保存服务端转录结果、字符错误率和时间戳基线。
- MLX 设备验证脚本见 `scripts/_verify_mlx_asr.py` 和 `scripts/_smoke_mlx_subprocess.py`。

SDK 安装和 CLI 用法见 [`sdk/README.md`](../../sdk/README.md)。

## HTTP 文件任务测试

- `tests/test_http_store.py`：真实 SQLite 与真实文件字节，锁定「字节 → fsync → offset 事务 → ACK」、未确认尾按数据库 offset 截断、真实 SQLite busy 不 ACK、OS 独占锁的第二进程竞争、重启收敛与坏 schema fail fast。
- `tests/test_http_file_tasks.py`：在 port 0 上起真实 aiohttp listener，消费者是已合入的 SDK（`sdk/capswriter_asr`），断言真实请求体落盘字节、恢复文件内容与库内 offset 一致；覆盖六个 route、幂等创建、断点续传、限额 429、I/O mailbox 32 名额，以及真实 server handler 被取消后两种 I/O 完成顺序各五次。
- `tests/test_http_config.py`：在无 PI/DELEGATE 身份的裸环境子进程里真实解析 `CW_HTTP_PORT`/`CW_HTTP_DATA_DIR`。
- `tests/test_http_supervision.py`：真实子进程的启用可见性与监督（默认关闭、坏数据目录/端口冲突/同目录第二实例非零退出、未知 HTTP/I/O 与 WS RuntimeError 非零退出、SIGTERM 零退出并释放端口）；另覆盖真实进程崩溃窗口重开上传、offset commit 前后恢复，以及结果 producer 输出经新进程 HTTP 读回。

完整验证命令显式安装 `aiohttp==3.14.3` 与 `httpx==0.28.1`，与 CI 消费依赖一致，因此 HTTP listener 测试不能以缺依赖 skip 冒充完整通过。HTTP 默认关闭的旧服务启动不要求安装 aiohttp；若在旧环境只验证 WS，应将其单独标为子集验证。
