# coding: utf-8
"""无模型文件的可编程 ASR 引擎，供真实 TaskPipeline 子进程使用。"""
from __future__ import annotations

import os
import time
from typing import List, Optional

import numpy as np

from core.server.engines.base import (
    BaseASREngine,
    EngineCapabilities,
    RecognitionStream,
)

IDENTITY_MARKER = "\x1eT7:"


class FakeRecognitionStream(RecognitionStream):
    def __init__(self):
        super().__init__()
        self.samples = np.empty(0, dtype=np.float32)

    def accept_waveform(self, sample_rate: int, audio: np.ndarray):
        self.sample_rate = sample_rate
        self.samples = audio


class ProgrammableFakeEngine(BaseASREngine):
    """将音频绝对起点与长度写入结果，并可按调用序号延迟或抛错。"""

    def __init__(
        self,
        config=None,
        *,
        fail_on_call: Optional[int] = None,
        delay_on_call: Optional[int] = None,
        delay_seconds: float = 0.0,
        exit_on_call: Optional[int] = None,
        exit_code: int = 3,
        supports_punc: bool = False,
        supports_timestamps: bool = False,
        result_text: Optional[str] = None,
        result_tokens: Optional[list[str]] = None,
        result_timestamps: Optional[list[float]] = None,
        calls=None,
    ):
        super().__init__(config)
        self.fail_on_call = fail_on_call
        self.delay_on_call = delay_on_call
        self.delay_seconds = delay_seconds
        # 真实进程崩溃探针：在第 N 次解码时让整个 worker 进程 os._exit(非零)，
        # 不做任何清理——与真实推理进程崩溃同形，父进程只能靠存活监控发现。
        self.exit_on_call = exit_on_call
        self.exit_code = exit_code
        self.supports_punc = supports_punc
        self.supports_timestamps = supports_timestamps
        self.result_text = result_text
        self.result_tokens = result_tokens
        self.result_timestamps = result_timestamps
        self.calls = calls if calls is not None else []
        self.call_count = 0

    @property
    def capabilities(self) -> List[EngineCapabilities]:
        capabilities = [EngineCapabilities.ASR]
        if self.supports_punc:
            capabilities.append(EngineCapabilities.PUNC)
        if self.supports_timestamps:
            capabilities.append(EngineCapabilities.TIMESTAMPS)
        return capabilities

    def create_stream(self, hotwords=None) -> FakeRecognitionStream:
        return FakeRecognitionStream()

    def decode_stream(self, stream, context=None, **kwargs):
        self.call_count += 1
        identity = (context or "").rsplit(IDENTITY_MARKER, 1)[-1]
        task_id, socket_id = identity.split(":", 1) if ":" in identity else ("", "")
        samples = stream.samples
        sample_rate = stream.sample_rate
        start_sample = int(round(float(samples[0]) * sample_rate)) if len(samples) else 0
        call = {
            "call_number": self.call_count,
            "task_id": task_id,
            "socket_id": socket_id,
            "start_sample": start_sample,
            "sample_count": int(len(samples)),
        }
        self.calls.append(call)

        if self.call_count == self.fail_on_call:
            raise RuntimeError(f"programmed fake engine failure on call {self.call_count}")
        if self.call_count == self.delay_on_call:
            time.sleep(self.delay_seconds)
        if self.call_count == self.exit_on_call:
            os._exit(self.exit_code)

        start_ms = round(start_sample * 1000 / sample_rate)
        duration_ms = round(len(samples) * 1000 / sample_rate)
        stream.result.text = (
            self.result_text
            if self.result_text is not None
            else f"{start_ms}ms=[s={start_ms},n={duration_ms}]"
        )
        if self.result_tokens is not None:
            stream.result.tokens = list(self.result_tokens)
        if self.result_timestamps is not None:
            stream.result.timestamps = list(self.result_timestamps)
        return stream.result

    def cleanup(self):
        return None
