## 测试

正式测试位于 [`tests/`](../../tests/)。完整验证命令：

```sh
uv run --no-project --python 3.12 --with numpy --with rich --with websockets --with colorama --with pytest --with soundfile --with pytest-asyncio python -m pytest tests/ -q -p no:cacheprovider
```

- **服务端与协议**：测试真实 WebSocket 接收、解码、分段、背压、错误帧和任务终态；`tests/harness/` 提供假引擎及服务端夹具。
- **Proxy**：覆盖后端配置、版本与编码路由、并发任务和连接生命周期。
- **SDK**：`tests/test_sdk_client.py` 与 `tests/test_e2e_sdk_server.py` 覆盖文件客户端 API、编码和端到端调用。
- **无头导入**：`tests/test_server_headless.py` 在禁用 tkinter 的子进程中导入 server、proxy 与 server app。
- **引擎集成**：需真实模型或平台依赖的测试在资源缺失时按用例标记跳过。

## PR 模型门禁

- [.github/workflows/gate.yml](../../.github/workflows/gate.yml) 是 Required Gate v2 的**调用方**：只声明触发事件、权限、`tier=internal` / `runner=self` / `has_ui=false`，以及透传 `SILO_ACCESS_KEY` / `SILO_SECRET_KEY` / `FEISHU_CI_WEBHOOK`。门禁逻辑本身在 `zlxlabs/gate`，改门禁要去 gate 仓。
- 上游单测与 lint 由 [ci.yml](../../.github/workflows/ci.yml) 跑；`gate.yml` 只管模型主审与门禁聚合，不替代单元测试。
- **前提**：本公开仓需自行配置 repository secrets `SILO_ACCESS_KEY` / `SILO_SECRET_KEY`；缺失时 gate-v2 的 S3 步骤 fail-loud（`SILO_ACCESS_KEY 未传入`），不会静默降级。`runner: self` 依赖 org `ci` runner 组（自建机+tailnet 访问 Silo），换成 hosted 会让主审整job skip。

## 真实服务验证脚本

- `scripts/_verify_dictation.py`：连接运行中的服务端，验证音频任务与识别结果。
- `scripts/_verify_file_transcribe.py`：验证文件任务返回真实字级时间戳并生成 SRT。
- `scripts/_baseline_asr.py`：保存服务端转录结果、字符错误率和时间戳基线。
- MLX 设备验证脚本见 `scripts/_verify_mlx_asr.py` 和 `scripts/_smoke_mlx_subprocess.py`。

SDK 安装和 CLI 用法见 [`sdk/README.md`](../../sdk/README.md)。
