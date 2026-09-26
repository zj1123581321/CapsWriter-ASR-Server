# Session 交接：MLX 版 Qwen3-ASR 并入主线

> 生成于 2026-06-25。用于新开 session 接手 MLX 移植工作。

任务：把 Apple MLX 版 Qwen3-ASR 推理能力作为新引擎并入主线 CapsWriter 项目。

## 背景
本机（Mac Studio）有两个 CapsWriter 体系的生产 server，PM2 管理：
- `capswriter-server`（`~/Production/capswriter_server_main`）：已切到主线 master，跑 paraformer，端口 6016。已完成，勿动。
- `qwen-asr-server`（`~/Production/qwen_asr_server`）：MLX 版 Qwen3-ASR，核心代码在 `tools/mlx_ws_server/`。它 remote 挂原作者 HaujetZhao 仓库、git 历史被 squash，与主线无共同祖先；PM2 里 restart 次数极高（~900+），有稳定性问题。

目标：把它的 MLX 推理移植成主线的一个引擎，将来让这台机器也能用主线统一架构跑 Qwen3-ASR（替代独立的 mlx_ws_server）。

## 关键路径
- 主线项目（开发仓库）：`/Users/zhanglixing/Dev/projects/CapsWriter-Offline-with-AI`（分支 master）
- 主线引擎体系：`core/server/engines/`，最贴近的模板是 `qwen_asr_gguf`（GGUF/llama.cpp 版 Qwen3-ASR）
- MLX 推理源：`~/Production/qwen_asr_server/tools/mlx_ws_server/`（`server.py` 等）
- **必读调研报告**：`docs/lixing/mlx-qwen-integration-plan.md`（上个 session 用子 Agent 生成，含文件级实施方案和骨架代码）

## 调研核心结论（详见报告）
- 工作量小：约 0.5~1 人天，核心代码 <150 行，两边接口天然契合。
- MLX 侧：推理已封装成干净的 `ASREngine` 类，底层依赖 PyPI 包 `mlx-qwen3-asr`（`from mlx_qwen3_asr import Session`）。接口极简：`Session(model="Qwen/Qwen3-ASR-0.6B", dtype=...)` 加载、`session.transcribe(audio_np, ...)` 推理；输入 float32/16kHz 单声道 numpy（与主线一致），输出含文本+标点+段级时间戳，未用热词。RTF≈0.27，内存~3.4GB，仅 Apple Silicon。
- 主线契约：引擎继承 `BaseASREngine`，实现 `capabilities` / `create_stream` / `decode_stream` / `cleanup`；标点缺失由 `ModelLoader` 自动外挂 CT-Transformer，时间戳缺失自动外挂 `ManagedAlignerProxy`。工厂用 `(EngineClass, ConfigClass, ArgsObj)` 三元组注册。
- 方案：新增 `core/server/engines/qwen_asr_mlx/asr_engine.py`（适配层，委托 `Session`，声明能力 `[ASR, PUNC]`，时间戳交外挂 Aligner，对齐 GGUF 版），在 `factory.py` 注册 + `_ASR_LOADERS` 映射，在 `config_server.py` 加 `QwenASRMLXArgs` 并把 `'qwen_asr_mlx'` 列入 `model_type`。

## 关键约束
- 平台限定：`mlx` 仅 Apple Silicon，必须**延迟导入**（import 放在引擎方法内，不在模块顶层），并用独立的 macOS requirements，绝不进 Win/Linux 默认依赖。
- 离线理念：MLX 模型默认走 HuggingFace repo id 自动下载，与项目「全本地离线」理念有出入，应支持指定本地模型目录。
- 时间戳：MLX 输出的是段级时间戳，与主线字级 token 契约不符，初版不声明 `TIMESTAMPS`（交外挂 Aligner）。

## 建议第一步
先读 `docs/lixing/mlx-qwen-integration-plan.md` 全文，再把 `core/server/engines/qwen_asr_gguf/` 逐文件读透作为模板，然后实现适配层。开发在主线仓库，验证可仿照上个 session 的离线脚本（直接 `ModelLoader` 加载引擎 + 喂一段音频打印识别结果，不走 WebSocket）。

环境：`conda activate c` 或 `~/Production/qwen_asr_server/venv`（含 `mlx-qwen3-asr`）；临时 Python 代码先写脚本文件再跑，不直接命令行执行，用完保留。

## 参考：上个 session 的相关产物
- 主线版 capswriter-server 迁移已完成（独立 venv、paraformer + 标点、端口 6016）。
- 离线识别验证脚本范例：`~/Production/capswriter_server_main/scripts/_verify_asr.py`（直接调 `ModelLoader` + 引擎识别音频）。
