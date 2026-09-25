## 测试 (Testing)
- **正式单测**: [`tests/`](tests/) 目录,pytest。`qwen_asr_mlx` 引擎逻辑通过注入假 `mlx_qwen3_asr` 模块测试(见 [`tests/conftest.py`](tests/conftest.py)),**无需真机 MLX**,覆盖能力声明、空音频早退、超长截断、语言映射、dtype 映射、工厂 Args→Config 展开、check_model 旁路。
  - 运行: `python -m pytest tests/ -q`(依赖 `numpy rich websockets colorama pytest`)。
  - **注意**: 根目录 `.gitignore` 用 `test_*.py` 忽略临时脚本,`tests/` 下正式测试靠 `!tests/test_*.py` 例外保留。
- **真机集成验证**: [`scripts/_verify_mlx_asr.py`](scripts/_verify_mlx_asr.py)(走 ModelLoader 全链路)、[`scripts/_smoke_mlx_subprocess.py`](scripts/_smoke_mlx_subprocess.py)(spawn 子进程冒烟),需 Apple Silicon + `mlx-qwen3-asr`,首次联网下载权重。
- **Aligner 集成测试**: [`tests/test_aligner_integration.py`](tests/test_aligner_integration.py) 验证外挂 ForceAligner 真实产出字级时间戳。**资产缺失时自动 skip**(其它机/CI 不受影响),本机装好 llama 后端 dylib + Qwen3-ForcedAligner 模型 + `onnxruntime gguf srt soundfile nagisa soynlp` 后转为真实断言。
- **端到端识别验证(连运行中的 server)**: 两个配对脚本覆盖两条识别路径,默认连 6016、`--server` 指定其它实例(如 6017 MLX)。
  - [`scripts/_verify_dictation.py`](scripts/_verify_dictation.py)：发 `source=mic`(听写路径,纯 ASR)验识别连通+文本+RTF。`--wav` 真实音频或 `--duration` 合成;`--source file` 可切文件路径。`python scripts/_verify_dictation.py --wav <wav> [--server ws://localhost:6017]`,依赖 `numpy websockets`(wav 走标准库 wave)。
  - [`scripts/_verify_file_transcribe.py`](scripts/_verify_file_transcribe.py)：发 `source=file`(文件转录)校验产出真·字级时间戳(单调递增、字间隔非均匀=真实对齐 aligner/原生,非字符均分回退)并落 srt。`python scripts/_verify_file_transcribe.py <wav> [--server ws://localhost:6017]`,依赖 `soundfile numpy websockets`。
  - 注：二者从各生产仓 untracked 的 `tools/test_ws_client.py`(文件名被 `.gitignore` 的 `test_*.py` 规则挡住,无法入库)收编泛化而来,现入主线 `scripts/`(`_` 前缀避开该规则)。
- **独立转录客户端**: [`scripts/transcribe_client.py`](scripts/transcribe_client.py) — 单文件零仓内依赖 CLI，拷到任意设备连局域网/Tailscale ASR 服务端转录本地音频并落 srt/txt/json；依赖 `soundfile numpy websockets`，非 16k mono wav 需系统 `ffmpeg`。

## 服务端端到端测试骨架
- `fake_asr_server` fixture 启动真实 `ws_recv`/`ws_send`、`TaskHandler` 和识别子进程，监听随机端口，不加载模型。
- 假引擎把音频绝对起点和长度编码进文本；`fail_on_call` 指定调用序号抛错，`delay_on_call` 与 `delay_seconds` 指定调用序号及阻塞时长。
- 调用记录包含顺序、`task_id` 和 `socket_id`；可用 `supports_punc`、`supports_timestamps` 调整能力声明。
- 运行：`python -m pytest tests/test_server_e2e_baseline.py -v`。
- `scripts/_baseline_asr.py --server ws://host:port --wav input.wav --out baseline.json [--ref reference.txt]` 保存真实服务端文本、CER 和时间戳；输入 WAV 需为 16 kHz。
