# coding: utf-8
"""服务端 /health 协议与运行态验收。"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import urlopen
from uuid import uuid4

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


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


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
        git_sha="test-sha",
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


@pytest.mark.asyncio
async def test_health_git_sha_is_frozen_at_server_start(tmp_path):
    repository_root = Path(__file__).resolve().parents[1]
    service_dir = tmp_path / "service"
    branch = f"health-sha-{uuid4().hex}"
    _run_git(repository_root, "worktree", "add", "-b", branch, str(service_dir), "HEAD")

    process = None
    try:
        shutil.copy2(
            repository_root / "core/server/connection/health.py",
            service_dir / "core/server/connection/health.py",
        )
        shutil.copy2(
            repository_root / "core/server/state.py",
            service_dir / "core/server/state.py",
        )
        initial_sha = _run_git(service_dir, "rev-parse", "--short", "HEAD").stdout.strip()

        instrumentation_dir = tmp_path / "instrumentation"
        instrumentation_dir.mkdir()
        calls_file = tmp_path / "git-rev-parse-calls.txt"
        (instrumentation_dir / "sitecustomize.py").write_text(
            "import os\n"
            "import subprocess\n"
            "_run = subprocess.run\n"
            "def _track_git_sha(*args, **kwargs):\n"
            "    command = args[0] if args else kwargs['args']\n"
            "    if command == ['git', 'rev-parse', '--short', 'HEAD']:\n"
            "        with open(os.environ['HEALTH_GIT_SHA_CALLS'], 'a') as calls:\n"
            "            calls.write(str(os.getpid()) + '\\n')\n"
            "    return _run(*args, **kwargs)\n"
            "subprocess.run = _track_git_sha\n",
            encoding="utf-8",
        )
        ready_file = tmp_path / "server-ready.txt"
        server_script = (
            "import asyncio, sys\n"
            "from tests.harness.server import ManagedFakeServerHarness\n"
            "async def main():\n"
            "    harness = await ManagedFakeServerHarness.start()\n"
            "    open(sys.argv[1], 'w', encoding='utf-8').write(harness.url)\n"
            "    await asyncio.to_thread(sys.stdin.readline)\n"
            "    await harness.stop()\n"
            "asyncio.run(main())\n"
        )
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(instrumentation_dir), str(service_dir)]
        )
        environment["HEALTH_GIT_SHA_CALLS"] = str(calls_file)
        process = subprocess.Popen(
            [sys.executable, "-c", server_script, str(ready_file)],
            cwd=service_dir,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )

        deadline = asyncio.get_running_loop().time() + 20
        while (
            not ready_file.exists()
            and process.poll() is None
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.05)
        assert ready_file.exists(), f"测试服务未能启动，exitcode={process.poll()}"

        url = f"{_http_url(ready_file.read_text(encoding='utf-8'))}/health"
        assert len(calls_file.read_text(encoding="utf-8").splitlines()) == 1

        status_code, started = await asyncio.to_thread(_get_health, url)
        assert status_code == 200
        assert started["git_sha"] == initial_sha

        (service_dir / "runtime-version.txt").write_text("new version\n")
        _run_git(service_dir, "add", "runtime-version.txt")
        _run_git(
            service_dir,
            "-c",
            "user.name=Health Test",
            "-c",
            "user.email=health@example.invalid",
            "commit",
            "-m",
            "advance test HEAD",
        )
        new_sha = _run_git(service_dir, "rev-parse", "--short", "HEAD").stdout.strip()
        assert new_sha != initial_sha

        for _ in range(20):
            status_code, current = await asyncio.to_thread(_get_health, url)
            assert status_code == 200
            assert current["git_sha"] == initial_sha
        assert len(calls_file.read_text(encoding="utf-8").splitlines()) == 1
    finally:
        if process is not None and process.poll() is None:
            process.stdin.write("stop\n")
            process.stdin.flush()
            process.wait(timeout=10)
        _run_git(repository_root, "worktree", "remove", "--force", str(service_dir))
        _run_git(repository_root, "branch", "-D", branch)


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
