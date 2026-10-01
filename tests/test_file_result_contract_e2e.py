# coding: utf-8
"""真实 WS、TaskHandler 子进程与 JSON final/error 发布契约。"""

from __future__ import annotations

import uuid

import numpy as np
import pytest
import websockets

from tests.harness.client import collect_terminal, send_audio


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fake_asr_server",
    [
        {
            "supports_timestamps": True,
            "result_text": "三百",
            "result_tokens": ["三", "百"],
            "result_timestamps": [0.1, 0.2],
        }
    ],
    indirect=True,
)
async def test_file_short_eof_publishes_final_contract_payload(fake_asr_server):
    task_id = str(uuid.uuid4())
    samples = np.zeros(round(5.05 * 16000), dtype=np.float32)

    async with websockets.connect(
        fake_asr_server.url, max_size=None, ping_interval=None, open_timeout=5
    ) as websocket:
        await send_audio(
            websocket,
            samples,
            task_id=task_id,
            source="file",
            seg_duration=5.0,
            seg_overlap=0.0,
            chunk_seconds=5.05,
            final_frame_seconds=0.05,
        )
        messages, closed = await collect_terminal(websocket, task_id=task_id, timeout=5)

    assert not closed
    final = messages[-1]
    assert final["type"] == "result"
    assert final["is_final"] is True
    assert "".join(final["tokens"]) == final["text_accu"]
    assert len(final["tokens"]) == len(final["timestamps"])
    assert final["text_accu"] == "300"
    assert final["tokens"] == ["300"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fake_asr_server",
    [
        {
            "supports_timestamps": True,
            "result_text": "错误数组",
            "result_tokens": ["错", "误"],
            "result_timestamps": [0.1],
        }
    ],
    indirect=True,
)
async def test_file_raw_mismatch_publishes_error_without_successful_final(
    fake_asr_server,
):
    task_id = str(uuid.uuid4())
    samples = np.zeros(1600, dtype=np.float32)

    async with websockets.connect(
        fake_asr_server.url, max_size=None, ping_interval=None, open_timeout=5
    ) as websocket:
        await send_audio(
            websocket,
            samples,
            task_id=task_id,
            source="file",
            seg_duration=5.0,
            seg_overlap=0.0,
            chunk_seconds=0.1,
            final_frame_seconds=0.1,
        )
        messages, _ = await collect_terminal(websocket, task_id=task_id, timeout=5)

    errors = [message for message in messages if message.get("type") == "error"]
    assert errors and errors[0]["code"] == "inference_failed"
    assert not any(message.get("is_final") for message in messages)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fake_asr_server",
    [
        {
            "supports_timestamps": True,
            "result_text": "正文",
            "result_tokens": ["正", "文"],
            "result_timestamps": [0.1, 0.2],
            "break_final_sync": True,
        }
    ],
    indirect=True,
)
async def test_file_missing_final_sync_publishes_error_without_successful_final(
    fake_asr_server,
):
    task_id = str(uuid.uuid4())
    samples = np.zeros(1600, dtype=np.float32)

    async with websockets.connect(
        fake_asr_server.url, max_size=None, ping_interval=None, open_timeout=5
    ) as websocket:
        await send_audio(
            websocket,
            samples,
            task_id=task_id,
            source="file",
            seg_duration=5.0,
            seg_overlap=0.0,
            chunk_seconds=0.1,
            final_frame_seconds=0.1,
        )
        messages, _ = await collect_terminal(websocket, task_id=task_id, timeout=5)

    errors = [message for message in messages if message.get("type") == "error"]
    assert errors and errors[0]["code"] == "inference_failed"
    assert not any(message.get("is_final") for message in messages)
