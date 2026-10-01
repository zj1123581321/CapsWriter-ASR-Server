"""CapsWriter ASR 协议 v2 文件客户端。"""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import math
import shutil
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

import numpy as np
import websockets


class AsrError(Exception):
    """服务端或 SDK 可识别的转录失败。"""

    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool = False,
        *,
        recovery_path=None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.recovery_path = recovery_path


@dataclass
class Transcript:
    text: str
    tokens: list[str]
    timestamps: list[float]
    duration: float
    raw: dict
    task_id: str | None = None
    is_final: bool = True
    time_start: float | None = None
    time_submit: float | None = None
    time_complete: float | None = None
    text_accu: str | None = None


_CHUNK_BYTES = 256 * 1024
_RAW_SAMPLE_RATE = 16000

# 服务端协议错误码；本地 timeout/connection_lost 等 SDK 异常不在此集合内。
PROTOCOL_ERROR_CODES = frozenset({
    "bad_request",
    "unsupported_encoding",
    "decode_failed",
    "task_conflict",
    "audio_too_long",
    "inference_failed",
    "inference_timeout",
    "overloaded",
    "slow_consumer",
    "no_backend",
    "internal",
})
_RAW_FRAME_SECONDS = 60


async def _run_process(*args: str) -> tuple[int, bytes, bytes]:
    """执行媒体工具，并在取消时杀死和回收子进程。"""
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await process.communicate()
        return process.returncode, stdout, stderr
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def _audio_duration(path: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise AsrError("decode_failed", "找不到 ffprobe，无法读取音频时长")
    try:
        code, stdout, stderr = await _run_process(
            ffprobe,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        )
    except OSError as exc:
        raise AsrError("decode_failed", f"无法启动 ffprobe: {exc}") from exc
    if code != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise AsrError("decode_failed", detail or f"ffprobe 退出码 {code}")
    try:
        duration = float(stdout.decode("ascii").strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise AsrError("decode_failed", "ffprobe 未返回有效音频时长") from exc
    if not math.isfinite(duration) or duration < 0:
        raise AsrError("decode_failed", "ffprobe 返回了无效音频时长")
    return duration


async def _transcode(path: Path, encoding: str) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise AsrError("decode_failed", "找不到 ffmpeg")
    args = [ffmpeg, "-nostdin", "-i", str(path), "-ar", "16000", "-ac", "1"]
    if encoding == "ogg_opus":
        args.extend(["-c:a", "libopus", "-b:a", "32k", "-f", "ogg"])
    else:
        args.extend(["-f", encoding])
    args.append("pipe:1")
    try:
        code, stdout, stderr = await _run_process(*args)
    except OSError as exc:
        raise AsrError("decode_failed", f"无法启动 ffmpeg: {exc}") from exc
    if code != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise AsrError("decode_failed", detail or f"ffmpeg 退出码 {code}")
    if encoding == "f32le":
        if len(stdout) % 4:
            raise AsrError("decode_failed", "ffmpeg 输出的 f32le 数据未按样本对齐")
        samples = np.frombuffer(stdout, dtype="<f4").size
        return stdout[:samples * 4]
    if encoding == "s16le":
        if len(stdout) % 2:
            raise AsrError("decode_failed", "ffmpeg 输出的 s16le 数据未按样本对齐")
        samples = np.frombuffer(stdout, dtype="<i2").size
        return stdout[:samples * 2]
    return stdout


def _health_url(url: str) -> str:
    parts = urlsplit(url)
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme)
    if scheme is None:
        raise AsrError("connection_lost", f"不支持的服务端 URL 协议: {parts.scheme}")
    path = parts.path.rstrip("/") + "/health"
    return urlunsplit((scheme, parts.netloc, path, "", ""))


def _get_health(url: str) -> tuple[int, bytes]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


async def _check_server(url: str, encoding: str, model: str | None = None) -> None:
    try:
        status, body = await asyncio.to_thread(_get_health, _health_url(url))
    except (TimeoutError, OSError, urllib.error.URLError) as exc:
        if isinstance(exc, TimeoutError):
            raise AsrError("timeout", "读取服务端 /health 超时") from exc
        if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError):
            raise AsrError("timeout", "读取服务端 /health 超时") from exc
        raise AsrError("connection_lost", f"无法读取服务端 /health: {exc}") from exc
    if status != 200:
        raise AsrError("server_too_old", f"服务端 /health 返回 HTTP {status}，需要协议 v2")
    try:
        health = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AsrError("server_too_old", "服务端 /health 不是有效 JSON") from exc
    version = health.get("protocol_version") if isinstance(health, dict) else None
    if isinstance(version, bool) or not isinstance(version, int) or version < 2:
        raise AsrError("server_too_old", "服务端协议版本低于 v2")
    encodings = health.get("encodings", [])
    if not isinstance(encodings, list) or encoding not in encodings:
        raise AsrError("unsupported_encoding", f"服务端不支持编码 {encoding}")
    if model is not None and health.get("role") == "server" and health.get("model") != model:
        raise AsrError("bad_request", f"请求模型 {model!r} 与服务端模型 {health.get('model')!r} 不符")


def _audio_frames(data: bytes, encoding: str):
    if encoding in {"f32le", "s16le"}:
        sample_bytes = 4 if encoding == "f32le" else 2
        frame_bytes = _RAW_SAMPLE_RATE * _RAW_FRAME_SECONDS * sample_bytes
    else:
        frame_bytes = _CHUNK_BYTES
    count = max(1, (len(data) + frame_bytes - 1) // frame_bytes)
    for index in range(count):
        yield data[index * frame_bytes:(index + 1) * frame_bytes], index == count - 1


def _audio_frame(
    data: bytes,
    *,
    task_id: str,
    time_start: float,
    is_final: bool,
    samples_total: int,
    encoding: str,
    seg_duration: float,
    seg_overlap: float,
    language: str | None,
    context: str | None,
    model: str | None,
) -> str:
    frame = {
        "task_id": task_id,
        "source": "file",
        "data": base64.b64encode(data).decode("ascii"),
        "is_final": is_final,
        "time_start": time_start,
        "seg_duration": seg_duration,
        "seg_overlap": seg_overlap,
        "encoding": encoding,
    }
    if is_final:
        frame["samples_total"] = samples_total
    if language is not None:
        frame["language"] = language
    if context is not None:
        frame["context"] = context
    if model is not None:
        frame["model"] = model
    return json.dumps(frame, ensure_ascii=False)


def _transcript(result: dict) -> Transcript:
    return Transcript(
        text=result["text"],
        tokens=result.get("tokens", []),
        timestamps=result.get("timestamps", []),
        duration=float(result.get("duration", 0.0)),
        raw=result,
        task_id=result.get("task_id"),
        is_final=result.get("is_final", True),
        time_start=result.get("time_start"),
        time_submit=result.get("time_submit"),
        time_complete=result.get("time_complete"),
        text_accu=result.get("text_accu"),
    )


async def _receive(ws, *, on_progress, idle_messages: asyncio.Queue) -> Transcript:
    while True:
        try:
            message = await ws.recv()
        except (OSError, websockets.exceptions.WebSocketException) as exc:
            raise AsrError("connection_lost", f"服务端关闭连接且未发送错误帧: {exc}") from exc
        if not idle_messages.full():
            idle_messages.put_nowait(None)
        result = json.loads(message)
        if result.get("type") == "error":
            raise AsrError(
                result["code"],
                result.get("message", "服务端转录失败"),
                result.get("retryable", False),
            )
        if result.get("type") == "result" and result.get("is_final", False):
            return _transcript(result)
        if result.get("type") == "result" and on_progress is not None:
            on_progress(result)


async def _transcribe_connected(
    url: str,
    data: bytes,
    *,
    encoding: str,
    samples_total: int,
    language: str | None,
    context: str | None,
    model: str | None,
    seg_duration: float,
    seg_overlap: float,
    idle_timeout: float,
    on_progress: Callable[[dict], object] | None,
) -> Transcript:
    connect_options = {"ping_interval": None, "max_size": None, "max_queue": None}
    if "proxy" in inspect.signature(websockets.connect).parameters:
        connect_options["proxy"] = None
    task_id = str(uuid.uuid4())
    time_start = time.time()
    idle_messages: asyncio.Queue = asyncio.Queue(maxsize=1)
    upload_done = asyncio.Event()

    async with websockets.connect(url, **connect_options) as ws:
        async def upload() -> None:
            for chunk, is_final in _audio_frames(data, encoding):
                frame = _audio_frame(
                    chunk,
                    task_id=task_id,
                    time_start=time_start,
                    is_final=is_final,
                    samples_total=samples_total,
                    encoding=encoding,
                    seg_duration=seg_duration,
                    seg_overlap=seg_overlap,
                    language=language,
                    context=context,
                    model=model,
                )
                try:
                    await asyncio.wait_for(ws.send(frame), timeout=idle_timeout)
                except TimeoutError as exc:
                    raise AsrError("timeout", "发送音频帧超过 idle_timeout") from exc
            upload_done.set()

        async def idle_watch() -> None:
            await upload_done.wait()
            while True:
                try:
                    await asyncio.wait_for(idle_messages.get(), timeout=idle_timeout)
                except TimeoutError as exc:
                    raise AsrError("timeout", "上传结束后等待服务端消息超时") from exc

        upload_task = asyncio.create_task(upload())
        receive_task = asyncio.create_task(
            _receive(ws, on_progress=on_progress, idle_messages=idle_messages)
        )
        idle_task = asyncio.create_task(idle_watch())
        tasks = {upload_task, receive_task, idle_task}
        try:
            while True:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    result = task.result()
                    tasks.remove(task)
                    if task is receive_task:
                        return result
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def _operation(
    path: Path,
    url: str,
    *,
    encoding: str,
    language: str | None,
    context: str | None,
    model: str | None,
    seg_duration: float,
    seg_overlap: float,
    idle_timeout: float,
    on_progress: Callable[[dict], object] | None,
    set_deadline,
) -> Transcript:
    duration = await _audio_duration(path)
    set_deadline(max(120.0, duration + 60.0))
    await _check_server(url, encoding, model)
    audio = await _transcode(path, encoding)
    if encoding == "f32le":
        samples_total = np.frombuffer(audio, dtype="<f4").size
        duration = samples_total / _RAW_SAMPLE_RATE
    elif encoding == "s16le":
        samples_total = np.frombuffer(audio, dtype="<i2").size
        duration = samples_total / _RAW_SAMPLE_RATE
    else:
        samples_total = round(duration * _RAW_SAMPLE_RATE)
    try:
        return await _transcribe_connected(
            url,
            audio,
            encoding=encoding,
            samples_total=samples_total,
            language=language,
            context=context,
            model=model,
            seg_duration=seg_duration,
            seg_overlap=seg_overlap,
            idle_timeout=idle_timeout,
            on_progress=on_progress,
        )
    except (OSError, websockets.exceptions.WebSocketException) as exc:
        raise AsrError("connection_lost", f"无法连接服务端 WebSocket: {exc}") from exc


async def transcribe_file(
    path,
    url,
    *,
    encoding="flac",
    language=None,
    context=None,
    seg_duration=15.0,
    seg_overlap=2.0,
    deadline_total=None,
    idle_timeout=300.0,
    on_progress=None,
    model=None,
) -> Transcript:
    """转录音频文件；不会降级或自动重试。"""
    started = time.monotonic()
    deadline = {"at": started + (120.0 if deadline_total is None else deadline_total)}
    deadline_changed = asyncio.Event()

    def set_deadline(seconds: float) -> None:
        if deadline_total is None:
            deadline["at"] = started + seconds
            deadline_changed.set()

    async def operation() -> Transcript:
        return await _operation(
            Path(path),
            url,
            encoding=encoding,
            language=language,
            context=context,
            seg_duration=seg_duration,
            seg_overlap=seg_overlap,
            idle_timeout=idle_timeout,
            on_progress=on_progress,
            model=model,
            set_deadline=set_deadline,
        )

    async def deadline_watch() -> None:
        while True:
            remaining = deadline["at"] - time.monotonic()
            if remaining <= 0:
                raise AsrError("timeout", "转录超过 deadline_total")
            try:
                await asyncio.wait_for(deadline_changed.wait(), timeout=remaining)
            except TimeoutError:
                if deadline["at"] <= time.monotonic():
                    raise AsrError("timeout", "转录超过 deadline_total")
            else:
                deadline_changed.clear()

    operation_task = asyncio.create_task(operation())
    timer_task = asyncio.create_task(deadline_watch())
    try:
        done, _ = await asyncio.wait(
            {operation_task, timer_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if timer_task in done:
            timer_task.result()
        if time.monotonic() >= deadline["at"]:
            raise AsrError("timeout", "转录超过 deadline_total")
        return operation_task.result()
    finally:
        for task in (operation_task, timer_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(operation_task, timer_task, return_exceptions=True)


def transcribe_file_sync(
    path,
    url,
    *,
    encoding="flac",
    language=None,
    context=None,
    seg_duration=15.0,
    seg_overlap=2.0,
    deadline_total=None,
    idle_timeout=300.0,
    on_progress=None,
    model=None,
) -> Transcript:
    """同步入口，适用于非 asyncio 调用方。"""
    return asyncio.run(
        transcribe_file(
            path,
            url,
            encoding=encoding,
            language=language,
            context=context,
            seg_duration=seg_duration,
            seg_overlap=seg_overlap,
            deadline_total=deadline_total,
            idle_timeout=idle_timeout,
            on_progress=on_progress,
            model=model,
        )
    )
