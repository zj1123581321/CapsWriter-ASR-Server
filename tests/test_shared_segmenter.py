# coding: utf-8
"""E1 共享 PCM 分段与 WebSocket producer 契约验收。"""

import queue

import pytest

from core.constants import AudioFormat
from core.protocol import AudioMessage
from core.server.connection import ws_recv
from core.server.connection.ws_recv import AudioCache, _submit_segments
from core.server.segmenter import PcmSegmenter


def test_ws_receiver_uses_shared_pcm_segmenter():
    assert hasattr(ws_recv, "PcmSegmenter")


def _configured_segmenter():
    segmenter = PcmSegmenter()
    segmenter.configure(
        cut_finder=None,
        engine_segment_limit=80.0,
        cut_snap=False,
        search_before=5.0,
        search_after=5.0,
        max_cut=72.0,
    )
    return segmenter


@pytest.mark.asyncio
async def test_shared_segmenter_preserves_bounded_offsets_and_final():
    segmenter = _configured_segmenter()
    first = b"A" * (8 * AudioFormat.BYTES_PER_SECOND)
    segmenter.append(first)

    ready = await segmenter.drain_ready(
        source="file",
        nominal=5.0,
        overlap=1.0,
    )
    assert len(ready) == 1
    assert ready[0].data == first[:6 * AudioFormat.BYTES_PER_SECOND]
    assert ready[0].offset == 0.0
    assert ready[0].overlap == 1.0
    assert ready[0].is_final is False
    assert len(segmenter.chunks) == 3 * AudioFormat.BYTES_PER_SECOND

    final_ready = await segmenter.drain_ready(
        source="file",
        nominal=5.0,
        overlap=1.0,
        is_final=True,
    )
    assert final_ready == []
    final = segmenter.final_segment(1.0)
    assert final.data == first[5 * AudioFormat.BYTES_PER_SECOND:]
    assert final.offset == 5.0
    assert final.is_final is True


@pytest.mark.asyncio
async def test_real_ws_producer_emits_actual_task_payload_from_shared_segmenter(
    monkeypatch,
):
    monkeypatch.setattr(ws_recv.Config, "seg_cut_snap", False)
    original = b"".join(
        bytes((index % 251,)) * AudioFormat.BYTES_PER_SECOND
        for index in range(8)
    )
    cache = AudioCache()
    cache.chunks = original
    cache.byte_count = len(original)
    message = AudioMessage(
        task_id="ws-job",
        source="file",
        data="",
        is_final=False,
        time_start=11.0,
        seg_duration=5.0,
        seg_overlap=1.0,
    )
    incoming = queue.Queue()

    assert await _submit_segments(message, cache, incoming, "socket-1")

    assert incoming.qsize() == 1
    task = incoming.get_nowait()
    assert task.data == original[:6 * AudioFormat.BYTES_PER_SECOND]
    assert task.offset == 0.0
    assert task.overlap == 1.0
    assert task.owner_kind == "ws"
    assert task.socket_id == "socket-1"
    assert task.task_id == "ws-job"
    assert task.is_final is False
    assert len(cache.chunks) == 3 * AudioFormat.BYTES_PER_SECOND
