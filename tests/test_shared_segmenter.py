# coding: utf-8
"""E1 共享 PCM 分段与 WebSocket producer 契约验收。"""

import queue

import numpy as np
import pytest

from core.constants import AudioFormat
from core.protocol import AudioMessage
from core.server.connection import ws_recv
from core.server.connection.ws_recv import AudioCache, _submit_segments, _validate_segmentation
from core.server.schema import Result
from core.server import segmenter as shared_segmenter
from core.server.segmenter import PcmSegmenter
from core.server.worker.audio import process_audio_task


def _sample_quantized_bytes(seconds: float) -> int:
    """旧 WS producer：先量化采样点，再换算 float32 字节。"""
    return round(seconds * AudioFormat.SAMPLE_RATE) * AudioFormat.BYTES_PER_SAMPLE


def _pcm(n_samples: int) -> bytes:
    return np.arange(n_samples, dtype="<f4").tobytes()


class _FrameAlignedFinder:
    def find(self, chunks, lo, hi, nominal):
        return 5.12, True


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


def _produce_validated_segment(
    monkeypatch,
    *,
    snap,
    duration,
    overlap,
    n_samples,
    finder=None,
    is_final=False,
    task_id="ws-job",
):
    monkeypatch.setattr(ws_recv.Config, "seg_cut_snap", snap)
    if finder is not None:
        monkeypatch.setattr(ws_recv, "get_cut_finder", finder)
    original = _pcm(n_samples)
    cache = AudioCache()
    cache.chunks = original
    cache.byte_count = len(original)
    message = AudioMessage(
        task_id=task_id,
        source="file",
        data="",
        is_final=is_final,
        time_start=11.0,
        seg_duration=duration,
        seg_overlap=overlap,
    )
    _validate_segmentation(message, cache)
    incoming = queue.Queue()
    return message, cache, original, incoming


@pytest.mark.asyncio
async def test_fixed_cut_quantizes_decimal_duration_to_complete_float32_samples(
    monkeypatch,
):
    duration = 5.00001
    overlap = 0.5
    message, cache, original, incoming = _produce_validated_segment(
        monkeypatch,
        snap=False,
        duration=duration,
        overlap=overlap,
        n_samples=7 * AudioFormat.SAMPLE_RATE,
        task_id="fixed-decimal-duration",
    )

    assert await _submit_segments(message, cache, incoming, "socket-1")

    stride_bytes = _sample_quantized_bytes(duration)
    overlap_bytes = _sample_quantized_bytes(overlap)
    expected = original[: stride_bytes + overlap_bytes]
    assert incoming.qsize() == 1
    task = incoming.get_nowait()
    assert len(task.data) == 352000
    assert len(task.data) % AudioFormat.BYTES_PER_SAMPLE == 0
    assert task.data == expected
    assert task.offset == 0.0
    assert task.overlap == overlap
    assert task.is_final is False
    samples = process_audio_task(
        task, Result(task_id=task.task_id, socket_id=task.socket_id, type=task.type)
    )
    assert samples is not None
    assert samples.dtype == np.float32
    assert samples.tobytes() == expected
    assert cache.offset == stride_bytes / AudioFormat.BYTES_PER_SECOND
    assert len(cache.chunks) == len(original) - stride_bytes
    assert len(cache.chunks) % AudioFormat.BYTES_PER_SAMPLE == 0


@pytest.mark.asyncio
async def test_fixed_cut_quantizes_decimal_overlap_to_complete_float32_samples(
    monkeypatch,
):
    duration = 5.0
    overlap = 0.50001
    message, cache, original, incoming = _produce_validated_segment(
        monkeypatch,
        snap=False,
        duration=duration,
        overlap=overlap,
        n_samples=7 * AudioFormat.SAMPLE_RATE,
        task_id="fixed-decimal-overlap",
    )

    assert await _submit_segments(message, cache, incoming, "socket-1")

    stride_bytes = _sample_quantized_bytes(duration)
    overlap_bytes = _sample_quantized_bytes(overlap)
    expected = original[: stride_bytes + overlap_bytes]
    task = incoming.get_nowait()
    assert task.data == expected
    assert task.offset == 0.0
    assert task.is_final is False
    assert len(task.data) % AudioFormat.BYTES_PER_SAMPLE == 0
    samples = process_audio_task(
        task, Result(task_id=task.task_id, socket_id=task.socket_id, type=task.type)
    )
    assert samples.tobytes() == expected
    assert cache.offset == stride_bytes / AudioFormat.BYTES_PER_SECOND
    assert len(cache.chunks) == len(original) - stride_bytes


@pytest.mark.asyncio
async def test_snap_cut_quantizes_decimal_overlap_at_frame_boundary(
    monkeypatch,
):
    duration = 5.0
    overlap = 0.50001
    cut = 5.12
    message, cache, original, incoming = _produce_validated_segment(
        monkeypatch,
        snap=True,
        duration=duration,
        overlap=overlap,
        n_samples=12 * AudioFormat.SAMPLE_RATE,
        finder=_FrameAlignedFinder,
        task_id="snap-decimal-overlap",
    )

    assert await _submit_segments(message, cache, incoming, "socket-1")

    stride_bytes = _sample_quantized_bytes(cut)
    overlap_bytes = _sample_quantized_bytes(overlap)
    expected = original[: stride_bytes + overlap_bytes]
    assert incoming.qsize() == 1
    task = incoming.get_nowait()
    assert len(task.data) == 359680
    assert len(task.data) % AudioFormat.BYTES_PER_SAMPLE == 0
    assert task.data == expected
    assert task.offset == 0.0
    assert task.overlap == overlap
    assert task.is_final is False
    samples = process_audio_task(
        task, Result(task_id=task.task_id, socket_id=task.socket_id, type=task.type)
    )
    assert samples.tobytes() == expected
    assert cache.offset == stride_bytes / AudioFormat.BYTES_PER_SECOND
    assert len(cache.chunks) == len(original) - stride_bytes
    assert len(cache.chunks) % AudioFormat.BYTES_PER_SAMPLE == 0


@pytest.mark.asyncio
async def test_final_segment_keeps_complete_samples_after_quantized_stride(
    monkeypatch,
):
    duration = 5.00001
    overlap = 0.5
    message, cache, original, incoming = _produce_validated_segment(
        monkeypatch,
        snap=False,
        duration=duration,
        overlap=overlap,
        n_samples=7 * AudioFormat.SAMPLE_RATE,
        task_id="final-after-decimal",
    )

    assert await _submit_segments(message, cache, incoming, "socket-1")
    stride_bytes = _sample_quantized_bytes(duration)
    remaining = original[stride_bytes:]
    assert incoming.qsize() == 1
    regular = incoming.get_nowait()
    assert regular.is_final is False
    assert regular.data == original[: stride_bytes + _sample_quantized_bytes(overlap)]

    assert await _submit_segments(
        message, cache, incoming, "socket-1", is_final=True
    )
    assert incoming.empty()
    final = cache.final_segment(overlap)
    assert final.is_final is True
    assert final.data == remaining
    assert len(final.data) % AudioFormat.BYTES_PER_SAMPLE == 0
    assert final.offset == stride_bytes / AudioFormat.BYTES_PER_SECOND
    final_task = type(regular)(
        type=regular.type,
        data=final.data,
        offset=final.offset,
        overlap=final.overlap,
        task_id=regular.task_id,
        socket_id=regular.socket_id,
        is_final=True,
        time_start=regular.time_start,
        time_submit=regular.time_submit,
        owner_kind=regular.owner_kind,
    )
    samples = process_audio_task(
        final_task,
        Result(task_id=final_task.task_id, socket_id=final_task.socket_id, type=final_task.type),
    )
    assert samples.tobytes() == remaining


@pytest.mark.parametrize(
    ("snap", "nominal", "overlap", "expected"),
    [
        (False, 4.999, 0.0, "seg_duration=4.999"),
        (False, 5.0, -0.5, "seg_overlap=-0.5"),
        (False, 5.0, 2.5, "seg_overlap=2.5"),
        (False, 79.0, 2.0, "单段最长 81s"),
        (True, 70.0, 8.0, "单段最长 83s"),
    ],
)
def test_shared_segment_params_rule_is_connection_free(
    monkeypatch, snap, nominal, overlap, expected
):
    """共享无状态规则独立于 WS 缓存/连接，直接按 Config 判定。"""
    monkeypatch.setattr(shared_segmenter.Config, "model_type", "qwen_asr")
    monkeypatch.setattr(shared_segmenter.Config, "seg_cut_snap", snap)
    with pytest.raises(ValueError, match=expected):
        shared_segmenter.validate_segment_params(nominal, overlap)


def test_shared_segment_params_rule_accepts_decimal_and_snap_budget(monkeypatch):
    monkeypatch.setattr(shared_segmenter.Config, "model_type", "qwen_asr")
    monkeypatch.setattr(shared_segmenter.Config, "seg_cut_snap", True)
    shared_segmenter.validate_segment_params(70.0, 5.0)
    shared_segmenter.validate_segment_params(5.00001, 0.50001)
