# coding: utf-8
"""按当前 v1 协议发送 float32 音频帧并收集服务端结果。"""
from __future__ import annotations

import asyncio
import base64
import json
import time

import numpy as np
import websockets

from core.protocol import AudioMessage


async def send_audio(
    websocket,
    samples: np.ndarray,
    *,
    task_id: str,
    source: str = "mic",
    seg_duration: float = 2.0,
    seg_overlap: float = 0.25,
    chunk_seconds: float = 0.5,
    final_frame_seconds: float = 0.0,
    finalize: bool = True,
    context: str = "",
):
    sample_rate = 16000
    common = {
        "task_id": task_id,
        "source": source,
        "time_start": time.time(),
        "seg_duration": seg_duration,
        "seg_overlap": seg_overlap,
        "context": context,
        "language": "auto",
    }
    final_count = min(len(samples), round(final_frame_seconds * sample_rate))
    prefix_end = len(samples) - final_count
    frame_samples = max(1, round(chunk_seconds * sample_rate))

    for start in range(0, prefix_end, frame_samples):
        frame = samples[start:min(start + frame_samples, prefix_end)]
        await websocket.send(AudioMessage(
            data=base64.b64encode(frame.astype("<f4", copy=False).tobytes()).decode("ascii"),
            is_final=False,
            **common,
        ).to_json())

    if not finalize:
        return
    if final_count:
        final_data = samples[prefix_end:]
        encoded = base64.b64encode(final_data.astype("<f4", copy=False).tobytes()).decode("ascii")
    else:
        encoded = ""
    await websocket.send(AudioMessage(data=encoded, is_final=True, **common).to_json())


async def collect_terminal(websocket, *, task_id: str, timeout: float = 30.0):
    messages = []
    try:
        async with asyncio.timeout(timeout):
            while True:
                message = json.loads(await websocket.recv())
                messages.append(message)
                if message.get("task_id") == task_id and (
                    message.get("is_final") or message.get("type") == "error"
                ):
                    return messages, False
    except TimeoutError as exc:
        raise AssertionError(
            f"等待任务 {task_id} 终态超时 ({timeout}s)，已收到消息: {messages!r}"
        ) from exc
    except websockets.ConnectionClosed:
        return messages, True


async def transcribe(
    server: str,
    samples: np.ndarray,
    *,
    task_id: str,
    timeout: float = 30.0,
    **send_options,
):
    messages = []
    try:
        async with asyncio.timeout(timeout):
            async with websockets.connect(
                server, max_size=None, ping_interval=None, open_timeout=timeout
            ) as websocket:
                await send_audio(websocket, samples, task_id=task_id, **send_options)
                messages, closed = await collect_terminal(
                    websocket, task_id=task_id, timeout=timeout
                )
                if closed or not messages or not messages[-1].get("is_final"):
                    raise AssertionError(
                        f"任务 {task_id} 未收到 is_final，已收到消息: {messages!r}"
                    )
                return messages
    except TimeoutError as exc:
        raise AssertionError(
            f"任务 {task_id} 整体等待超时 ({timeout}s)，已收到消息: {messages!r}"
        ) from exc
