"""SDK v2 的跨 HTTP、子进程和 WebSocket 契约测试。"""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path

import numpy as np
import pytest
import pytest_asyncio
import soundfile as sf
import websockets
from websockets.datastructures import Headers

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "sdk"))

import capswriter_asr.client as sdk_client
from capswriter_asr import AsrError, Transcript, transcribe_file
from capswriter_asr.client import _transcode


@pytest_asyncio.fixture(autouse=True)
async def assert_async_processes_are_reaped(monkeypatch):
    original = asyncio.create_subprocess_exec
    processes = []

    async def tracked(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", tracked)
    yield
    assert all(process.returncode is not None for process in processes), [
        process.pid for process in processes if process.returncode is None
    ]


def make_audio(path: Path, seconds: float = 1.0) -> Path:
    samples = np.zeros(int(16000 * seconds), dtype=np.float32)
    sf.write(path, samples, 16000)
    return path


def final_result(**overrides):
    result = {
        "type": "result",
        "is_final": True,
        "text": "你好，世界。",
        "tokens": ["你", "好", "，", "世", "界", "。"],
        "timestamps": [0.0, 0.3, 0.5, 0.7, 0.9, 1.1],
        "duration": 2.0,
    }
    result.update(overrides)
    return result


@asynccontextmanager
async def fake_v2_server(handler, *, health=None, status=200):
    state = {"connections": 0, "frames": [], "final_received": False}
    if isinstance(health, bytes):
        health_body = health
    else:
        health_body = json.dumps(
            health or {"protocol_version": 2, "encodings": ["flac", "ogg_opus", "f32le", "s16le"]}
        ).encode()

    async def process_request(connection, request):
        path = request.path if hasattr(request, "path") else connection
        if path != "/health":
            return None
        headers = Headers()
        headers["Content-Type"] = "application/json"
        if hasattr(request, "path"):
            from websockets.http11 import Response

            return Response(status, HTTPStatus(status).phrase, headers, health_body)
        return HTTPStatus(status), headers, health_body

    async def receive(ws):
        state["connections"] += 1
        await handler(ws, state)

    async with websockets.serve(
        receive,
        "127.0.0.1",
        0,
        process_request=process_request,
        ping_interval=None,
        max_size=None,
        max_queue=None,
    ) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}", state


async def accept_and_finish(ws, state):
    async for message in ws:
        frame = json.loads(message)
        state["frames"].append(frame)
        if frame["is_final"]:
            state["final_received"] = True
            await ws.send(json.dumps(final_result()))
            return


@pytest.mark.asyncio
async def test_flac_upload_matches_transcode_and_v2_frames(tmp_path):
    audio_path = make_audio(tmp_path / "source.wav", 5)
    expected = await _transcode(audio_path, "flac")
    async with fake_v2_server(accept_and_finish) as (url, state):
        transcript = await transcribe_file(audio_path, url)
    assert isinstance(transcript, Transcript)
    assert b"".join(base64.b64decode(frame["data"]) for frame in state["frames"]) == expected
    assert state["frames"]
    task_ids = {frame["task_id"] for frame in state["frames"]}
    assert len(task_ids) == 1
    assert all(frame["encoding"] == "flac" for frame in state["frames"])
    assert all(len(base64.b64decode(frame["data"])) <= 256 * 1024 for frame in state["frames"])
    assert state["frames"][-1]["is_final"] is True
    assert transcript.raw["type"] == "result"


@pytest.mark.asyncio
async def test_raw_f32le_frames_are_at_most_sixty_seconds(tmp_path, monkeypatch):
    audio_path = make_audio(tmp_path / "source.wav")
    pcm = b"\0" * (61 * 16000 * 4 + 4)
    monkeypatch.setattr(sdk_client, "_transcode", lambda *_: asyncio.sleep(0, result=pcm))
    async with fake_v2_server(accept_and_finish) as (url, state):
        await transcribe_file(audio_path, url, encoding="f32le")
    chunks = [base64.b64decode(frame["data"]) for frame in state["frames"]]
    assert len(chunks) == 2
    assert all(len(chunk) // 4 <= 60 * 16000 for chunk in chunks)
    assert [frame["is_final"] for frame in state["frames"]] == [False, True]


@pytest.mark.asyncio
async def test_progress_is_received_before_upload_finishes(tmp_path, monkeypatch):
    audio_path = make_audio(tmp_path / "source.wav")
    pcm = b"\0" * (3 * 256 * 1024)
    monkeypatch.setattr(sdk_client, "_transcode", lambda *_: asyncio.sleep(0, result=pcm))
    sent = {"final_send_returned": False}
    original_connect = websockets.connect

    class DelayedConnection:
        def __init__(self, context_manager):
            self.context_manager = context_manager
            self.ws = None
            self.calls = 0

        async def __aenter__(self):
            self.ws = await self.context_manager.__aenter__()
            return self

        async def __aexit__(self, *args):
            return await self.context_manager.__aexit__(*args)

        async def send(self, message):
            await self.ws.send(message)
            self.calls += 1
            if json.loads(message)["is_final"]:
                sent["final_send_returned"] = True
            if self.calls == 1:
                await asyncio.sleep(0.1)

        async def recv(self):
            return await self.ws.recv()

    def delayed_connect(url, *args, **kwargs):
        assert kwargs["ping_interval"] is None
        assert kwargs["max_size"] is None
        assert kwargs["max_queue"] is None
        if "proxy" in inspect.signature(original_connect).parameters:
            assert kwargs["proxy"] is None
        return DelayedConnection(original_connect(url, *args, **kwargs))

    delayed_connect.__signature__ = inspect.signature(original_connect)

    monkeypatch.setattr(sdk_client.websockets, "connect", delayed_connect)
    progress_observed = []

    async def reply_on_first_frame(ws, state):
        async for message in ws:
            frame = json.loads(message)
            state["frames"].append(frame)
            if len(state["frames"]) == 1:
                await ws.send(json.dumps({"type": "result", "is_final": False, "text": "进度"}))
            if frame["is_final"]:
                state["final_received"] = True
                await ws.send(json.dumps(final_result()))
                return

    async with fake_v2_server(reply_on_first_frame) as (url, state):
        await transcribe_file(
            audio_path,
            url,
            encoding="flac",
            on_progress=lambda result: progress_observed.append((result, sent["final_send_returned"])),
        )
    assert progress_observed == [({"type": "result", "is_final": False, "text": "进度"}, False)]
    assert state["final_received"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,health,expected_code", [
    (426, None, "server_too_old"),
    (200, {"protocol_version": 1, "encodings": ["flac"]}, "server_too_old"),
    (200, {"protocol_version": "2", "encodings": ["flac"]}, "server_too_old"),
    (200, b"not json", "server_too_old"),
    (200, {"protocol_version": 2, "encodings": ["f32le"]}, "unsupported_encoding"),
])
async def test_health_gate_rejects_before_websocket(tmp_path, status, health, expected_code):
    audio_path = make_audio(tmp_path / "source.wav")
    async with fake_v2_server(accept_and_finish, health=health, status=status) as (url, state):
        with pytest.raises(AsrError) as caught:
            await transcribe_file(audio_path, url)
    assert caught.value.code == expected_code
    assert state["connections"] == 0


@pytest.mark.asyncio
async def test_server_error_code_and_retryable_are_preserved(tmp_path):
    audio_path = make_audio(tmp_path / "source.wav")

    async def send_error(ws, state):
        await ws.recv()
        await ws.send(json.dumps({
            "type": "error", "code": "inference_failed", "message": "engine failed", "retryable": True
        }))

    async with fake_v2_server(send_error) as (url, _):
        with pytest.raises(AsrError) as caught:
            await transcribe_file(audio_path, url)
    assert caught.value.code == "inference_failed"
    assert caught.value.retryable is True
    assert caught.value.message == "engine failed"


@pytest.mark.asyncio
async def test_idle_timeout_is_independent_of_incoming_messages(tmp_path):
    audio_path = make_audio(tmp_path / "source.wav")

    async def never_reply(ws, _state):
        await ws.recv()
        await ws.recv()

    async with fake_v2_server(never_reply) as (url, _):
        started = time.monotonic()
        with pytest.raises(AsrError) as caught:
            await transcribe_file(audio_path, url, idle_timeout=2, deadline_total=10)
    assert caught.value.code == "timeout"
    assert time.monotonic() - started < 5


@pytest.mark.asyncio
async def test_blocked_send_uses_idle_timeout(tmp_path, monkeypatch):
    audio_path = make_audio(tmp_path / "source.wav")
    original_connect = websockets.connect
    proxy_unspecified = object()

    class BlockedSendConnection:
        def __init__(self, context_manager):
            self.context_manager = context_manager
            self.ws = None

        async def __aenter__(self):
            self.ws = await self.context_manager.__aenter__()
            return self

        async def __aexit__(self, *args):
            return await self.context_manager.__aexit__(*args)

        async def send(self, _message):
            await asyncio.Future()

        async def recv(self):
            return await self.ws.recv()

    def blocked_connect(url, *, ping_interval=None, max_size=None, max_queue=None, proxy=proxy_unspecified):
        options = {"ping_interval": ping_interval, "max_size": max_size, "max_queue": max_queue}
        if proxy is not proxy_unspecified:
            options["proxy"] = proxy
        return BlockedSendConnection(original_connect(url, **options))

    blocked_connect.__signature__ = inspect.signature(original_connect)
    monkeypatch.setattr(sdk_client.websockets, "connect", blocked_connect)

    async with fake_v2_server(accept_and_finish) as (url, _state):
        started = time.monotonic()
        with pytest.raises(AsrError) as caught:
            await transcribe_file(audio_path, url, idle_timeout=2, deadline_total=10)
    assert caught.value.code == "timeout"
    assert "发送音频帧" in caught.value.message
    assert time.monotonic() - started < 5


@pytest.mark.asyncio
async def test_total_deadline_expires_despite_continuous_progress(tmp_path):
    audio_path = make_audio(tmp_path / "source.wav")

    async def progress_forever(ws, _state):
        await ws.recv()
        while True:
            await ws.send(json.dumps({"type": "result", "is_final": False, "text": "进度"}))
            await asyncio.sleep(0.05)

    async with fake_v2_server(progress_forever) as (url, _):
        with pytest.raises(AsrError) as caught:
            await transcribe_file(
                audio_path,
                url,
                deadline_total=3,
                idle_timeout=10,
                on_progress=lambda _result: None,
            )
    assert caught.value.code == "timeout"


@pytest.mark.asyncio
async def test_close_without_error_frame_maps_to_connection_lost(tmp_path):
    audio_path = make_audio(tmp_path / "source.wav")

    async def close_without_error(ws, _state):
        await ws.recv()
        await ws.close(code=4000)

    async with fake_v2_server(close_without_error) as (url, _):
        with pytest.raises(AsrError) as caught:
            await transcribe_file(audio_path, url)
    assert caught.value.code == "connection_lost"


@pytest.mark.asyncio
async def test_transcode_failure_uses_decode_failed(monkeypatch, tmp_path):
    monkeypatch.setattr(sdk_client.shutil, "which", lambda _name: None)
    with pytest.raises(AsrError) as caught:
        await _transcode(tmp_path / "source.wav", "flac")
    assert caught.value.code == "decode_failed"


def test_cli_help_exits_successfully():
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT / "sdk"))
    result = subprocess.run(
        [sys.executable, "-m", "capswriter_asr", "--help"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "--url" in result.stdout


@pytest.mark.asyncio
async def test_cli_writes_srt_txt_and_json_with_legacy_srt_layout(tmp_path):
    audio_path = make_audio(tmp_path / "clip.wav")
    out_dir = tmp_path / "outputs"

    async def finish_with_legacy_fixture(ws, state):
        await accept_and_finish(ws, state)

    async with fake_v2_server(finish_with_legacy_fixture) as (url, _state):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "capswriter_asr",
            str(audio_path),
            "--url",
            url,
            "--out-dir",
            str(out_dir),
            "--format",
            "srt,txt,json",
            env=dict(os.environ, PYTHONPATH=str(REPO_ROOT / "sdk")),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
    assert process.returncode == 0, (stdout, stderr)
    assert (out_dir / "clip.srt").read_text(encoding="utf-8") == (
        "1\n00:00:00,000 --> 00:00:01,600\n你好，世界。\n"
    )
    assert (out_dir / "clip.txt").read_text(encoding="utf-8") == "你好，世界。"
    assert json.loads((out_dir / "clip.json").read_text(encoding="utf-8"))["type"] == "result"
