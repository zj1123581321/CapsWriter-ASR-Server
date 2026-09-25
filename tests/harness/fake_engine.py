# coding: utf-8
"""无模型文件的可编程 ASR 引擎，供真实 TaskPipeline 子进程使用。"""
from __future__ import annotations

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
        supports_punc: bool = False,
        supports_timestamps: bool = False,
        calls=None,
    ):
        super().__init__(config)
        self.fail_on_call = fail_on_call
        self.delay_on_call = delay_on_call
        self.delay_seconds = delay_seconds
        self.supports_punc = supports_punc
        self.supports_timestamps = supports_timestamps
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

        start_ms = round(start_sample * 1000 / sample_rate)
        duration_ms = round(len(samples) * 1000 / sample_rate)
        stream.result.text = f"{start_ms}ms=[s={start_ms},n={duration_ms}]"
        return stream.result

    def cleanup(self):
        return None
