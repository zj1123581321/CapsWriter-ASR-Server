# coding: utf-8
"""服务端 /health 协议与运行态验收。"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest
import websockets
import numpy as np

from config_server import ServerConfig
from core.server.connection.audio_decoder import available_encodings
from core.server.connection.health import build_health_payload, process_request
from core.server.worker.worker import RecognizerWorker
from tests.harness.client import collect_terminal, send_audio
from tests.harness.server import ManagedFakeServerHarness


class RecordingQueue:
    def __init__(self):
        self.messages = []

    def put(self, message):
        self.messages.append(message)


@pytest.mark.parametrize("aligner_status", ["native", "loaded", "not_required"])
def test_worker_emits_model_loaded_signal_with_aligner(aligner_status):
    output = RecordingQueue()
    worker = RecognizerWorker.__new__(RecognizerWorker)
    worker._setup_environment = lambda: None
    worker.loader = SimpleNamespace(
        load=lambda: None,
        recognizer=object(),
        punc_model=None,
        aligner=None,
        aligner_status=aligner_status,
    )
    worker.handler = SimpleNamespace(
        queue_out=output,
        set_engine=lambda **kwargs: None,
    )

    worker.initialize()

    assert output.messages == [{"loaded": True, "aligner": aligner_status}]


def test_health_payload_reports_unavailable_when_worker_is_dead():
    state = SimpleNamespace(
        recognize_process=SimpleNamespace(is_alive=lambda: False),
        tasks={},
        pending_segments={},
    )
    process_manager = SimpleNamespace(models_ready=True, aligner_status="loaded")
    app = SimpleNamespace(state=state, process_manager=process_manager)

    payload = build_health_payload(state, process_manager)
    response = process_request(None, SimpleNamespace(path="/health"), app=app)

    assert payload["worker_alive"] is False
    assert payload["status"] == "unavailable"
    assert response.status_code == 503
    unavailable_payload = json.loads(response.body)
    assert unavailable_payload["worker_alive"] is False
    assert set(unavailable_payload) == set(payload)


def _get_health(url: str) -> tuple[int, dict]:
    try:
        with urlopen(url, timeout=5) as response:
            return response.status, json.loads(response.read())
    except HTTPError as response:
        try:
            return response.code, json.loads(response.read())
        finally:
            response.close()


def _http_url(websocket_url: str) -> str:
    return websocket_url.replace("ws://", "http://", 1)


@pytest.mark.asyncio
async def test_health_endpoint_tracks_work_and_preserves_http_and_websocket():
    harness = await ManagedFakeServerHarness.start(
        options={"delay_on_call": 1, "delay_seconds": 2.0}
    )
    try:
        status_code, payload = await asyncio.to_thread(
            _get_health, f"{_http_url(harness.url)}/health"
        )
        assert status_code == 200
        assert set(payload) == {
            "status",
            "protocol_version",
            "role",
            "encodings",
            "model",
            "git_sha",
            "llama_build",
            "worker_alive",
            "aligner",
            "active_tasks",
            "queued_segments",
        }
        assert payload["status"] == "ok"
        assert payload["protocol_version"] == 2
        assert payload["role"] == "server"
        assert payload["encodings"] == available_encodings()
        assert payload["model"] == ServerConfig.model_type
        assert isinstance(payload["git_sha"], str) and payload["git_sha"]
        if (
            payload["model"].lower() in {"qwen_asr", "fun_asr_nano"}
            or payload["aligner"] == "loaded"
        ):
            assert payload["llama_build"] == "b10621"
        else:
            assert payload["llama_build"] is None
        assert payload["worker_alive"] is True
        assert payload["aligner"] == "loaded"
        assert type(payload["active_tasks"]) is int
        assert type(payload["queued_segments"]) is int
        assert payload["active_tasks"] == 0
        assert payload["queued_segments"] == 0

        with pytest.raises(HTTPError) as http_error:
            await asyncio.to_thread(
                urlopen, f"{_http_url(harness.url)}/other", timeout=5
            )
        assert http_error.value.code == 426
        http_error.value.read()
        http_error.value.close()

        async with websockets.connect(harness.url, ping_interval=None) as websocket:
            await send_audio(
                websocket,
                np.zeros(16000, dtype=np.float32),
                task_id="health-active-task",
                seg_duration=5.0,
                seg_overlap=1.0,
            )
            await harness.wait_for_calls(1)
            _, active = await asyncio.to_thread(
                _get_health, f"{_http_url(harness.url)}/health"
            )
            assert active["active_tasks"] == 1
            assert active["queued_segments"] == 1

            messages, closed = await collect_terminal(
                websocket, task_id="health-active-task", timeout=10
            )
            assert closed is False
            assert messages[-1]["is_final"] is True

        _, idle = await asyncio.to_thread(
            _get_health, f"{_http_url(harness.url)}/health"
        )
        assert idle["active_tasks"] == 0
        assert idle["queued_segments"] == 0
    finally:
        await harness.stop()


@pytest.mark.skipif(os.name == "nt", reason="SIGKILL 子进程验证仅适用于 POSIX")
@pytest.mark.asyncio
async def test_health_returns_503_after_worker_is_killed_before_monitor_exit():
    harness = await ManagedFakeServerHarness.start(monitor_interval=30)
    try:
        os.kill(harness.worker_pid, signal.SIGKILL)
        await asyncio.sleep(0.05)
        status_code, payload = await asyncio.to_thread(
            _get_health, f"{_http_url(harness.url)}/health"
        )

        assert status_code == 503
        assert payload["status"] == "unavailable"
        assert payload["worker_alive"] is False
    finally:
        await harness.stop()
