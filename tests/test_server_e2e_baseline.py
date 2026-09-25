# coding: utf-8
"""真实 WebSocket/TaskHandler 子进程端到端骨架。"""
from __future__ import annotations

import asyncio
import re
import uuid

import numpy as np
import pytest
import websockets

from core.protocol import AudioMessage
from tests.harness.client import collect_terminal, send_audio, transcribe

SAMPLE_RATE = 16000
SPAN_PATTERN = re.compile(r"\[s=(\d+),n=(\d+)\]")


def make_encoded_audio(seconds: float, start_sample: int = 0) -> np.ndarray:
    count = round(seconds * SAMPLE_RATE)
    return np.arange(start_sample, start_sample + count, dtype=np.float32) / SAMPLE_RATE


def decode_spans(text: str) -> list[tuple[int, int]]:
    return [
        (int(start_ms), int(start_ms) + int(duration_ms))
        for start_ms, duration_ms in SPAN_PATTERN.findall(text)
    ]


def assert_gapless(spans: list[tuple[int, int]], end_ms: int, calls=()):
    assert spans, "假引擎编码区间缺失"
    cursor = 0
    for start_ms, end_span_ms in sorted(spans):
        assert start_ms <= cursor, (
            f"音频区间有空洞：游标={cursor}ms，下段={start_ms}..{end_span_ms}ms；"
            f"假引擎调用={list(calls)!r}"
        )
        cursor = max(cursor, end_span_ms)
    assert cursor >= end_ms, f"识别区间末端 {cursor}ms 未覆盖目标 {end_ms}ms"


@pytest.mark.asyncio
async def test_twenty_seconds_are_covered_without_gaps(fake_asr_server):
    task_id = str(uuid.uuid4())
    messages = await transcribe(
        fake_asr_server.url,
        make_encoded_audio(20),
        task_id=task_id,
        seg_duration=2.0,
        seg_overlap=0.25,
        chunk_seconds=0.5,
    )
    final = messages[-1]
    assert final["is_final"] is True
    assert final["task_id"] == task_id
    assert_gapless(decode_spans(final["text"]), 20_000, fake_asr_server.calls)


@pytest.mark.asyncio
async def test_three_connections_keep_results_separate(fake_asr_server):
    jobs = [
        (str(uuid.uuid4()), start, make_encoded_audio(1.0, start))
        for start in (0, 480_000, 960_000)
    ]
    all_messages = await asyncio.gather(*(
        transcribe(fake_asr_server.url, audio, task_id=task_id)
        for task_id, _, audio in jobs
    ))

    socket_by_task = {
        item["task_id"]: item["socket_id"] for item in list(fake_asr_server.observed)
    }
    assert set(socket_by_task) == {task_id for task_id, _, _ in jobs}
    assert len(set(socket_by_task.values())) == 3
    for (task_id, start, audio), messages in zip(jobs, all_messages):
        final = messages[-1]
        assert final["task_id"] == task_id and final["is_final"] is True
        spans = decode_spans(final["text"])
        assert spans
        lo_ms = start * 1000 // SAMPLE_RATE
        hi_ms = lo_ms + len(audio) * 1000 // SAMPLE_RATE
        assert all(lo_ms <= span_start < hi_ms and span_end <= hi_ms for span_start, span_end in spans)


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, reason="中间段推理异常被 TaskHandler 吞掉并仍可能返回缺段 final，由 T2 修复")
@pytest.mark.parametrize("fake_asr_server", [{"fail_on_call": 2}], indirect=True)
async def test_inference_failure_must_error_or_close_without_incomplete_final(fake_asr_server):
    task_id = str(uuid.uuid4())
    audio = make_encoded_audio(2.0)
    async with websockets.connect(
        fake_asr_server.url, max_size=None, ping_interval=None, open_timeout=5
    ) as websocket:
        await send_audio(
            websocket,
            audio,
            task_id=task_id,
            seg_duration=0.5,
            seg_overlap=0,
            chunk_seconds=0.5,
        )
        messages, closed = await collect_terminal(websocket, task_id=task_id, timeout=5)

    assert closed or any(message.get("type") == "error" for message in messages)
    assert not any(message.get("is_final") for message in messages)


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, reason="is_final 末帧缓冲区绕过分段并整块提交，由 T2b 修复")
async def test_final_payload_is_split_to_configured_segment_size(fake_asr_server):
    task_id = str(uuid.uuid4())
    messages = await transcribe(
        fake_asr_server.url,
        make_encoded_audio(10.0),
        task_id=task_id,
        seg_duration=2.0,
        seg_overlap=0.25,
        chunk_seconds=0.5,
        final_frame_seconds=6.0,
    )
    assert messages[-1]["is_final"] is True
    assert max(call["sample_count"] for call in list(fake_asr_server.calls)) <= round(2.25 * SAMPLE_RATE)


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, reason="WorkerState 仅按 task_id 建会话导致跨连接同 ID 串结果，由 T2 修复")
@pytest.mark.parametrize(
    "fake_asr_server", [{"delay_on_call": 1, "delay_seconds": 0.5}], indirect=True
)
async def test_same_task_id_on_two_connections_must_not_cross_results(fake_asr_server):
    task_id = str(uuid.uuid4())
    async with (
        websockets.connect(fake_asr_server.url, max_size=None, ping_interval=None, open_timeout=5) as ws_a,
        websockets.connect(fake_asr_server.url, max_size=None, ping_interval=None, open_timeout=5) as ws_b,
    ):
        await send_audio(
            ws_a,
            make_encoded_audio(0.5),
            task_id=task_id,
            seg_duration=0.5,
            seg_overlap=0,
            finalize=False,
        )
        await fake_asr_server.wait_for_calls(1, timeout=5)
        await send_audio(
            ws_b,
            make_encoded_audio(1.0, 480_000),
            task_id=task_id,
            seg_duration=0.5,
            seg_overlap=0,
        )
        messages_a, closed_a = await collect_terminal(ws_a, task_id=task_id, timeout=5)

    assert not closed_a and messages_a[-1].get("is_final") is True
    spans_a = decode_spans(messages_a[-1]["text"])
    assert spans_a
    assert all(start_ms < 1000 for start_ms, _ in spans_a), f"连接 A 收到其它连接的区间: {spans_a!r}"
