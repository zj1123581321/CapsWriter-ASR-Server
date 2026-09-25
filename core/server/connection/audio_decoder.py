"""协议 v2 上行音频解码器。"""

import asyncio
import shutil

import numpy as np

_CANCEL_TIMEOUT_SECONDS = 5.0


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
    _COMPRESSED_ENCODINGS = {"flac": "flac", "ogg_opus": "ogg"}

    def __init__(self, encoding: str):
        if encoding not in self._RAW_ENCODINGS | self._COMPRESSED_ENCODINGS.keys():
            raise AudioDecodeError("unsupported_encoding", f"不支持音频编码：{encoding}")
        if encoding in self._COMPRESSED_ENCODINGS and shutil.which("ffmpeg") is None:
            raise AudioDecodeError("unsupported_encoding", "本机 PATH 中没有 ffmpeg")
        self.encoding = encoding
        self.samples_emitted = 0
        self.process: asyncio.subprocess.Process | None = None
        self._output: asyncio.Queue = asyncio.Queue(maxsize=2)
        self._read_permits: asyncio.Queue = asyncio.Queue()
        self._input: asyncio.Queue | None = None
        self._writer_task: asyncio.Task | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_tail = bytearray()
        self._reader_error: Exception | None = None
        self._finish_error: AudioDecodeError | None = None
        self._input_finished = False
        self._finished = False
        self._iterated = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        if not self._finished:
            await self.cancel()

    async def feed(self, data: bytes) -> None:
        if self._input_finished:
            raise RuntimeError("音频解码器已结束输入")
        if self.encoding in self._RAW_ENCODINGS:
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
            return

        await self._start_ffmpeg()
        try:
            await self._put_input(data)
        except asyncio.CancelledError:
            await self.cancel()
            raise
        except Exception as exc:
            await self.cancel()
            raise self._ffmpeg_error(f"写入 ffmpeg 输入失败：{exc}") from exc

    async def _start_ffmpeg(self) -> None:
        if self.process is not None:
            return
        demuxer = self._COMPRESSED_ENCODINGS[self.encoding]
        try:
            self.process = await asyncio.create_subprocess_exec(
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-f", demuxer, "-i", "pipe:0", "-ar", "16000", "-ac", "1",
                "-f", "f32le", "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise AudioDecodeError("unsupported_encoding", "本机 PATH 中没有 ffmpeg") from exc
        self._input = asyncio.Queue(maxsize=2)
        self._stderr_task = asyncio.create_task(self._read_stderr())
        self._writer_task = asyncio.create_task(self._write_stdin())
        self._reader_task = asyncio.create_task(self._read_stdout())

    async def _put_input(self, item: bytes | object) -> None:
        put_task = asyncio.create_task(self._input.put(item))
        try:
            done, _ = await asyncio.wait(
                (put_task, self._writer_task), return_when=asyncio.FIRST_COMPLETED
            )
        except BaseException:
            put_task.cancel()
            await asyncio.gather(put_task, return_exceptions=True)
            raise
        if self._writer_task in done and not put_task.done():
            put_task.cancel()
            await asyncio.gather(put_task, return_exceptions=True)
            await self._writer_task
            raise RuntimeError("ffmpeg 输入协程已提前结束")
        await put_task

    async def _write_stdin(self) -> None:
        while True:
            item = await self._input.get()
            if item is self._END:
                self.process.stdin.close()
                await self.process.stdin.wait_closed()
                return
            self.process.stdin.write(item)
            await self.process.stdin.drain()

    async def _read_stdout(self) -> None:
        try:
            pending = bytearray()
            eof = False
            while True:
                await self._read_permits.get()
                while len(pending) < 4:
                    data = await self.process.stdout.read(65536)
                    if not data:
                        eof = True
                        break
                    pending.extend(data)
                aligned_length = len(pending) // 4 * 4
                if aligned_length:
                    await self._output.put(bytes(pending[:aligned_length]))
                    del pending[:aligned_length]
                    continue
                if eof:
                    break
            returncode = await self.process.wait()
            await self._stderr_task
            if pending:
                self._reader_error = AudioDecodeError(
                    "decode_failed", "ffmpeg 输出的 f32le 数据未按 4 字节对齐"
                )
            if returncode != 0:
                self._reader_error = self._ffmpeg_error()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._reader_error = exc
        await self._output.put(self._END)

    async def _read_stderr(self) -> None:
        while data := await self.process.stderr.read(4096):
            self._stderr_tail.extend(data)
            del self._stderr_tail[:-500]

    def _ffmpeg_error(self, message: str | None = None) -> AudioDecodeError:
        tail = bytes(self._stderr_tail).decode("utf-8", errors="replace")
        detail = f"；ffmpeg stderr 末尾：{tail}" if tail else ""
        prefix = message or f"ffmpeg 解码失败，退出码 {self.process.returncode}"
        return AudioDecodeError("decode_failed", f"{prefix}{detail}")

    async def pcm_chunks(self):
        if self._iterated:
            raise RuntimeError("PCM 迭代器只能消费一次")
        self._iterated = True
        while True:
            if self.encoding in self._COMPRESSED_ENCODINGS:
                await self._read_permits.put(None)
            item = await self._output.get()
            if item is self._END:
                return
            if isinstance(item, bytes):
                item = np.frombuffer(item, dtype="<f4")
            self.samples_emitted += int(item.size)
            yield item

    async def finish(self) -> None:
        if self._input_finished:
            if self._finish_error:
                raise self._finish_error
            return
        self._input_finished = True
        if self.encoding in self._RAW_ENCODINGS or self.process is None:
            await self._output.put(self._END)
            self._finished = True
            return
        try:
            await self._put_input(self._END)
            await self._writer_task
            await self._reader_task
            if self._reader_error:
                raise self._reader_error
        except asyncio.CancelledError:
            await self.cancel()
            raise
        except AudioDecodeError as exc:
            self._finish_error = exc
            await self.cancel()
            raise
        except Exception as exc:
            await self.cancel()
            self._finish_error = self._ffmpeg_error(f"结束 ffmpeg 输入失败：{exc}")
            raise self._finish_error from exc
        self._finished = True

    async def cancel(self) -> None:
        self._input_finished = True
        if self.process is None:
            return
        if not self.process.stdin.is_closing():
            self.process.stdin.close()
        if self.process.returncode is None:
            if self._writer_task and not self._writer_task.done():
                self._writer_task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.shield(self.process.wait()), timeout=_CANCEL_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        for task in (self._writer_task, self._reader_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (self._writer_task, self._reader_task, self._stderr_task) if task),
            return_exceptions=True,
        )


def available_encodings() -> list[str]:
    """返回当前可用的编码。"""
    encodings = ["f32le", "s16le"]
    if shutil.which("ffmpeg") is not None:
        encodings.extend(("flac", "ogg_opus"))
    return encodings
