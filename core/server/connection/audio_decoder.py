"""协议 v2 上行音频解码器。"""

import asyncio

import numpy as np


class AudioDecodeError(Exception):
    """携带协议错误码的音频解码错误。"""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class AudioDecoder:
    """把一个任务的音频块输出为 float32 PCM。"""

    _END = object()
    _RAW_ENCODINGS = {"f32le", "s16le"}

    def __init__(self, encoding: str):
        if encoding not in self._RAW_ENCODINGS:
            raise AudioDecodeError("unsupported_encoding", f"不支持音频编码：{encoding}")
        self.encoding = encoding
        self.samples_emitted = 0
        self._output: asyncio.Queue = asyncio.Queue(maxsize=2)
        self._input_finished = False
        self._finished = False
        self._cancelled = False
        self._iterated = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        if not self._finished:
            await self.cancel()

    async def feed(self, data: bytes) -> None:
        if self._input_finished:
            raise RuntimeError("音频解码器已结束输入")
        if self.encoding == "f32le":
            if len(data) % 4:
                raise AudioDecodeError("decode_failed", "f32le 数据长度必须是 4 的倍数")
            pcm = np.frombuffer(data, dtype="<f4")
        else:
            if len(data) % 2:
                raise AudioDecodeError("decode_failed", "s16le 数据长度必须是 2 的倍数")
            pcm = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
        if pcm.size:
            await self._output.put(pcm)

    async def pcm_chunks(self):
        if self._iterated:
            raise RuntimeError("PCM 迭代器只能消费一次")
        self._iterated = True
        while True:
            item = await self._output.get()
            if item is self._END:
                return
            self.samples_emitted += int(item.size)
            yield item

    async def finish(self) -> None:
        if self._input_finished:
            return
        self._input_finished = True
        await self._output.put(self._END)
        self._finished = True

    async def cancel(self) -> None:
        self._cancelled = True
        self._input_finished = True


def available_encodings() -> list[str]:
    """返回当前可用的编码。"""
    return ["f32le", "s16le"]
