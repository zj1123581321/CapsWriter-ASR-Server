# coding: utf-8
"""T2 服务端错误契约的真实 WebSocket 与子进程验收。"""
from __future__ import annotations

import asyncio
import base64
import json
import multiprocessing
import os
import signal
import time

import numpy as np
import pytest
import websockets

from core.protocol import AudioMessage
from core.server.schema import Result
from tests.harness.client import collect_terminal, send_audio
from tests.harness.server import ManagedFakeServerHarness, run_model_load_failure


SAMPLE_RATE = 16000


def make_audio(seconds: float, start_sample: int = 0) -> np.ndarray:
    count = round(seconds * SAMPLE_RATE)
    return np.arange(start_sample, start_sample + count, dtype=np.float32) / SAMPLE_RATE


def frame(task_id: str, *, is_final: bool = False, seconds: float = 0.1) -> str:
    samples = make_audio(seconds)
    return AudioMessage(
        task_id=task_id,
        source="mic",
        data=base64.b64encode(samples.astype("<f4").tobytes()).decode("ascii"),
        is_final=is_final,
        time_start=time.time(),
        seg_duration=10.0,
        seg_overlap=0.0,
    ).to_json()


async def receive_error_and_close(websocket, *, code: str, task_id: str, timeout: float = 5):
    error = json.loads(await asyncio.wait_for(websocket.recv(), timeout))
    assert set(error) == {"type", "task_id", "code", "message", "retryable"}
    assert error["type"] == "error"
    assert error["task_id"] == task_id
    assert error["code"] == code
    assert isinstance(error["message"], str)
    assert type(error["retryable"]) is bool
    with pytest.raises(websockets.ConnectionClosed) as closed:
        await asyncio.wait_for(websocket.recv(), timeout)
    assert closed.value.code == 4000
    assert closed.value.reason == code
    return error


async def assert_connection_closed(websocket, *, code: str = "inference_failed"):
    with pytest.raises(websockets.ConnectionClosed) as closed:
        await asyncio.wait_for(websocket.recv(), 5)
    assert closed.value.code == 4000
    assert closed.value.reason == code


@pytest.mark.parametrize("fake_asr_server", [{"fail_on_call": 2}], indirect=True)
@pytest.mark.asyncio
async def test_intermediate_inference_failure_stops_later_segments(fake_asr_server):
    task_id = "middle-segment-failure"
    async with websockets.connect(
        fake_asr_server.url, max_size=None, ping_interval=None
    ) as websocket:
        await send_audio(
            websocket,
            make_audio(2.0),
            task_id=task_id,
            seg_duration=0.5,
            seg_overlap=0,
            chunk_seconds=0.5,
        )
        messages, closed = await collect_terminal(websocket, task_id=task_id, timeout=5)
        assert closed is False
        errors = [message for message in messages if message.get("type") == "error"]
        assert len(errors) == 1
        assert errors[0]["code"] == "inference_failed"
        assert errors[0]["retryable"] is True
        assert "offset=" in errors[0]["message"]
        assert "RuntimeError" in errors[0]["message"]
        assert not any(message.get("is_final") for message in messages)
        await assert_connection_closed(websocket)
    assert len(fake_asr_server.calls) == 2


@pytest.mark.parametrize(
    ("raw", "expected_task_id"),
    [
        ("this is not json", ""),
        (json.dumps({"source": "mic", "data": "", "is_final": True, "time_start": 0}), ""),
    ],
)
@pytest.mark.asyncio
async def test_bad_request_sends_error_then_closes(fake_asr_server, raw, expected_task_id):
    async with websockets.connect(fake_asr_server.url, ping_interval=None) as websocket:
        await websocket.send(raw)
        error = await receive_error_and_close(
            websocket, code="bad_request", task_id=expected_task_id
        )
    assert error["retryable"] is False


@pytest.mark.asyncio
async def test_same_connection_conflict_closes_with_task_conflict(fake_asr_server):
    async with websockets.connect(fake_asr_server.url, ping_interval=None) as websocket:
        await websocket.send(frame("task-a"))
        await websocket.send(frame("task-b"))
        error = await receive_error_and_close(
            websocket, code="task_conflict", task_id="task-b"
        )
    assert error["retryable"] is False


@pytest.mark.asyncio
async def test_same_connection_can_start_next_task_after_final(fake_asr_server):
    async with websockets.connect(
        fake_asr_server.url, max_size=None, ping_interval=None
    ) as websocket:
        for task_id in ("first", "second"):
            await send_audio(
                websocket,
                make_audio(0.5),
                task_id=task_id,
                seg_duration=0.5,
                seg_overlap=0,
                chunk_seconds=0.5,
            )
            messages, closed = await collect_terminal(websocket, task_id=task_id, timeout=5)
            assert not closed
            assert messages[-1]["type"] == "result"
            assert messages[-1]["is_final"] is True


@pytest.mark.parametrize("fake_asr_server", [{"fail_on_call": 4}], indirect=True)
@pytest.mark.asyncio
async def test_final_segment_inference_failure_is_error(fake_asr_server):
    task_id = "final-segment-failure"
    async with websockets.connect(
        fake_asr_server.url, max_size=None, ping_interval=None
    ) as websocket:
        await send_audio(
            websocket,
            make_audio(2.0),
            task_id=task_id,
            seg_duration=0.5,
            seg_overlap=0,
            chunk_seconds=0.5,
            final_frame_seconds=0.5,
        )
        messages, closed = await collect_terminal(websocket, task_id=task_id, timeout=5)
        errors = [message for message in messages if message.get("type") == "error"]
        assert errors and errors[0]["code"] == "inference_failed"
        assert errors[0]["retryable"] is True
        assert "offset=" in errors[0]["message"] and "RuntimeError" in errors[0]["message"]
        assert closed is False
        assert not any(message.get("is_final") for message in messages)
        await assert_connection_closed(websocket)
    assert len(fake_asr_server.calls) == 4


@pytest.mark.skipif(os.name == "nt", reason="SIGKILL 子进程验证仅适用于 POSIX")
@pytest.mark.asyncio
async def test_worker_killed_closes_clients_and_exits_main_nonzero():
    server = await ManagedFakeServerHarness.start(
        {"delay_on_call": 1, "delay_seconds": 30}
    )
    try:
        async with websockets.connect(
            server.url, max_size=None, ping_interval=None
        ) as websocket:
            await send_audio(
                websocket,
                make_audio(1.0),
                task_id="kill-worker",
                seg_duration=0.5,
                seg_overlap=0,
                chunk_seconds=0.5,
            )
            await server.wait_for_calls(1)
            os.kill(server.worker_pid, signal.SIGKILL)
            messages, closed = await collect_terminal(
                websocket, task_id="kill-worker", timeout=5
            )
            assert closed or any(
                message.get("type") == "error" and message.get("code") == "internal"
                for message in messages
            )
        await asyncio.to_thread(server.process.join, 5)
        assert server.process.exitcode == 1
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_segment_watchdog_errors_and_exits_main_nonzero(monkeypatch):
    monkeypatch.setenv("CW_SEGMENT_TIMEOUT", "2")
    server = await ManagedFakeServerHarness.start(
        {"delay_on_call": 1, "delay_seconds": 10}
    )
    try:
        async with websockets.connect(
            server.url, max_size=None, ping_interval=None
        ) as websocket:
            await send_audio(
                websocket,
                make_audio(1.0),
                task_id="watchdog",
                seg_duration=0.5,
                seg_overlap=0,
                chunk_seconds=0.5,
            )
            messages, closed = await collect_terminal(
                websocket, task_id="watchdog", timeout=3
            )
            errors = [message for message in messages if message.get("type") == "error"]
            assert errors and errors[0]["code"] == "inference_timeout"
            assert errors[0]["retryable"] is True
            assert not any(message.get("is_final") for message in messages)
        await asyncio.to_thread(server.process.join, 5)
        assert server.process.exitcode == 1
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_slow_consumer_does_not_block_another_connection():
    server = await ManagedFakeServerHarness.start(stall_first_sender=True)
    try:
        async with websockets.connect(
            server.url, max_size=None, max_queue=1, ping_interval=None
        ) as slow:
            await send_audio(
                slow,
                make_audio(0.8),
                task_id="slow",
                seg_duration=0.2,
                seg_overlap=0,
                chunk_seconds=0.8,
                finalize=False,
            )
            slow.transport.pause_reading()
            await server.wait_for_calls(1)
            observation = next(item for item in list(server.observed) if item["task_id"] == "slow")
            socket_id = observation["socket_id"]
            inject = [
                Result(
                    task_id="slow",
                    socket_id=socket_id,
                    type="mic",
                    text="x" * 1024,
                )
                for _ in range(400)
            ]
            injection = asyncio.create_task(
                asyncio.to_thread(
                    lambda: [server.queue_out.put(result) for result in inject]
                )
            )

            async with websockets.connect(
                server.url, max_size=None, ping_interval=None
            ) as normal:
                await send_audio(
                    normal,
                    make_audio(1.0),
                    task_id="normal",
                    seg_duration=0.5,
                    seg_overlap=0,
                    chunk_seconds=0.5,
                )
                normal_messages, normal_closed = await collect_terminal(
                    normal, task_id="normal", timeout=10
                )
                assert not normal_closed
                assert normal_messages[-1]["type"] == "result"
                assert normal_messages[-1]["is_final"] is True

            await asyncio.wait_for(injection, timeout=30)
            slow.transport.resume_reading()
            with pytest.raises(websockets.ConnectionClosed) as closed:
                async with asyncio.timeout(20):
                    while True:
                        await slow.recv()
            assert closed.value.code == 4000
            assert closed.value.reason == "slow_consumer"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_model_load_failure_exits_main_nonzero():
    process = multiprocessing.Process(target=run_model_load_failure)
    process.start()
    await asyncio.to_thread(process.join, 5)
    assert not process.is_alive()
    assert process.exitcode == 1
