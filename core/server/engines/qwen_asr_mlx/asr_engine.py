# coding=utf-8
"""
Apple MLX 版 Qwen3-ASR 引擎适配层。

推理委托给第三方 PyPI 包 mlx-qwen3-asr 的 Session API，本层只做
「numpy 音频 → 调 MLX → 写回 stream.result.text」这一段，其余（标点外挂、
字级时间戳、text/text_accu 拼接）全部由 ModelLoader / TaskPipeline 处理。

decode_stream 处理流水线：
    stream.audio_data (float32/16k)
        │  None? ──► 直接返回（空音频早退）
        ▼
    断言音频不超过 chunk_size * 16000 采样
        ▼
    语言映射 get_language(ENGINE_QWEN_ASR, lang) → Qwen 英文明称
        ▼
    Session.transcribe(audio, return_timestamps=False[, language=...])
        ▼
    stream.result.text = result.text        ← Pipeline 只读这里

设计约束（见 docs/archive/research/mlx-qwen-integration-plan.md 评审报告）：
  - mlx / mlx-qwen3-asr 仅 Apple Silicon → 延迟导入，放在 __init__ 内
  - 能力 [ASR, PUNC]，不声明 TIMESTAMPS（段级≠字级，交外挂 Aligner）
  - 初版不支持 context 续写（已知限制，见 TODOS.md）
  - 模型来源默认 HF repo id，首次运行联网下载（见 README / CLAUDE.md）
"""
from dataclasses import dataclass
from typing import Optional, List

import numpy as np

from ..base import BaseASREngine, RecognitionStream, EngineCapabilities
from ..language import get_language, ENGINE_QWEN_ASR
from .. import logger


@dataclass
class MLXEngineConfig:
    """qwen_asr_mlx 引擎配置。字段名须与 config_server.QwenASRMLXArgs 公开属性一致。"""
    model: str = "Qwen/Qwen3-ASR-0.6B"   # HF repo id 或本地模型目录
    dtype: Optional[str] = None          # 'float16' / 'bfloat16' / None=库默认
    chunk_size: float = 80.0             # 单段最大音频时长（秒）
    verbose: bool = False


class QwenASRMLXStream(RecognitionStream):
    """MLX Qwen-ASR 识别流：仅缓存整段音频，推理时一次性喂入。"""
    def __init__(self, sample_rate: int = 16000):
        super().__init__(sample_rate)
        self.audio_data: Optional[np.ndarray] = None

    def accept_waveform(self, sample_rate: int, audio: np.ndarray):
        self.sample_rate = sample_rate
        self.audio_data = audio.astype(np.float32)


class QwenASRMLXEngine(BaseASREngine):
    """委托 mlx-qwen3-asr Session 的薄适配层。"""

    def __init__(self, config: MLXEngineConfig):
        super().__init__(config)
        # 延迟导入：mlx / mlx-qwen3-asr 仅 Apple Silicon 可用，
        # 绝不能在模块顶层 import，否则污染跨平台打包链。
        try:
            from mlx_qwen3_asr import Session
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                "无法导入 mlx_qwen3_asr。qwen_asr_mlx 引擎仅支持 Apple Silicon (arm64 macOS)，"
                "且需安装依赖：pip install mlx mlx-qwen3-asr（见 requirements-server-macos.txt）。"
                f"\n原始错误：{e}"
            ) from e

        kwargs = {"model": config.model}
        if config.dtype:
            # Session 期望 mx.Dtype（非字符串，已核 mlx-qwen3-asr 0.3.5 源码无字符串强转），
            # 把配置里的 dtype 名映射成 mlx.core 的 dtype 对象。
            import mlx.core as mx
            if not hasattr(mx, config.dtype):
                raise RuntimeError(
                    f"无效的 dtype '{config.dtype}'，应为 mlx.core 支持的 dtype 名（如 float16 / bfloat16）"
                )
            kwargs["dtype"] = getattr(mx, config.dtype)

        logger.info(f"加载 MLX Qwen3-ASR：model={config.model}, dtype={config.dtype or '默认'}")
        try:
            self._session = Session(**kwargs)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"加载 MLX 模型失败：model={config.model}。"
                "若为 HF repo id，首次运行需联网下载权重；也可改用本地模型目录。"
                f"\n原始错误：{e}"
            ) from e

    @property
    def capabilities(self) -> List[EngineCapabilities]:
        # 不声明 TIMESTAMPS：MLX 段级时间戳与主线字级 token 契约不符，交外挂 Aligner。
        return [EngineCapabilities.ASR, EngineCapabilities.PUNC]

    def create_stream(self, hotwords: Optional[str] = None) -> QwenASRMLXStream:
        return QwenASRMLXStream()

    def decode_stream(self, stream: QwenASRMLXStream, context: Optional[str] = None,
                      language: Optional[str] = None, **kwargs):
        if stream.audio_data is None:
            return

        audio = stream.audio_data
        # 服务端保证分段不超过引擎上限；违反时失败，不能静默丢弃尾部。
        max_samples = int(self.config.chunk_size * 16000)
        if len(audio) > max_samples:
            actual_seconds = len(audio) / 16000
            raise ValueError(
                f"音频时长 {actual_seconds:.3f}s 超过 chunk_size 上限 "
                f"{self.config.chunk_size:.3f}s"
            )

        # mic 路径只要文本，时间戳交外挂 Aligner；初版不支持 context（见 TODOS.md）
        t_kwargs = {"return_timestamps": False}
        if language:
            mapped = get_language(ENGINE_QWEN_ASR, language)
            if mapped:
                t_kwargs["language"] = mapped

        result = self._session.transcribe(audio, **t_kwargs)
        stream.result.text = (getattr(result, "text", None) or "")

    def update_hotwords(self, hotwords: List[str]):
        # MLX 封装未支持热词/prompt 注入；客户端音素 RAG 热词兜底。
        pass

    def cleanup(self):
        # 丢弃 Session 引用，由 GC 释放 Metal 显存。
        self._session = None
