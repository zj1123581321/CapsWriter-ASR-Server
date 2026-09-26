# Session 交接：MLX 加速纳入统一版本管理 + 生产迁移到主线

> 生成于 2026-06-26。用于新开 session 讨论并实施：把 MLX 引擎的部署纳入统一版本管理，
> 让主线一份代码同时支持 Win/Linux/Mac，并把生产 `qwen-asr-server` 迁到主线。

## ✅ 已完成（2026-06-26，经 /plan-eng-review 评审后实施）

**决定**：(a) 环境变量覆盖；(b) 原地 `checkout -B`。

**Phase 1（主线）**：`config_server.py` 的 `model_type`/`port`/`addr` 改 `_env_str` 覆盖
（`CW_MODEL_TYPE`/`CW_PORT`/`CW_ADDR`），更新注释清单 + `check_model.py` 报错文案。
pytest 23 通过 + env 覆盖冒烟验证。commit `25f0bd2` 已 push `origin/master`。

**Phase 2（生产迁移）**：`~/Production/qwen_asr_server` 已迁到 `fork/master`（HEAD 25f0bd2，
回滚锚点分支 `backup-pre-mlx-migrate@0f1250a`）。ecosystem env 写
`CW_MODEL_TYPE=qwen_asr_mlx`/`CW_PORT=6017`/`CW_ALIGNER_LLM_USE_GPU=1`/`TMPDIR=/tmp`。
`pm2 start ecosystem.config.cjs --update-env` + `pm2 save`。听写端到端验证通过
（引擎=MLX、6017 监听、5s/60s 中文识别准、RTF 0.137 稳态）。

**评审救命的坑（交接文档原假设错误）**：生产仓是旧 upstream 快照，把 `*.dylib`
（llama/bin + qwen_asr_gguf/inference/bin）和 `ecosystem.config.cjs` **force-add 成 tracked**，
主线 gitignore 不入库它们 → `checkout` 会删。迁移前已备份、迁移后 `cp -a` 恢复，Metal 加速保住。
（venv/models 是 ignored，本就安全。）

**遗留**：aligner 字级时间戳（文件转录）路径未单独端到端触发——`test_ws_client` 走听写不触发
aligner；配置就位 + 启动已挂载代理。如需确认字幕，起 client 拖文件转录跑一次。

下面是实施前的原始交接内容（保留供追溯）。

---

## 本次要解决的问题（一句话）

`qwen_asr_mlx`（Apple MLX/Metal 版 Qwen3-ASR）引擎**代码已经在主线 master 里**，但生产那台
`qwen-asr-server`（端口 6017）跑的是**旧 upstream 快照 + 手工移植**的代码，没纳入统一版本管理。
目标：让生产从主线 master 跑起来，部署差异（端口/引擎/GPU 开关）只靠环境变量区分，**一份代码三平台 + 多部署**。

`qwen-asr-server` **可随时停机**（用户已授权停机迁移）。

## 关键前提：MLX 引擎已在主线，且跨平台安全（务必先认清这点）

不要重复移植——`qwen_asr_mlx` 已 commit + push 到 fork 的 `origin/master`：
- 引擎：`core/server/engines/qwen_asr_mlx/asr_engine.py`，能力 `[ASR, PUNC]`，委托第三方包
  `mlx-qwen3-asr`（`Session` API），**方法内延迟导入 `mlx_qwen3_asr`**。
- 跨平台安全：`mlx`/`mlx_qwen3_asr` 只有在 `model_type=qwen_asr_mlx` 且真去实例化时才 import；
  Win/Linux 完全不碰。`mlx`/`mlx-qwen3-asr` 只进 `requirements-server-macos.txt`，不进 Win/Linux 依赖。
- factory（`_load_qwen_asr_mlx`）、`config_server.QwenASRMLXArgs`、`check_model.py` 的 `qwen_asr_mlx`
  分支（repo-id 旁路）都已在 master。
- 配套：`requirements-server-macos.txt`（pin `mlx-qwen3-asr==0.3.5`，保留 sherpa-onnx）、
  单测 `tests/`、验证/基准脚本 `scripts/_verify_mlx_asr.py` `_smoke_mlx_subprocess.py`
  `_bench_mlx_rtf.py` `_mem_mlx.py`、`CLAUDE.md` 已记 macOS aligner 启用 + RTF/内存实测。

**所以"合并进主线、同时支持三平台"在代码层面已完成。** 缺的只是「部署纳入统一管理」。

## 阻碍：部署差异目前要改代码

master 的 `config_server.py` 里 `model_type` / `port` 是**硬编码**（`'qwen_asr'` / `'6016'`）。
两个生产实例需要不同值：

| PM2 部署 | 目录 | model_type | port | 备注 |
|---|---|---|---|---|
| capswriter-server | `~/Production/capswriter_server_main` | paraformer | 6016 | **别的项目，勿动** |
| qwen-asr-server | `~/Production/qwen_asr_server` | qwen_asr_mlx | 6017 | 本次迁移目标 |

靠各自改 `config_server.py` 会代码漂移，违背"统一版本管理"。

## 推荐方案（待用户最终确认）

1. **主线改一点**：把 `model_type` / `port` 改成环境变量可覆盖
   （`CW_MODEL_TYPE` / `CW_PORT`，用 `config_server.py` 已有的 `_env_str`）。
   aligner 的 Metal 开关 `CW_ALIGNER_LLM_USE_GPU` 主线本就支持。改动跨平台无害。commit + push。
2. **生产目录变成 master 的干净 checkout**：把 `qwen_asr_server` 的 git 迁到 fork 并 reset 到 master，
   **保留其 venv / models / llama dylib**（见下方"生产现状"，这些是大文件不入库）。
3. **部署差异只写在各自 `ecosystem.config.cjs` 的 `env`**：qwen-asr-server 设
   `CW_MODEL_TYPE=qwen_asr_mlx`、`CW_PORT=6017`、`CW_ALIGNER_LLM_USE_GPU=1`、`TMPDIR=/tmp`。

迁到 master 后，**之前手工移植的 2 个生产提交（b31dfb4 引擎、0f1250a aligner/内存）大部分变冗余**——
因为 master 已自带 qwen_asr_mlx 全套；只剩"加 env 覆盖"这一处主线改动是新的。

## 两个待用户拍板的决定（上个 session 卡在这）

- **(a) 配置走环境变量**（`CW_MODEL_TYPE`/`CW_PORT`，推荐）**还是**各部署各自改 `config_server.py`？
- **(b) 生产目录**：原地 rebase 到 fork/master，**还是**全新 clone 一份干净的、把 `models/`+`venv/` 软链过去？

## 生产现状（`~/Production/qwen_asr_server`，迁移时要保住的东西）

- PM2 名 `qwen-asr-server`，端口 6017，`ecosystem.config.cjs`（`script=start_server.py`,
  `interpreter=venv/bin/python`, `max_memory_restart=12G`, `TMPDIR=/tmp`）。
- git remote 只有 `upstream`（HaujetZhao 原作者仓，**勿推**）。当前在
  `a6f22e8 (upstream v2.5+patches)` → `1c0834f (PM2)` → 本地 `b31dfb4`、`0f1250a`。
  备份：`config_server.py.bak-pre-mlx`。
- **venv**：`venv/`，Python 3.12，**uv 建的、没有 pip**（装包用 `VIRTUAL_ENV=$PWD/venv uv pip install ...`，
  uv 在 `/opt/homebrew/bin/uv`）。已装 `mlx-qwen3-asr==0.3.5`。
- **models**：`models/Qwen3-ForcedAligner/Qwen3-ForcedAligner-0.6B/`（3 文件 ~776MB：
  `qwen3_aligner_encoder_frontend.int4.onnx` / `..._backend.int4.onnx` / `qwen3_aligner_llm.q5_k.gguf`），
  2026-06-26 从 dev 仓拷入（之前是空的，文件转录一直降级为字符均分估算时间戳）。
- **llama 后端**：`core/server/engines/llama/bin/` 有 b7798 macOS arm64 dylib（`libllama.dylib`/
  `libggml*.dylib`，含 `libggml-metal.dylib`），已清 quarantine。
- ASR 模型权重走 HF 共享缓存 `~/.cache/huggingface`（`Qwen/Qwen3-ASR-0.6B` 已缓存，迁移不必重下）。

## 实测数据（决策参考）

- RTF（60s 中文音频，预热稳态，footprint 口径）：听写 ~0.07；文件转录 ASR+Metal aligner ~0.12。
- 内存：听写常驻 ~3.3GB；文件转录瞬时峰值 ~7GB（故 `max_memory_restart` 已设 12G）。
- 首次文件请求会多花 ~0.3s 加载对齐模型（aligner 按需加载、闲置 10s 卸载，既有设计）。

## 关键路径 / 命令

- DEV 仓：`~/Dev/projects/CapsWriter-Offline-with-AI`（branch master，
  origin `git@github.com:zj1123581321/CapsWriter-Offline-with-AI.git`）。
- 验证脚本（dev，装了 mlx 的 `.venv-test`/`.venv-mlx` 见 memory `mlx-engine-dev-setup`）：
  `python -m pytest tests/ -q`、`python scripts/_verify_mlx_asr.py <wav>`。
- 真实测试音频：`~/Production/funasr_spk_server/tests/fixtures/audio/tts_1speaker_5s.wav`（中文单人）、
  `~/Production/funasr_spk_server/temp/samples/test_60s.wav`。
- 生产端到端测：`venv/bin/python tools/test_ws_client.py --server ws://localhost:6017 --wav <wav>`。
- 回滚现状：`config_server.py` 改 `model_type='qwen_asr'` 重启（GGUF 引擎保留）。

## 别碰

- `capswriter-server`（6016，`~/Production/capswriter_server_main`，paraformer）——**别的项目**。
- 生产 `qwen_asr_server` 的 git **不要 push 到 upstream**（那是原作者仓）。

## 建议第一步

先跟用户敲定上面 (a)(b) 两个决定，再动手。然后：dev master 加 `CW_MODEL_TYPE`/`CW_PORT` 覆盖 →
commit & push → 停 qwen-asr-server → 按选定方式把生产迁到 master（保住 venv/models/dylib）→
ecosystem env 写部署差异 → pm2 起 → `tools/test_ws_client.py` 验证听写+文件转录 → `pm2 save`。

> 顺带可提：`capswriter_server_main`（6016）将来也能用同一套 env 覆盖收编，但那是别的项目，本次不动。
