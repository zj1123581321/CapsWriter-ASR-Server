# coding: utf-8
"""服务端协议 v2 连接层验收。"""
from __future__ import annotations

import base64
import asyncio
import json
import logging
import multiprocessing
import os
import shutil
import subprocess
import time
import uuid

import numpy as np
import pytest
import websockets
from config_server import ServerConfig

import core.server.connection.audio_decoder as audio_decoder
import core.server.connection.ws_recv as ws_recv_module
from tests.harness.client import collect_terminal, transcribe
from tests.test_backpressure import LocalServer
from tests.test_server_e2e_baseline import assert_gapless, decode_spans, make_encoded_audio


def _frame(task_id, payload=b"", *, encoding="f32le", final=True, samples_total=0):
    message = {
        "task_id": task_id,
        "source": "mic",
        "data": base64.b64encode(payload).decode("ascii"),
        "is_final": final,
        "time_start": time.time(),
        "seg_duration": 5.0,
        "seg_overlap": 0.0,
        "encoding": encoding,
    }
    if final:
        message["samples_total"] = samples_total
    return json.dumps(message)


async def _error(url, *frames):
    async with websockets.connect(url, max_size=None, ping_interval=None) as client:
        for frame in frames:
            await client.send(frame)
        messages, _ = await collect_terminal(client, task_id=json.loads(frames[-1])["task_id"])
    return next(message for message in messages if message.get("type") == "error")


def _encode_flac(samples):
    if shutil.which("ffmpeg") is None:
        pytest.skip("测试环境没有 ffmpeg")
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
         "-f", "f32le", "-ar", "16000", "-ac", "1", "-i", "pipe:0",
         "-c:a", "flac", "-f", "flac", "pipe:1"],
        input=samples.astype("<f4", copy=False).tobytes(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout


@pytest.fixture(scope="module")
def flac_audio():
    samples = np.random.default_rng(11).uniform(-0.8, 0.8, 10 * 16000).astype(np.float32)
    return samples, _encode_flac(samples)


@pytest.mark.asyncio
async def test_v1_frame_without_encoding_keeps_baseline_result(fake_asr_server):
    task_id = str(uuid.uuid4())
    messages = await transcribe(
        fake_asr_server.url, make_encoded_audio(3), task_id=task_id,
        seg_duration=5, seg_overlap=0,
    )
    assert messages[-1]["type"] == "result" and messages[-1]["is_final"] is True
    assert_gapless(decode_spans(messages[-1]["text"]), 3000, fake_asr_server.calls)


@pytest.mark.asyncio
async def test_encoding_change_is_bad_request(fake_asr_server):
    task_id = str(uuid.uuid4())
    error = await _error(
        fake_asr_server.url,
        _frame(task_id, np.zeros(160, dtype="<f4").tobytes(), final=False),
        _frame(task_id, np.zeros(160, dtype="<i2").tobytes(), encoding="s16le", samples_total=160),
    )
    assert error["code"] == "bad_request"


@pytest.mark.asyncio
async def test_unknown_and_unavailable_encodings_are_unsupported(fake_asr_server, monkeypatch):
    error = await _error(fake_asr_server.url, _frame(str(uuid.uuid4()), encoding="mp3"))
    assert error["code"] == "unsupported_encoding"

    monkeypatch.setattr(audio_decoder.shutil, "which", lambda _name: None)
    error = await _error(fake_asr_server.url, _frame(str(uuid.uuid4()), encoding="flac"))
    assert error["code"] == "unsupported_encoding"


@pytest.mark.asyncio
async def test_truncated_and_invalid_flac_fail_and_reap_ffmpeg(fake_asr_server, flac_audio, monkeypatch):
    samples, flac = flac_audio
    real_decoder = ws_recv_module.AudioDecoder
    decoders = []

    class TrackingDecoder(real_decoder):
        def __init__(self, encoding):
            super().__init__(encoding)
            decoders.append(self)

    monkeypatch.setattr(ws_recv_module, "AudioDecoder", TrackingDecoder)
    cases = (
        (flac[:int(len(flac) * 0.4)], int(samples.size), True),
        (os.urandom(4096), int(samples.size), False),
    )
    for payload, declared, truncated in cases:
        error = await _error(
            fake_asr_server.url,
            _frame(str(uuid.uuid4()), payload, encoding="flac", samples_total=declared),
        )
        assert error["code"] == "decode_failed"
        if truncated:
            assert f"声明 {declared}" in error["message"]
            assert "实际解码" in error["message"]
    assert len(decoders) == 2
    assert all(decoder.process.returncode is not None for decoder in decoders)


@pytest.mark.asyncio
async def test_v2_final_requires_samples_total(fake_asr_server):
    message = json.loads(_frame(str(uuid.uuid4())))
    del message["samples_total"]
    error = await _error(fake_asr_server.url, json.dumps(message))
    assert error["code"] == "bad_request"


@pytest.mark.asyncio
async def test_frame_limit_and_decoded_duration_limit(fake_asr_server, monkeypatch):
    monkeypatch.setattr(ws_recv_module, "MAX_AUDIO_FRAME_BYTES", 2)
    error = await _error(
        fake_asr_server.url,
        _frame(str(uuid.uuid4()), np.zeros(1, dtype="<f4").tobytes(), samples_total=1),
    )
    assert error["code"] == "bad_request"

    monkeypatch.setattr(ws_recv_module, "MAX_AUDIO_FRAME_BYTES", 64 * 1024 * 1024)
    monkeypatch.setenv("CW_MAX_TASK_SECONDS", "10")
    samples = np.zeros(15 * 16000, dtype="<f4")
    error = await _error(
        fake_asr_server.url,
        _frame(str(uuid.uuid4()), samples.tobytes(), samples_total=samples.size),
    )
    assert error["code"] == "audio_too_long"


@pytest.mark.asyncio
async def test_compressed_disconnect_reaps_ffmpeg_within_five_seconds(
    fake_asr_server, flac_audio, monkeypatch,
):
    _, flac = flac_audio
    real_decoder = ws_recv_module.AudioDecoder
    decoders = []

    class TrackingDecoder(real_decoder):
        def __init__(self, encoding):
            super().__init__(encoding)
            decoders.append(self)

    monkeypatch.setattr(ws_recv_module, "AudioDecoder", TrackingDecoder)
    client = await websockets.connect(fake_asr_server.url, max_size=None, ping_interval=None)
    await client.send(_frame(str(uuid.uuid4()), flac, encoding="flac", final=False))
    deadline = time.monotonic() + 2
    while not decoders or decoders[0].process is None:
        assert time.monotonic() < deadline
        await asyncio.sleep(0.01)
    process = decoders[0].process
    await client.close()
    deadline = time.monotonic() + 5
    while process.returncode is None:
        assert time.monotonic() < deadline
        await asyncio.sleep(0.01)
    assert process.returncode is not None


@pytest.mark.asyncio
async def test_compressed_consumer_backpressure_pauses_upload_idle(
    monkeypatch,
):
    samples = np.random.default_rng(19).uniform(-0.8, 0.8, 20 * 16000).astype(np.float32)
    flac = _encode_flac(samples)
    monkeypatch.setattr(ServerConfig, "seg_cut_snap", False)
    monkeypatch.setattr(ServerConfig, "max_inflight_segments", 1)
    monkeypatch.setattr(ServerConfig, "upload_idle_seconds", 1)
    task_id = str(uuid.uuid4())
    release = multiprocessing.Event()
    server = await LocalServer.start(release_event=release)
    try:
        async with websockets.connect(server.url, max_size=None, ping_interval=None) as client:
            split = int(len(flac) * 0.8)
            await client.send(_frame(task_id, flac[:split], encoding="flac", final=False))
            deadline = time.monotonic() + 3
            while not server.state.tasks:
                assert time.monotonic() < deadline
                await asyncio.sleep(0.01)
            record = next(iter(server.state.tasks.values()))
            while not record.backpressured:
                assert time.monotonic() < deadline
                await asyncio.sleep(0.01)
            await asyncio.sleep(1.2)
            await client.send(_frame(
                task_id, flac[split:], encoding="flac", samples_total=samples.size
            ))
            release.set()
            messages, _ = await collect_terminal(client, task_id=task_id, timeout=8)
        assert messages[-1]["type"] == "result" and messages[-1]["is_final"] is True
    finally:
        release.set()
        await server.stop()


@pytest.mark.asyncio
async def test_task_end_logs_one_structured_line_for_success_and_failure(fake_asr_server, caplog):
    caplog.set_level(logging.INFO)
    task_id = str(uuid.uuid4())
    await transcribe(
        fake_asr_server.url, np.zeros(2, dtype=np.float32), task_id=task_id,
        seg_duration=5, seg_overlap=0,
    )
    failed_id = str(uuid.uuid4())
    error = await _error(fake_asr_server.url, _frame(failed_id, encoding="mp3"))
    assert error["code"] == "unsupported_encoding"
    lines = [record.getMessage() for record in caplog.records if record.getMessage().startswith("task_end ")]
    own_lines = [line for line in lines if f"task={task_id} " in line or f"task={failed_id} " in line]
    assert len(own_lines) == 2
    assert any(f"task={task_id} status=done code=- " in line and "encoding=v1" in line for line in own_lines)
    assert any(f"task={failed_id} status=failed code=unsupported_encoding " in line and "encoding=mp3" in line for line in own_lines)
    assert all("duration_s=" in line and "elapsed_s=" in line and "segments=" in line for line in own_lines)
