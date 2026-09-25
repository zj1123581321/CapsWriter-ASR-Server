# coding: utf-8
"""服务端分段参数、末帧 flush 与引擎段长契约。"""
from __future__ import annotations

import asyncio
import base64
import queue
import time
import uuid

import numpy as np
import pytest
import websockets

from config_server import ServerConfig
from core.protocol import AudioMessage
from core.server.connection import ws_recv
from core.server.connection.ws_recv import AudioCache
from tests.harness.client import collect_terminal, transcribe
from tests.harness.server import ManagedFakeServerHarness


def make_message(task_id="task", *, duration=5.0, overlap=0.0, is_final=False):
    return AudioMessage(
        task_id=task_id,
        source="file",
        data="",
        is_final=is_final,
        time_start=time.time(),
        seg_duration=duration,
        seg_overlap=overlap,
    )


@pytest.mark.parametrize(
    ("duration", "overlap", "expected"),
    [
        (0.0, 0.0, "seg_duration=0"),
        (4.0, 0.0, "seg_duration=4"),
        (5.0, -1.0, "seg_overlap=-1"),
        (5.0, 2.5, "seg_overlap=2.5"),
        (79.0, 2.0, "seg_duration=79"),
    ],
)
def test_rejects_segmentation_values_outside_allowed_range(
    monkeypatch, duration, overlap, expected
):
    monkeypatch.setattr(ServerConfig, "model_type", "qwen_asr")
    monkeypatch.setattr(ServerConfig, "seg_cut_snap", False)
    with pytest.raises(ValueError, match=expected):
        ws_recv._validate_segmentation(
            make_message(duration=duration, overlap=overlap), AudioCache()
        )


def test_snap_validation_accounts_for_maximum_cut_and_search_after(monkeypatch):
    monkeypatch.setattr(ServerConfig, "model_type", "qwen_asr")
    monkeypatch.setattr(ServerConfig, "seg_cut_snap", True)
    cache = AudioCache()
    with pytest.raises(ValueError, match="seg_max_cut=72.*83s.*80s"):
        ws_recv._validate_segmentation(make_message(duration=70, overlap=8), cache)

    ws_recv._validate_segmentation(make_message(duration=70, overlap=4), cache)


def test_task_cannot_change_segmentation_parameters_after_first_frame(monkeypatch):
    monkeypatch.setattr(ServerConfig, "model_type", "qwen_asr")
    cache = AudioCache()
    ws_recv._validate_segmentation(make_message(duration=10, overlap=1), cache)
    with pytest.raises(ValueError, match="seg_duration=11.*首帧值 10"):
        ws_recv._validate_segmentation(make_message(duration=11, overlap=1), cache)


@pytest.mark.asyncio
async def test_final_file_cut_does_not_wait_for_more_audio(monkeypatch):
    class Finder:
        def find(self, chunks, lo, hi, nominal):
            assert hi == 72.0
            return 72.0, False

    monkeypatch.setattr(ServerConfig, "seg_cut_snap", True)
    monkeypatch.setattr(ws_recv, "_engine_segment_limit", lambda: 80.0)
    monkeypatch.setattr(ws_recv, "get_cut_finder", Finder)
    cache = AudioCache()
    cache.chunks = bytes(100 * 16000 * 4)
    submitted = queue.Queue()
    await ws_recv._submit_segments(
        make_message(duration=15, overlap=2),
        cache,
        submitted,
        "socket",
        is_final=True,
    )

    segment = submitted.get_nowait()
    assert segment.is_final is False
    assert len(segment.data) == 74 * 16000 * 4
    assert cache.duration == 28.0


async def _send_bad_frame(server_url, *, duration, overlap, task_id=None):
    task_id = task_id or str(uuid.uuid4())
    samples = np.zeros(int(0.5 * 16000), dtype=np.float32)
    frame = AudioMessage(
        task_id=task_id,
        source="file",
        data=base64.b64encode(samples.tobytes()).decode("ascii"),
        is_final=False,
        time_start=time.time(),
        seg_duration=duration,
        seg_overlap=overlap,
    )
    async with websockets.connect(server_url, max_size=None, ping_interval=None) as websocket:
        await websocket.send(frame.to_json())
        messages, closed = await collect_terminal(websocket, task_id=task_id, timeout=5)
        assert not closed
        assert messages[0]["type"] == "error"
        assert messages[0]["code"] == "bad_request"
        assert messages[0]["task_id"] == task_id
        assert messages[0]["message"]


@pytest.mark.asyncio
async def test_bad_parameters_fail_fast_and_other_connection_completes(monkeypatch):
    monkeypatch.setattr(ServerConfig, "model_type", "qwen_asr")
    server = await ManagedFakeServerHarness.start()
    try:
        invalid = asyncio.create_task(
            _send_bad_frame(server.url, duration=0.0, overlap=0.0)
        )
        healthy = asyncio.create_task(
            transcribe(
                server.url,
                np.zeros(16000, dtype=np.float32),
                task_id="healthy-task",
                seg_duration=5.0,
                seg_overlap=0.0,
                chunk_seconds=5.0,
            )
        )
        _, healthy_messages = await asyncio.gather(invalid, healthy)
        assert healthy_messages[-1]["is_final"] is True

        for duration, overlap in (
            (4.0, 0.0),
            (5.0, -1.0),
            (5.0, 2.5),
            (79.0, 2.0),
        ):
            await _send_bad_frame(server.url, duration=duration, overlap=overlap)

        task_id = str(uuid.uuid4())
        async with websockets.connect(server.url, max_size=None, ping_interval=None) as websocket:
            first = AudioMessage(
                task_id=task_id,
                source="file",
                data="",
                is_final=False,
                time_start=time.time(),
                seg_duration=5.0,
                seg_overlap=0.0,
            )
            changed = AudioMessage(
                task_id=task_id,
                source="file",
                data="",
                is_final=True,
                time_start=time.time(),
                seg_duration=6.0,
                seg_overlap=0.0,
            )
            await websocket.send(first.to_json())
            await websocket.send(changed.to_json())
            messages, closed = await collect_terminal(websocket, task_id=task_id, timeout=5)
            assert not closed
            assert messages[0]["code"] == "bad_request"
            assert "seg_duration=6" in messages[0]["message"]
    finally:
        await server.stop()
