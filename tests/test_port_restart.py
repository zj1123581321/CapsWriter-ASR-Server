# coding: utf-8
"""服务端真实监听端口的 TIME_WAIT 重启与冲突回归。"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest
import websockets

from tests.harness.client import collect_terminal, send_audio
from tests.test_server_e2e_baseline import make_encoded_audio


ROOT = Path(__file__).resolve().parents[1]


def _start_fake_service(port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "tests.test_port_restart", "--serve"],
        cwd=ROOT,
        env={**os.environ, "CW_ADDR": "127.0.0.1", "CW_PORT": str(port)},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _run_fake_service() -> None:
    from multiprocessing import Manager, Process
    from types import SimpleNamespace

    from core.server.connection.server_manager import SocketManager
    from core.server.state import ServerState
    from tests.harness.worker import run_health_fake_worker

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    state = ServerState()
    manager = Manager()
    state.sockets_id = manager.list()
    calls = manager.list()
    worker = Process(
        target=run_health_fake_worker,
        args=(state.queue_in, state.queue_out, state.sockets_id, {}, calls),
        daemon=True,
    )
    worker.start()
    ready = state.queue_out.get(True, 20)
    assert ready["loaded"] is True
    state.recognize_process = worker
    process_manager = SimpleNamespace(
        models_ready=True, aligner_status="native", stopped=asyncio.Event()
    )

    async def monitor():
        await process_manager.stopped.wait()

    process_manager.monitor = monitor
    app = SimpleNamespace(loop=loop, state=state, process_manager=process_manager)
    socket_manager = SocketManager(app)

    def stop():
        state.queue_in.put(None)
        state.queue_out.put(None)
        socket_manager.stop()
        process_manager.stopped.set()

    if sys.platform == "win32":
        signal.signal(signal.SIGTERM, lambda *_: loop.call_soon_threadsafe(stop))
    else:
        loop.add_signal_handler(signal.SIGTERM, stop)
    try:
        loop.run_until_complete(socket_manager.start())
    finally:
        if worker.is_alive():
            worker.join(timeout=5)
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=5)
        manager.shutdown()
        loop.close()


def _unused_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_health(process, port, timeout=60):
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output, _ = process.communicate()
            raise AssertionError(
                f"服务端提前退出，exit={process.returncode}，日志：\n{output}"
            )
        try:
            with urlopen(url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except URLError:
            time.sleep(0.1)
    raise AssertionError(f"服务端 {timeout}s 内未通过 /health 返回 200，端口 {port}")


async def _complete_task_and_wait_for_server_close(process, port):
    async with websockets.connect(
        f"ws://127.0.0.1:{port}", max_size=None, ping_interval=None
    ) as websocket:
        task_id = "port-restart-task"
        await send_audio(websocket, make_encoded_audio(1.0), task_id=task_id,
                         source="mic", seg_duration=5.0, seg_overlap=0.0)
        messages, closed = await collect_terminal(websocket, task_id=task_id, timeout=20)
        assert not closed
        assert messages[-1]["type"] == "result" and messages[-1]["is_final"] is True, messages

        process.send_signal(signal.SIGTERM)
        await asyncio.wait_for(websocket.wait_closed(), timeout=10)


def _has_time_wait(port):
    if sys.platform.startswith("linux"):
        tables = (Path("/proc/net/tcp"), Path("/proc/net/tcp6"))
        if not any(path.exists() for path in tables):
            return None
        for path in tables:
            if path.exists():
                for line in path.read_text().splitlines()[1:]:
                    fields = line.split()
                    if int(fields[1].rsplit(":", 1)[1], 16) == port and fields[3] == "06":
                        return True
        return False

    netstat = shutil.which("netstat")
    if netstat is None:
        return None
    port_field = re.compile(rf"(?:[:.]){port}(?:\s|$)")
    output = subprocess.run([netstat, "-an"], check=True, capture_output=True, text=True).stdout
    return any("TIME_WAIT" in line and port_field.search(line) for line in output.splitlines())


def _wait_for_time_wait(port):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = _has_time_wait(port)
        if state is True:
            return True
        if state is None:
            return False
        time.sleep(0.1)
    return False


def _stop_service(process):
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
    output, _ = process.communicate(timeout=15)
    return output


def test_time_wait_does_not_block_server_restart():
    if sys.platform == "win32":
        pytest.skip("该重启验收依赖 POSIX SIGTERM 与服务端 TIME_WAIT 状态")
    port = _unused_port()
    first = _start_fake_service(port)
    try:
        _wait_for_health(first, port)
        asyncio.run(_complete_task_and_wait_for_server_close(first, port))
        first_output = first.communicate(timeout=15)[0]
        assert first.returncode == 0, first_output
        if not _wait_for_time_wait(port):
            pytest.skip("当前环境无法确认服务端口处于 TIME_WAIT；未继续判断重启行为")

        second = _start_fake_service(port)
        try:
            _wait_for_health(second, port)
            second_output = _stop_service(second)
            assert second.returncode == 0, second_output
        finally:
            if second.poll() is None:
                _stop_service(second)
    finally:
        if first.poll() is None:
            _stop_service(first)


def test_real_listener_conflict_fails_fast_with_port_in_log():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        process = _start_fake_service(port)
        output, _ = process.communicate(timeout=30)

    assert process.returncode != 0, output
    assert str(port) in output, output


if __name__ == "__main__" and sys.argv[1:] == ["--serve"]:
    _run_fake_service()
