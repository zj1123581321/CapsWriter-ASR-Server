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
        seg_duration=5.0,
        seg_overlap=0.5,
        chunk_seconds=0.5,
    )
    final = messages[-1]
    assert final["type"] == "result"
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
        transcribe(
            fake_asr_server.url,
            audio,
            task_id=task_id,
            seg_duration=5.0,
            seg_overlap=0.0,
        )
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
@pytest.mark.parametrize("fake_asr_server", [{"fail_on_call": 2}], indirect=True)
async def test_inference_failure_must_error_or_close_without_incomplete_final(fake_asr_server):
    task_id = str(uuid.uuid4())
    audio = make_encoded_audio(10.0)
    async with websockets.connect(
        fake_asr_server.url, max_size=None, ping_interval=None, open_timeout=5
    ) as websocket:
        await send_audio(
            websocket,
            audio,
            task_id=task_id,
            seg_duration=5.0,
            seg_overlap=0,
            chunk_seconds=5.0,
        )
        messages, closed = await collect_terminal(websocket, task_id=task_id, timeout=5)

    errors = [message for message in messages if message.get("type") == "error"]
    assert errors and errors[0]["code"] == "inference_failed"
    assert errors[0]["retryable"] is True
    assert not any(message.get("is_final") for message in messages)
    assert len(fake_asr_server.calls) == 2


@pytest.mark.asyncio
async def test_final_payload_after_three_seconds_is_split_without_gaps(fake_asr_server):
    task_id = str(uuid.uuid4())
    messages = await transcribe(
        fake_asr_server.url,
        make_encoded_audio(103.0),
        task_id=task_id,
        source="mic",
        seg_duration=72.0,
        seg_overlap=2.0,
        chunk_seconds=3.0,
        final_frame_seconds=100.0,
    )
    assert messages[-1]["is_final"] is True
    calls = list(fake_asr_server.calls)
    assert max(call["sample_count"] for call in calls) <= 80 * SAMPLE_RATE
    assert_gapless(decode_spans(messages[-1]["text"]), 103_000, calls)


@pytest.mark.asyncio
async def test_first_final_frame_with_two_hundred_seconds_is_split_without_gaps(fake_asr_server):
    task_id = str(uuid.uuid4())
    messages = await transcribe(
        fake_asr_server.url,
        make_encoded_audio(200.0),
        task_id=task_id,
        source="mic",
        seg_duration=72.0,
        seg_overlap=2.0,
        chunk_seconds=200.0,
        final_frame_seconds=200.0,
    )
    calls = list(fake_asr_server.calls)
    assert max(call["sample_count"] for call in calls) <= 80 * SAMPLE_RATE
    assert_gapless(decode_spans(messages[-1]["text"]), 200_000, calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fake_asr_server", [{"delay_on_call": 1, "delay_seconds": 0.2}], indirect=True
)
async def test_same_task_id_on_two_connections_must_not_cross_results(fake_asr_server):
    task_id = str(uuid.uuid4())
    messages_a, messages_b = await asyncio.gather(
        transcribe(
            fake_asr_server.url,
            make_encoded_audio(0.5),
            task_id=task_id,
            seg_duration=5.0,
            seg_overlap=0,
        ),
        transcribe(
            fake_asr_server.url,
            make_encoded_audio(0.5, 480_000),
            task_id=task_id,
            seg_duration=5.0,
            seg_overlap=0,
        ),
    )
    spans_a = decode_spans(messages_a[-1]["text"])
    spans_b = decode_spans(messages_b[-1]["text"])
    assert spans_a and spans_b
    assert all(0 <= start < 1000 and end <= 500 for start, end in spans_a), spans_a
    assert all(30_000 <= start < 31_000 and end <= 30_500 for start, end in spans_b), spans_b
