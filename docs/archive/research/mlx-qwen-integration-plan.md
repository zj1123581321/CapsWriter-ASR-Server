# 把 Apple MLX 版 Qwen3-ASR 整合进主线引擎体系 — 调研报告

> 状态：只读调研，未改动任何源码。
> 主线项目：`/Users/zhanglixing/Dev/projects/CapsWriter-Offline-with-AI`（master）
> MLX 推理源：`/Users/zhanglixing/Production/qwen_asr_server/tools/mlx_ws_server/`
> 日期：2026-06-25

---

## 0. 核心结论（TL;DR）

- MLX 推理在 `mlx_ws_server/server.py` 里已经被封装成一个干净的 `ASREngine` 类，底层依赖 **第三方 PyPI 包 `mlx-qwen3-asr`（import 名 `mlx_qwen3_asr`）的 `Session` API**。接口非常简单：`Session(model=..., dtype=...)` 加载，`session.transcribe(audio_np, return_timestamps=True, return_chunks=True)` 推理，返回带 `.text / .language / .segments` 的结果对象。
- 主线引擎契约非常薄：一个引擎只需继承 `BaseASREngine`，实现 4 个东西 —— `capabilities` 属性、`create_stream()`、`decode_stream(stream, context, language, **kwargs)`、`cleanup()`，并把识别文本写回 `stream.result.text`。`qwen_asr_gguf` 引擎就是最贴近的模板（它声明 `[ASR, PUNC]`，纯文本输出，时间戳交给 Pipeline 的外挂 Aligner 补齐）。
- **整合工作量小**：新增一个 `core/server/engines/qwen_asr_mlx/` 目录（1 个 `asr_engine.py` + `__init__.py`），在 `factory.py` 注册一个 `_load_qwen_asr_mlx` loader + `_ASR_LOADERS` 映射项，在 `config_server.py` 加一个 `QwenASRMLXArgs` 配置类，并把 `model_type='qwen_asr_mlx'` 列入可选项。预计 **0.5～1 人天**（核心代码 < 150 行），主要成本在依赖安装与真机联调。
- **最大风险是平台限定**：`mlx` / `mlx-qwen3-asr` 仅在 **Apple Silicon (arm64 macOS)** 上可用，不能进 Windows/Linux 发行版的强依赖。必须做成**延迟导入 + 可选依赖**（与现有 `import sherpa_onnx`、GGUF 后端的延迟导入风格一致），不污染跨平台打包。
- MLX 引擎建议声明能力为 `[ASR, PUNC]`（Qwen3-ASR 自带标点），**不声明 TIMESTAMPS**：虽然 MLX `transcribe` 能返回 segment 级时间戳，但那是“段级”不是“字级 token 级”，与主线 `text_accu`（字级 token 去重）+ Aligner 的字级对齐契约不吻合。保持与 `qwen_asr_gguf` 一致由外挂 Aligner 补字级时间戳，最稳。

---

## 1. MLX 推理现状（`mlx_ws_server`）

### 1.1 依赖库与模型

| 项 | 值 | 来源 |
|---|---|---|
| 推理库 | PyPI 包 `mlx-qwen3-asr`，`from mlx_qwen3_asr import Session` | `server.py:134` |
| 底层框架 | Apple **MLX / Metal**（Apple Silicon 原生 GPU） | 文件头 docstring |
| 默认模型 | `Qwen/Qwen3-ASR-0.6B`（HuggingFace repo id，运行时自动下载/缓存） | `server.py:121,549` |
| 可选模型 | `Qwen/Qwen3-ASR-1.7B`（benchmark 脚本里也测） | `run_benchmark.sh` |
| dtype | 可选 `float16`/`bfloat16`，默认由库决定 | `server.py:551` |
| 模型文件格式 | **不是本地 GGUF/ONNX 文件**，而是 HF repo id，由 `mlx-qwen3-asr` 自行加载（MLX 把 safetensors 权重重映射到 MLX 数组）。也支持传本地路径。 | `--model` 帮助文本 |

> 注意：这与主线 `qwen_asr_gguf` 的“本地 ONNX encoder + 本地 GGUF decoder 文件”形态**完全不同**。MLX 引擎是“给个 repo id / 目录，库自己搞定权重”。这影响离线打包策略（见 §6）。

### 1.2 推理调用接口（`mlx_ws_server/server.py` 的 `ASREngine` 类）

```python
class ASREngine:
    def __init__(self, model="Qwen/Qwen3-ASR-0.6B", dtype=None): ...

    def load(self) -> float:
        from mlx_qwen3_asr import Session          # 延迟导入
        self._session = Session(model=..., dtype=...)   # 加载，返回耗时

    def transcribe(self, audio_np: np.ndarray, language="auto") -> dict:
        kwargs = {"return_timestamps": True, "return_chunks": True}
        if language and language != "auto":
            kwargs["language"] = language           # 语言用 Qwen 英文明称
        result = self._session.transcribe(audio_np, **kwargs)
        # result.text / result.language / result.segments (list[{text,start,...}])
        tokens, timestamps = [], []
        if result.segments:
            for seg in result.segments:
                tokens.append(seg.get("text", ""))
                timestamps.append(seg.get("start", 0.0))
        return {"text": result.text or "", "language": result.language or "",
                "tokens": tokens, "timestamps": timestamps}

    @property
    def model_info(self): return self._session.model_info
```

要点：
- **音频输入**：`np.ndarray`，`float32`、**16kHz 单声道**（与主线 `stream.accept_waveform` 拿到的 `samples` 完全一致）。
- **输出**：`result.text`（含标点）、`result.language`、`result.segments`（**段级**，每段有 `text` 和 `start`，不是字级 token）。
- **时间戳**：有，但**段级**（segment-level），不是字级 token 级。
- **标点**：有（Qwen3-ASR 自带标点）。
- **热词**：MLX 封装里**没有用热词**，`transcribe` 没传热词参数；`mlx-qwen3-asr` 是否支持 prompt/context 注入需另查上游，当前封装未用。
- **生命周期**：`Session` 一次加载常驻，`run_in_executor` 把同步 `transcribe` 丢线程池避免阻塞 asyncio（在主线里我们跑在独立 worker 进程，无此顾虑）。

### 1.3 Benchmark 性能（来自 deepresearch 文档 + benchmark 脚本）

`mlx_ws_server` 自带 `benchmark_client.py` + `run_benchmark.sh`，测 5/10/20/60s 合成与真实音频，记录 `inference_latency`、`rtf = latency/duration`、`roundtrip_time`。具体跑分数据未落库（需真机运行）。

调研文档 `docs/lixing/deepresearch-qwen3asr/`（chatgpt/gemini/kimi）给出的**外推/锚点**结论：

| 配置 | RTF / 吞吐 | 内存 | 置信度 |
|---|---|---|---|
| MLX / Metal fp16，1.7B（M4 Pro 实测锚点） | RTF ≈ 0.27～0.28（约 3.6x 实时） | 占用 ≈ 3.4 GB | 中（来自 mlx-qwen3-asr 官方 artifacts） |
| MLX / Metal fp16，1.7B（M1 Max 外推） | 吞吐 1.9～2.7 秒音频/秒 | 载入 3.4～4.2GB，峰值 4.5～6GB | 中 |
| MLX / Metal 8-bit，1.7B（M1 Max 外推） | 吞吐 3.8～6.7 秒音频/秒 | 载入 2.0～2.8GB | 中低 |

结论：**MLX 是 Apple Silicon 上"省心 + 可维护 + GPU 原生"的最佳路线**，RTF 远小于 1（快于实时），内存占用比官方 PyTorch/CPU 路线低很多。

---

## 2. 主线引擎接口契约

### 2.1 基类（`core/server/engines/base.py`）

一个 ASR 引擎必须继承 `BaseASREngine(ABC)` 并实现：

| 成员 | 类型 | 说明 | 是否抽象 |
|---|---|---|---|
| `__init__(self, config)` | 构造 | 接收 ConfigClass 实例（由工厂注入），父类存到 `self.config` | 继承即可 |
| `capabilities` | `@property -> List[EngineCapabilities]` | 声明能力位 | **抽象，必须实现** |
| `create_stream(self, hotwords=None)` | `-> RecognitionStream` | 返回一个 stream 对象 | **抽象，必须实现** |
| `decode_stream(self, stream, context=None, **kwargs)` | 推理 | 执行推理，把结果写回 `stream.result` | **抽象，必须实现** |
| `update_hotwords(self, hotwords: List[str])` | 可选 | 默认 no-op；仅声明 HOTWORDS 时才被调用 | 继承即可 |
| `cleanup(self)` | 释放 | 释放模型资源 | **抽象，必须实现** |

`EngineCapabilities` 枚举：`ASR / PUNC / TIMESTAMPS / STREAMING / HOTWORDS`（`enum.auto()`）。

`RecognitionStream(ABC)`：构造时持有 `self.sample_rate` 与 `self.result = RecognitionResult()`，抽象方法 `accept_waveform(sample_rate, audio)`。

`RecognitionResult`（dataclass）字段：`text / tokens / timestamps / language / duration / performance`。**Pipeline 主要读 `stream.result.text`**（见 §2.3）。

### 2.2 工厂与配置注入（`core/server/engines/factory.py`）

`EngineFactory._ASR_LOADERS` 是 `model_type -> loader 函数` 的映射。每个 loader 返回三元组 `(EngineClass, ConfigClass, ArgsObj)`。`create_asr_engine` 的关键逻辑：

```python
EngineClass, ConfigClass, ArgsObj = loader()
config_data = {k: v for k, v in ArgsObj.__dict__.items() if not k.startswith('_')}
config = ConfigClass(**config_data)   # Args 的公开类属性 -> ConfigClass 的构造参数
return EngineClass(config)
```

即：**`ArgsObj` 是 `config_server.py` 里的一个普通类（如 `Qwen3ASRGGUFArgs`），它的所有非下划线类属性会被原样当 kwargs 传给 `ConfigClass`**。所以 ConfigClass 的 dataclass 字段名必须与 Args 类属性名一一对应。

### 2.3 ModelLoader 智能补丁（`core/server/worker/model_loader.py`）

`ModelLoader.load()` 流程：
1. `recognizer = EngineFactory.create_asr_engine(Config.model_type)`
2. `caps = recognizer.capabilities`
3. `PUNC not in caps` → `_load_punc_model()`（挂 CT-Transformer 标点）
4. `TIMESTAMPS not in caps` → `_load_align_model()`（挂 `ManagedAlignerProxy`，按需加载 + 闲置卸载）
5. `HOTWORDS in caps and hotwords_path.exists()` → 读 `hot-server.txt` 调 `recognizer.update_hotwords(...)`

### 2.4 Pipeline 调用序列（`core/server/worker/pipeline.py` `TaskPipeline.process`）

```python
samples = process_audio_task(task, result)          # 预处理 -> float32 16k 单声道
stream = self.recognizer.create_stream()
stream.accept_waveform(task.samplerate, samples)
self.recognizer.decode_stream(stream, context=task.context, language=task.language)
asr_raw_text = stream.result.text                   # ← 引擎只需把文本写到这里
self._process_simple_merge(result, asr_raw_text)    # 文本拼接 (text)
# 仅当 task.type=='file' 且 TIMESTAMPS not in caps 且有 aligner 时，调 aligner.align() 补字级时间戳
```

**关键点**：对听写（mic）路径，引擎**只需把识别文本填到 `stream.result.text`**，其余（标点外挂、字级时间戳、text_accu）都由 Pipeline/ModelLoader 处理。这正是 `qwen_asr_gguf` 引擎的做法。

### 2.5 模板：`qwen_asr_gguf` 引擎逐文件读透

- `core/server/engines/qwen_asr_gguf/asr_engine.py`（**适配层，整合时主要照抄此文件结构**）：
  - `QwenASRStream(RecognitionStream)`：`accept_waveform` 把音频存进 `self.audio_data`（`astype(np.float32)`）。
  - `QwenASREngine(BaseASREngine)`：
    - `__init__(config)`：`self.engine = QwenInternalEngine(config)`（底层推理引擎，GGUF 版是 `inference/asr.py`）。
    - `capabilities` → `[EngineCapabilities.ASR, EngineCapabilities.PUNC]`（**不含 TIMESTAMPS**，时间戳靠外挂 Aligner）。
    - `create_stream(hotwords=None)` → `QwenASRStream()`。
    - `decode_stream(stream, context, language, temperature=0.4, **kwargs)`：截断到 `chunk_size`、`encoder.encode()` → `_build_prompt_embd()`（语言用 `get_language(ENGINE_QWEN_ASR, language)` 映射成 Qwen 英文明称）→ `_safe_decode()` → 写 `stream.result.text = res.text`。
    - `update_hotwords` → no-op（GGUF 版不支持）。
    - `cleanup()` → `self.engine.shutdown()`。
  - `inference/`：GGUF 版自带 encoder（ONNX）、llama（llama.cpp）、aligner、schema 等一大堆重实现。**MLX 版完全不需要这一坨** —— MLX 把推理都委托给 `mlx_qwen3_asr.Session`，适配层非常薄。
- `core/server/engines/qwen_asr_gguf/inference/schema.py`：`ASREngineConfig`（dataclass）字段 `model_dir / encoder_frontend_fn / encoder_backend_fn / llm_fn / onnx_provider / llm_use_gpu / dml_pad_to / n_ctx / chunk_size / memory_num / verbose / enable_aligner / align_config`。**MLX 版要定义自己的轻量 ConfigClass**，字段对应 MLX Args（见 §4）。
- `config_server.py` 的 `Qwen3ASRGGUFArgs`：纯类属性（`model_dir / encoder_*_fn / llm_fn / onnx_provider / llm_use_gpu / n_ctx / chunk_size / memory_num / dml_pad_to / verbose`），会被工厂展开成 ConfigClass kwargs。

### 2.6 语言映射（`core/server/engines/language.py`）

统一码（`auto/chinese/english/...`）→ 引擎标识。Qwen3-ASR 用**英文明称首字母大写**（`Chinese/English/...`）。常量 `ENGINE_QWEN_ASR = "qwen_asr"`，工具函数 `get_language(engine, unified_code)`。

> MLX 整合时可**直接复用 `ENGINE_QWEN_ASR` 映射**（同一模型家族，语言标识格式相同 —— 都是 Qwen3 英文明称）。无需新增 language.py 条目。

---

## 3. 逐步整合方案（建议引擎名 `qwen_asr_mlx`）

设计原则：**适配层极薄**，把 `mlx_ws_server/server.py` 里的 `ASREngine` 逻辑搬进一个 `decode_stream`，能力声明对齐 `qwen_asr_gguf`（`[ASR, PUNC]`），时间戳交给外挂 Aligner。延迟导入 `mlx_qwen3_asr`，不污染跨平台。

### 3.1 文件清单

| 操作 | 文件 | 内容 |
|---|---|---|
| 新增 | `core/server/engines/qwen_asr_mlx/__init__.py` | 空或仅 `from .. import logger` |
| 新增 | `core/server/engines/qwen_asr_mlx/asr_engine.py` | `QwenASRMLXStream` + `QwenASRMLXEngine` + 轻量 `MLXEngineConfig`（dataclass） |
| 改 | `core/server/engines/factory.py` | 加 `_load_qwen_asr_mlx` 静态方法 + `_ASR_LOADERS['qwen_asr_mlx']` 映射项 |
| 改 | `config_server.py` | 加 `QwenASRMLXArgs` 类；`model_type` 注释列出新选项 |
| 改（可选） | `requirements-server-*.txt` / 新建 `requirements-server-macos.txt` | 加 `mlx`、`mlx-qwen3-asr`（仅 macOS arm64） |
| 改（可选） | `CLAUDE.md` 模型支持表 | 增加 `qwen_asr_mlx` 行 |

### 3.2 `qwen_asr_mlx/asr_engine.py` 骨架（实现参考，非最终代码）

```python
# coding=utf-8
from dataclasses import dataclass
from typing import Optional, List
import numpy as np
from ..base import BaseASREngine, RecognitionStream, EngineCapabilities
from ..language import get_language, ENGINE_QWEN_ASR

@dataclass
class MLXEngineConfig:
    model: str = "Qwen/Qwen3-ASR-0.6B"   # HF repo id 或本地目录
    dtype: Optional[str] = None          # float16 / bfloat16 / None
    chunk_size: float = 80.0             # 与 server 时间切片对齐
    verbose: bool = False

class QwenASRMLXStream(RecognitionStream):
    def __init__(self, sample_rate=16000):
        super().__init__(sample_rate)
        self.audio_data = None
    def accept_waveform(self, sample_rate, audio):
        self.sample_rate = sample_rate
        self.audio_data = audio.astype(np.float32)

class QwenASRMLXEngine(BaseASREngine):
    def __init__(self, config: MLXEngineConfig):
        super().__init__(config)
        from mlx_qwen3_asr import Session       # 延迟导入，仅 Apple Silicon
        kwargs = {"model": config.model}
        if config.dtype:
            kwargs["dtype"] = config.dtype
        self._session = Session(**kwargs)

    @property
    def capabilities(self) -> List[EngineCapabilities]:
        return [EngineCapabilities.ASR, EngineCapabilities.PUNC]

    def create_stream(self, hotwords: Optional[str] = None) -> QwenASRMLXStream:
        return QwenASRMLXStream()

    def decode_stream(self, stream, context=None, language=None, **kwargs):
        if stream.audio_data is None:
            return
        audio = stream.audio_data
        max_samples = int(self.config.chunk_size * 16000)
        if len(audio) > max_samples:
            audio = audio[:max_samples]
        t_kwargs = {"return_timestamps": False}   # mic 只要文本；时间戳交外挂 Aligner
        if language:
            mapped = get_language(ENGINE_QWEN_ASR, language)
            if mapped:
                t_kwargs["language"] = mapped
        result = self._session.transcribe(audio, **t_kwargs)
        stream.result.text = result.text or ""

    def cleanup(self):
        self._session = None
```

> 备选：若希望 MLX 自带的段级时间戳也用上，可声明 `[ASR, PUNC, TIMESTAMPS]` 并在 `decode_stream` 里把 `result.segments` 拆成 `stream.result.tokens/timestamps`。**但不建议初版这么做** —— 段级不是字级，会破坏 `text_accu`（字级去重）与文件转录字幕的字级时间戳预期。初版按 `[ASR, PUNC]` 走外挂 Aligner，与 `qwen_asr_gguf` 完全一致，风险最低。

### 3.3 `factory.py` 改动

```python
@staticmethod
def _load_qwen_asr_mlx():
    from .qwen_asr_mlx.asr_engine import QwenASRMLXEngine, MLXEngineConfig
    from config_server import QwenASRMLXArgs
    return QwenASRMLXEngine, MLXEngineConfig, QwenASRMLXArgs

_ASR_LOADERS = {
    'sensevoice': _load_sensevoice,
    'paraformer': _load_paraformer,
    'fun_asr_nano': _load_fun_asr_nano,
    'qwen_asr': _load_qwen_asr,
    'qwen_asr_mlx': _load_qwen_asr_mlx,   # ← 新增
}
```

> 注意 `factory.py` 顶部已从 `config_server` import 了各 Args 类，建议把 `QwenASRMLXArgs` 也加进那个 import（保持与现有风格一致），上面在 loader 内 import 只是示意。

### 3.4 `config_server.py` 改动

```python
class QwenASRMLXArgs:
    """Qwen3-ASR MLX (Apple Silicon) 模型参数配置"""
    model = _env_str('CW_MLX_MODEL', 'Qwen/Qwen3-ASR-0.6B')  # repo id 或本地目录
    dtype = _env_str('CW_MLX_DTYPE', '') or None             # float16 / bfloat16 / 空=默认
    chunk_size = 80.0
    verbose = False
```

并把 `ServerConfig.model_type` 的注释更新为：
`# 语音模型选择：'qwen_asr', 'qwen_asr_mlx', 'fun_asr_nano', 'sensevoice', 'paraformer'`

> `QwenASMLXArgs` 的公开类属性会被工厂自动展开成 `MLXEngineConfig(**...)` 的 kwargs，**字段名必须严格对应**（`model / dtype / chunk_size / verbose`）。

### 3.5 验证步骤

1. Apple Silicon 机器：`pip install mlx mlx-qwen3-asr`（首次 `Session(...)` 会从 HF 下载 0.6B 权重）。
2. `config_server.py` 设 `model_type = 'qwen_asr_mlx'`，启动 `start_server.py`，确认日志「引擎加载成功，能力清单: ['ASR','PUNC']」、且自动挂载了 Aligner（因无 TIMESTAMPS）、未挂 Punc（有 PUNC）。
3. 客户端听写：验证文本+标点正确上屏。
4. 文件转录：拖入音频，验证 `.srt` 字级时间戳由外挂 Aligner 正常生成。
5. 跑 `mlx_ws_server/benchmark_client.py` 或自测 RTF。

---

## 4. 工作量评估

| 任务 | 估时 | 说明 |
|---|---|---|
| 新建 `qwen_asr_mlx/asr_engine.py`（适配层） | 1～2h | 照搬 server.py 的 ASREngine，去掉 ws/asyncio，< 150 行 |
| `factory.py` + `config_server.py` 接线 | 0.5h | 各 1 个映射项 + 1 个 Args 类 |
| 依赖与 requirements 处理 | 0.5h | 仅 macOS 可选依赖，延迟导入 |
| 真机联调（mic + file 两路 + 语言映射） | 2～4h | 含权重下载、RTF 测试、字幕时间戳验证 |
| 文档（CLAUDE.md 模型表 + 本报告） | 0.5h | — |
| **合计** | **0.5～1 人天** | 核心代码量极小，成本主要在联调 |

复用程度高：**直接复用** `BaseASREngine` 契约、`ManagedAlignerProxy`（字级时间戳）、CT-Transformer 标点（实际不会触发因为有 PUNC）、`language.py` 的 `ENGINE_QWEN_ASR` 映射、Pipeline 的 text/text_accu 合并 —— 新引擎只贡献「拿 numpy 音频 → 调 MLX → 返回文本」这一段。

---

## 5. 风险与依赖

| 风险 | 等级 | 缓解 |
|---|---|---|
| **平台限定**：`mlx`/`mlx-qwen3-asr` 仅 Apple Silicon (arm64 macOS) | 高 | 延迟导入（仅 `model_type='qwen_asr_mlx'` 时才 `import mlx_qwen3_asr`）；不进默认 requirements；单独 `requirements-server-macos.txt` 或 extras。绝不能让 Windows/Linux 打包链 import 到它。 |
| **第三方包成熟度**：`mlx-qwen3-asr` 非官方一等公民，API 可能变动 | 中 | 适配层薄，pin 版本号；`Session.transcribe` 的 `return_chunks/return_timestamps/language` 参数以上游为准，需在真机确认签名。 |
| **模型分发形态差异**：MLX 用 HF repo id 自动下载，而非主线的本地 `models/` 文件夹 | 中 | 与主线「全本地、离线」理念有出入。可在 Args 里支持本地目录路径，并文档说明首次需联网下载/或预置到本地目录。 |
| **段级 vs 字级时间戳**：MLX 段级时间戳与主线字级 token 契约不一致 | 中 | 初版声明 `[ASR,PUNC]`，时间戳走外挂 Aligner（与 qwen_asr_gguf 一致），不暴露 TIMESTAMPS。 |
| **热词**：MLX 封装未支持热词/prompt 注入 | 低 | 不声明 HOTWORDS；客户端侧仍有音素 RAG 热词纠正兜底，服务端热词缺失影响有限。 |
| **依赖共存**：MLX (Metal) 与现有 ONNX/GGUF/sherpa_onnx 在同一环境 | 低 | 各引擎延迟导入、互不加载；同进程只会按 `model_type` 实例化其一，无运行时冲突。仅 pip 解析时 `mlx` 系包体积较大。 |
| **冷启动/权重下载耗时**：首次 `Session()` 下载 0.6B/1.7B 权重 | 低 | worker 子进程一次性加载常驻；文档提示首次联网。 |

### 新增 requirements 项（仅 macOS arm64）

```
mlx               # Apple MLX 框架
mlx-qwen3-asr     # import 名 mlx_qwen3_asr，提供 Session API
```

建议放进独立的 `requirements-server-macos.txt`，**不要**塞进 `requirements-server-linux.txt` 或 Windows 默认依赖。

---

## 6. 与现有 `qwen_asr_gguf` 的关系建议

二者是**同一模型（Qwen3-ASR）的不同推理后端**：

| | `qwen_asr`（GGUF） | `qwen_asr_mlx`（MLX，本方案） |
|---|---|---|
| 后端 | ONNX encoder + llama.cpp(GGUF) decoder | Apple MLX / Metal |
| 平台 | Win/Linux（CPU/CUDA/DML/Vulkan） | 仅 Apple Silicon |
| 模型文件 | 本地 `models/Qwen3-ASR/` 多文件 | HF repo id（库自动加载）或本地目录 |
| 能力声明 | `[ASR, PUNC]` | `[ASR, PUNC]`（建议一致） |
| 时间戳 | 外挂 Aligner | 外挂 Aligner（建议一致） |
| 适配层代码量 | 大（自带 inference/ 全套） | 极小（委托给 Session） |

→ 互补共存，由 `config_server.model_type` 选择。Mac 用户用 `qwen_asr_mlx` 享受 GPU 原生加速，Win/Linux 用户继续用 `qwen_asr`/`fun_asr_nano`。

---

## 附：关键源码位置索引

- 引擎基类/能力位：`core/server/engines/base.py`
- 引擎工厂：`core/server/engines/factory.py`（`_ASR_LOADERS`、`create_asr_engine`）
- 模型加载器/智能补丁：`core/server/worker/model_loader.py`
- 推理流水线：`core/server/worker/pipeline.py`（`TaskPipeline.process`）
- 语言映射：`core/server/engines/language.py`（`ENGINE_QWEN_ASR`、`get_language`）
- GGUF 模板引擎：`core/server/engines/qwen_asr_gguf/asr_engine.py`、`inference/schema.py`
- 服务端配置：`config_server.py`（`ServerConfig.model_type`、`Qwen3ASRGGUFArgs`、`ModelPaths`）
- MLX 推理源：`/Users/zhanglixing/Production/qwen_asr_server/tools/mlx_ws_server/server.py`（`ASREngine` 类，行 116-194）
- MLX benchmark：`/Users/zhanglixing/Production/qwen_asr_server/tools/mlx_ws_server/benchmark_client.py`、`run_benchmark.sh`
- 调研文档：`/Users/zhanglixing/Production/qwen_asr_server/docs/lixing/deepresearch-qwen3asr/{chatgpt,gemini,kimi}.md`

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | ISSUES_OPEN | 7 issues, 1 critical gap |
| Outside Voice | `/codex review` | Independent 2nd opinion | 1 | ISSUES_FOUND | P0 漏项 + 5 盲点 |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |

**契约核实:** plan 对 `base.py`/`factory.py`/`qwen_asr_gguf`/`model_loader.py`/`pipeline.py`/`language.py` 的契约声明全部属实;MLX 源 `Session` API 描述准确。`[ASR,PUNC]`+外挂 Aligner 架构判断正确。

**7 项决策(全部已定,0 unresolved):**
1. 离线源 → 保留 HF 自动下载 + 文档说明
2. 子进程+Metal 验证 → **先写冒烟脚本再写适配层**
3. 第三方 API → 真机验 `transcribe()` 签名 + requirements pin 确切版本
4. 验证交付物 → 提交可复跑脚本(mic+file+边界+加载失败)
5. **P0 check_model** → 加 `qwen_asr_mlx` 分支 + repo-id 旁路(plan 原清单漏了 `check_model.py`)
6. sherpa_onnx 依赖 → macOS requirements 保留,不改代码
7. context 续写 → 初版不支持,文档标已知限制(+ TODOS.md)

**CRITICAL GAP:** `Session()` 加载失败 → 子进程 crash loop(`process_manager.py:96` 报错不具体),呼应交接文档 ~900 次重启。已由决策 4(验证脚本含加载失败)+ T7(改清晰报错)覆盖。

**CODEX:** 抓到 plan 与首轮评审共同漏掉的 P0(`check_model` 硬拒未知 model_type)+ 5 个盲点(sherpa_onnx 无条件 import、不重采样、context 丢弃、directml 依赖矩阵、工期低估)。全部亲自核实属实。

**CROSS-MODEL:** 无矛盾——Codex 与首轮评审互补(找盲点而非反驳),强一致信号。工期由 plan 的 0.5~1 人天修正为 **1.5~2 人天**(整合面比适配层大)。

**VERDICT:** ENG REVIEW 完成,7 决策全部落定,但状态 ISSUES_OPEN(P0 必修 + 1 critical gap 待实现)。实现 T1(check_model)+ T2(冒烟)前**不建议**直接进开发。CEO/Design review 本变更不需要(后端引擎,无 UI/产品方向变化)。

NO UNRESOLVED DECISIONS

