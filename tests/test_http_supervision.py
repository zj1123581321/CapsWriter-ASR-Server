# coding: utf-8
"""HTTP listener 的监督与启用可见性：真实子进程、真实信号、真实退出码。

覆盖：
  * 显式启用但装配失败（坏数据目录 / 端口冲突）必须以非零退出，不吞初始化错误；
  * 正常 SIGTERM 仍以 0 退出并释放端口；
  * 默认关闭时不创建 HTTP listener（旧 WS 行为不变）；
  * 同目录第二实例竞争失败且退出码明确。
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from hashlib import sha256
from pathlib import Path

import pytest

# HTTP listener 默认关闭，aiohttp 只在显式启用时安装：缺它就跳过，不假装通过
pytest.importorskip("aiohttp", reason="未安装 aiohttp==3.14.3；HTTP 入口默认关闭")

REPO_ROOT = Path(__file__).resolve().parents[1]

PROBE = '''\
import asyncio, json, os, signal, sys, threading, time
from pathlib import Path
sys.path.insert(0, "__REPO__")

import core.server.http_server as http_server
from core.server.http_store import HttpStore

_original_serve = http_server.HttpServer.serve


async def announcing_serve(self):
    task = asyncio.create_task(_original_serve(self))
    while self._bound_port is None:
        if task.done():
            return await task
        await asyncio.sleep(0.01)
    print(f"HTTP_LISTENER_READY={self._bound_port}", flush=True)
    return await task

http_server.HttpServer.serve = announcing_serve

if os.environ.get("PROBE_WS_RUNTIME_ERROR"):
    from core.server.connection.server_manager import SocketManager

    async def fail_ws_start(self):
        raise RuntimeError("probe_ws_listener_runtime")

    SocketManager.start = fail_ws_start

if os.environ.get("PROBE_UNKNOWN_HTTP"):
    def fail_job_lookup(self, job_id, token):
        raise RuntimeError("probe_unknown_operation_failure")

    HttpStore.job_record = fail_job_lookup

if os.environ.get("PROBE_CANCELLED_IO_FAILURE"):
    _io_started = threading.Event()
    _io_release = threading.Event()

    def fail_after_cancel(self, job_id, token):
        _io_started.set()
        if not _io_release.wait(10):
            raise RuntimeError("probe_io_release_timeout")
        raise RuntimeError("probe_cancelled_io_failure")

    HttpStore.job_record = fail_after_cancel

    def request_task(path):
        for task in asyncio.all_tasks():
            if task is asyncio.current_task() or task.done():
                continue
            coro = task.get_coro()
            while coro is not None:
                frame = getattr(coro, "cr_frame", None)
                if frame is not None:
                    request = frame.f_locals.get("request")
                    if frame.f_code.co_name == "wrapped" and request is not None and request.path == path:
                        return task
                coro = getattr(coro, "cr_await", None)
        return None

    async def cancel_http_request(path):
        if not await asyncio.to_thread(_io_started.wait, 10):
            raise RuntimeError("probe_io_never_started")
        task = request_task(path)
        while task is None:
            await asyncio.sleep(0)
            task = request_task(path)
        task.cancel()
        print("HTTP_HANDLER_CANCELLED", flush=True)
        await asyncio.sleep(0)
        _io_release.set()

    _original_get_job = http_server.HttpServer._get_job

    async def cancel_before_io_failure(self, request):
        asyncio.create_task(cancel_http_request(request.path))
        return await _original_get_job(self, request)

    http_server.HttpServer._get_job = cancel_before_io_failure

_offset_window = os.environ.get("PROBE_OFFSET_WINDOW")
if _offset_window == "before":
    class OffsetBarrierConnection:
        def __init__(self, connection):
            self.connection = connection
            self.block_once = True

        def execute(self, sql, *args):
            if self.block_once and sql == "BEGIN IMMEDIATE":
                self.block_once = False
                print("FILE_FSYNCED_BEFORE_OFFSET", flush=True)
                threading.Event().wait()
            return self.connection.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    _original_patch = http_server.HttpServer._patch_upload

    async def stop_before_offset_commit(self, request):
        self._store._conn = OffsetBarrierConnection(self._store.conn)
        return await _original_patch(self, request)

    http_server.HttpServer._patch_upload = stop_before_offset_commit
elif _offset_window == "after":
    _original_patch = http_server.HttpServer._patch_upload

    async def stop_after_offset_commit(self, request):
        response = await _original_patch(self, request)
        print("OFFSET_COMMITTED_BEFORE_ACK", flush=True)
        await asyncio.Event().wait()
        return response

    http_server.HttpServer._patch_upload = stop_after_offset_commit

if os.environ.get("PROBE_COMMIT_RESULT"):
    _original_commit = HttpStore.commit_upload
    http_server.HttpServer._inference_ready = lambda self: True

    def commit_and_write_result(self, upload_id, token):
        job = _original_commit(self, upload_id, token)
        result = {
            "task_id": job.job_id, "socket_id": "", "type": "file", "owner_kind": "http",
            "duration": 0.25, "time_start": 1.0, "time_submit": 2.0, "time_complete": 3.0,
            "text": "重启结果", "text_accu": "重启结果", "tokens": ["重启结果"],
            "timestamps": [0.1], "is_final": True,
        }
        self.record_result(job.job_id, result)
        Path(os.environ["PROBE_RESULT_PATH"]).write_text(
            json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        return job

    HttpStore.commit_upload = commit_and_write_result

from core.server.app import CapsWriterServer

server = CapsWriterServer()
server.process_manager.start = lambda: None
server.process_manager.stop = lambda: None
server.process_manager.models_ready = False

async def _never_finishes():
    await asyncio.Event().wait()

server.process_manager.monitor = _never_finishes
print("HTTP_DISABLED" if server.http_server is None else "HTTP_ENABLED_BEFORE_START", flush=True)

if os.environ.get("PROBE_SIGTERM_AFTER"):
    def killer():
        time.sleep(float(os.environ["PROBE_SIGTERM_AFTER"]))
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=killer, daemon=True).start()

server.start()
print(f"EXIT_CODE={server.exit_code}", flush=True)
raise SystemExit(server.exit_code)
'''


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _run_probe(tmp_path: Path, **env_overrides) -> subprocess.Popen:
    script = tmp_path / "probe_http_server.py"
    script.write_text(PROBE.replace("__REPO__", repr(str(REPO_ROOT))), encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "200"}
    env.update(env_overrides)
    return subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        stdin=subprocess.DEVNULL,
    )


def _start_http_process(tmp_path: Path, data_dir: Path, **overrides):
    http_port = _free_port()
    process = _run_probe(
        tmp_path,
        CW_PORT=str(_free_port()),
        CW_ADDR="127.0.0.1",
        CW_HTTP_PORT=str(http_port),
        CW_HTTP_DATA_DIR=str(data_dir),
        **overrides,
    )
    _wait_for(process, "HTTP_LISTENER_READY=", timeout=20)
    return process, http_port


def _create_upload(client, base_url: str, payload: bytes, token: str, key: str) -> str:
    response = client.post(
        base_url + "/v1/uploads",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
        json={"size_bytes": len(payload), "sha256": sha256(payload).hexdigest(), "options": {}},
    )
    assert response.status_code == 201, response.text
    return response.json()["upload_id"]


def _patch_upload(client, base_url: str, upload_id: str, token: str, payload: bytes, offset: int):
    return client.patch(
        f"{base_url}/v1/uploads/{upload_id}",
        headers={"Authorization": f"Bearer {token}", "Content-Length": str(len(payload)),
                 "Upload-Offset": str(offset), "Content-Type": "application/octet-stream"},
        content=payload,
    )


def _wait_for(process: subprocess.Popen, needle: str, timeout: float = 60.0) -> str:
    deadline = time.time() + timeout
    collected: list[str] = []
    assert process.stdout is not None
    while time.time() < deadline:
        line = process.stdout.readline()
        if line == "":
            break
        collected.append(line)
        if needle in line:
            return "".join(collected)
    process.kill()
    raise AssertionError(f"未等到 {needle}；已输出：{''.join(collected)}")


def _run_bare_probe(tmp_path: Path, **env_overrides) -> subprocess.Popen:
    """无 PI/DELEGATE 身份的正式 CapsWriterServer 子进程。"""
    script = tmp_path / "probe_http_server.py"
    script.write_text(PROBE.replace("__REPO__", repr(str(REPO_ROOT))), encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "PYTHONPATH": str(REPO_ROOT),
        "LANG": "C.UTF-8",
        "NO_COLOR": "1",
        "TERM": "dumb",
        "COLUMNS": "200",
    }
    env.update(env_overrides)
    return subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        stdin=subprocess.DEVNULL,
    )


def _read_http_from_socket(sock: socket.socket, timeout: float) -> bytes:
    sock.settimeout(timeout)
    chunks = []
    try:
        while True:
            piece = sock.recv(4096)
            if not piece:
                break
            chunks.append(piece)
            data = b"".join(chunks)
            header_end = data.find(b"\r\n\r\n")
            if header_end < 0:
                continue
            header = data[:header_end]
            length = 0
            for line in header.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1].strip())
            if len(data) - header_end - 4 >= length:
                return data
    except socket.timeout as exc:
        raise AssertionError(f"真实进程未在期限内返回 HTTP 响应：{exc}") from exc
    raise AssertionError(f"真实进程连接在完整响应前关闭：{b''.join(chunks)!r}")


def test_http_is_off_by_default_and_sigterm_still_exits_zero(tmp_path):
    ws_port = _free_port()
    process = _run_probe(tmp_path, CW_PORT=str(ws_port), CW_ADDR="127.0.0.1",
                         PROBE_SIGTERM_AFTER="3")
    try:
        _wait_for(process, "HTTP_DISABLED")
        assert process.wait(timeout=60) == 0
        stdout = process.stdout.read()
        assert "HTTP_LISTENER_READY" not in stdout
        assert "EXIT_CODE=0" in stdout
    finally:
        if process.poll() is None:
            process.kill()
    # WS 端口已释放
    with socket.socket() as sock:
        sock.settimeout(2)
        with pytest.raises(OSError):
            sock.connect(("127.0.0.1", ws_port))


def test_enabled_http_serves_and_sigterm_exits_zero_releasing_port(tmp_path):
    ws_port = _free_port()
    http_port = _free_port()
    process = _run_probe(tmp_path, CW_PORT=str(ws_port), CW_ADDR="127.0.0.1",
                         CW_HTTP_PORT=str(http_port), CW_HTTP_DATA_DIR=str(tmp_path / "data"),
                         PROBE_SIGTERM_AFTER="3")
    try:
        _wait_for(process, "HTTP_LISTENER_READY")
        import httpx

        response = httpx.get(f"http://127.0.0.1:{http_port}/v1/jobs/unknown",
                             headers={"Authorization": "Bearer probe-token"}, timeout=10)
        assert response.status_code == 404
        assert process.wait(timeout=60) == 0
        assert "EXIT_CODE=0" in process.stdout.read()
    finally:
        if process.poll() is None:
            process.kill()
    with socket.socket() as sock:
        sock.settimeout(2)
        with pytest.raises(OSError):
            sock.connect(("127.0.0.1", http_port))


def test_bad_data_dir_fails_fast_with_nonzero_exit(tmp_path):
    """显式启用但存储初始化失败：真实非零退出，且错误可见。"""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    process = _run_probe(tmp_path, CW_PORT=str(_free_port()), CW_ADDR="127.0.0.1",
                         CW_HTTP_PORT=str(_free_port()), CW_HTTP_DATA_DIR=str(blocked / "state"))
    stdout, stderr = process.communicate(timeout=60)
    assert process.returncode != 0
    assert "HTTP 存储初始化失败" in stderr or "NotADirectoryError" in stderr
    assert "EXIT_CODE=0" not in stdout


def test_second_instance_on_same_data_dir_fails_fast(tmp_path):
    """同目录第二实例：OS 独占锁失败必须真实非零退出，而不是删锁猜活跃。"""
    data_dir = tmp_path / "data"
    first = _run_probe(tmp_path, CW_PORT=str(_free_port()), CW_ADDR="127.0.0.1",
                       CW_HTTP_PORT=str(_free_port()), CW_HTTP_DATA_DIR=str(data_dir))
    try:
        _wait_for(first, "HTTP_LISTENER_READY")
        second = _run_probe(tmp_path, CW_PORT=str(_free_port()), CW_ADDR="127.0.0.1",
                            CW_HTTP_PORT=str(_free_port()), CW_HTTP_DATA_DIR=str(data_dir))
        stdout, stderr = second.communicate(timeout=60)
        assert second.returncode != 0
        assert "data_dir_locked" in stderr
        assert "EXIT_CODE=0" not in stdout
    finally:
        if first.poll() is None:
            first.send_signal(signal.SIGTERM)
            try:
                first.wait(timeout=60)
            except subprocess.TimeoutExpired:  # pragma: no cover
                first.kill()


def test_http_port_conflict_fails_fast(tmp_path):
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    taken = holder.getsockname()[1]
    try:
        process = _run_probe(tmp_path, CW_PORT=str(_free_port()), CW_ADDR="127.0.0.1",
                             CW_HTTP_PORT=str(taken), CW_HTTP_DATA_DIR=str(tmp_path / "data2"))
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode != 0
        assert "HTTP 监听失败" in stderr
        assert "EXIT_CODE=0" not in stdout
    finally:
        holder.close()


def test_unknown_http_operation_stops_listener_and_exits_nonzero(tmp_path):
    ws_port = _free_port()
    http_port = _free_port()
    process = _run_probe(tmp_path, CW_PORT=str(ws_port), CW_ADDR="127.0.0.1",
                         CW_HTTP_PORT=str(http_port), CW_HTTP_DATA_DIR=str(tmp_path / "fatal-http"),
                         PROBE_UNKNOWN_HTTP="1")
    try:
        _wait_for(process, "HTTP_LISTENER_READY=")
        import httpx

        response = httpx.get(f"http://127.0.0.1:{http_port}/v1/jobs/probe",
                             headers={"Authorization": "Bearer probe-token"}, timeout=10)
        assert response.status_code == 500
        deadline = time.monotonic() + 5
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert process.poll() is not None, "未知 HTTP operation 后服务仍存活"
        assert process.returncode != 0
    finally:
        if process.poll() is None:
            process.kill()
    with socket.socket() as sock:
        sock.settimeout(2)
        with pytest.raises(OSError):
            sock.connect(("127.0.0.1", http_port))


def test_cancelled_http_handler_io_failure_reaches_process_supervisor(tmp_path):
    http_port = _free_port()
    process = _run_probe(
        tmp_path,
        CW_PORT=str(_free_port()),
        CW_ADDR="127.0.0.1",
        CW_HTTP_PORT=str(http_port),
        CW_HTTP_DATA_DIR=str(tmp_path / "cancelled-io"),
        PROBE_CANCELLED_IO_FAILURE="1",
    )
    try:
        output = _wait_for(process, "HTTP_LISTENER_READY=")
        import httpx

        try:
            httpx.get(f"http://127.0.0.1:{http_port}/v1/jobs/probe",
                      headers={"Authorization": "Bearer probe-token"}, timeout=10)
        except httpx.HTTPError:
            pass
        deadline = time.monotonic() + 5
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        exited_after_failure = process.poll() is not None
        if not exited_after_failure:
            process.kill()
        stdout, stderr = process.communicate(timeout=5)
        assert exited_after_failure, stdout + stderr
        assert process.returncode != 0
        assert "HTTP_HANDLER_CANCELLED" in output + stdout
        assert "probe_cancelled_io_failure" in stdout + stderr
    finally:
        if process.poll() is None:
            process.kill()


def test_websocket_runtime_error_exits_nonzero(tmp_path):
    process = _run_probe(tmp_path, CW_PORT=str(_free_port()), CW_ADDR="127.0.0.1",
                         PROBE_WS_RUNTIME_ERROR="1")
    stdout, stderr = process.communicate(timeout=20)
    assert process.returncode != 0, stdout
    assert "EXIT_CODE=0" not in stdout


@pytest.mark.skipif(os.name != "posix", reason="进程崩溃窗口探针只在 POSIX 上执行")
def test_http_offset_crash_windows_recover_in_new_processes(tmp_path):
    import httpx

    data_dir = tmp_path / "crash-window-data"
    token = "crash-window-token"
    payload = bytes((index * 11 + 7) % 251 for index in range(4096))
    prefix_size = 2048
    unconfirmed_tail = bytes((byte + 1) % 251 for byte in payload)
    process, port = _start_http_process(tmp_path, data_dir, PROBE_OFFSET_WINDOW="before")
    before_upload = None
    before_ack = []
    with httpx.Client(trust_env=False, timeout=30) as client:
        before_upload = _create_upload(client, f"http://127.0.0.1:{port}", payload, token, "before-offset")

        def send_unconfirmed_prefix():
            try:
                before_ack.append(_patch_upload(
                    client, f"http://127.0.0.1:{port}", before_upload, token,
                    unconfirmed_tail, 0,
                ))
            except httpx.HTTPError as exc:
                before_ack.append(exc)

        request_thread = threading.Thread(target=send_unconfirmed_prefix, daemon=True)
        request_thread.start()
        _wait_for(process, "FILE_FSYNCED_BEFORE_OFFSET", timeout=15)
        process.kill()
        process.communicate(timeout=10)
        request_thread.join(timeout=10)
        assert not request_thread.is_alive()
        assert before_ack and not isinstance(before_ack[0], httpx.Response)

    process, port = _start_http_process(tmp_path, data_dir)
    try:
        with httpx.Client(trust_env=False, timeout=10) as client:
            base_url = f"http://127.0.0.1:{port}"
            recovered = client.get(
                f"{base_url}/v1/uploads/{before_upload}",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert recovered.status_code == 200
            assert recovered.json()["confirmed_offset"] == 0
            prefix_path = data_dir / "sources" / f"{before_upload}.bin"
            assert prefix_path.read_bytes() == unconfirmed_tail
            assert sha256(prefix_path.read_bytes()).hexdigest() == sha256(unconfirmed_tail).hexdigest()

            repaired_prefix = _patch_upload(
                client, base_url, before_upload, token, payload[:prefix_size], 0)
            assert repaired_prefix.status_code == 204
            assert repaired_prefix.headers["Upload-Offset"] == str(prefix_size)
            assert prefix_path.read_bytes() == payload[:prefix_size]
            assert sha256(prefix_path.read_bytes()).hexdigest() == sha256(payload[:prefix_size]).hexdigest()
            repaired_suffix = _patch_upload(
                client, base_url, before_upload, token, payload[prefix_size:], prefix_size)
            assert repaired_suffix.status_code == 204
            confirmed = client.get(
                f"{base_url}/v1/uploads/{before_upload}",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert confirmed.json()["confirmed_offset"] == len(payload)
            assert sha256(prefix_path.read_bytes()).hexdigest() == sha256(payload).hexdigest()
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            assert process.wait(timeout=15) == 0
            process.communicate(timeout=5)

    after_payload = b"offset-before-ack" * 128
    process, port = _start_http_process(tmp_path, data_dir, PROBE_OFFSET_WINDOW="after")
    after_upload = None
    after_ack = []
    with httpx.Client(trust_env=False, timeout=30) as client:
        after_upload = _create_upload(client, f"http://127.0.0.1:{port}",
                                      after_payload, token, "after-offset")

        def send_before_ack():
            try:
                after_ack.append(_patch_upload(
                    client, f"http://127.0.0.1:{port}", after_upload, token, after_payload, 0,
                ))
            except httpx.HTTPError as exc:
                after_ack.append(exc)

        request_thread = threading.Thread(target=send_before_ack, daemon=True)
        request_thread.start()
        _wait_for(process, "OFFSET_COMMITTED_BEFORE_ACK", timeout=15)
        process.kill()
        process.communicate(timeout=10)
        request_thread.join(timeout=10)
        assert not request_thread.is_alive()
        assert after_ack and not isinstance(after_ack[0], httpx.Response)

    process, port = _start_http_process(tmp_path, data_dir)
    try:
        with httpx.Client(trust_env=False, timeout=10) as client:
            base_url = f"http://127.0.0.1:{port}"
            recovered = client.get(
                f"{base_url}/v1/uploads/{after_upload}",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert recovered.status_code == 200
            assert recovered.json()["confirmed_offset"] == len(after_payload)
            source_path = data_dir / "sources" / f"{after_upload}.bin"
            assert source_path.read_bytes() == after_payload
            assert sha256(source_path.read_bytes()).hexdigest() == sha256(after_payload).hexdigest()
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            assert process.wait(timeout=15) == 0
            process.communicate(timeout=5)


@pytest.mark.skipif(os.name != "posix", reason="真实子进程重启探针只在 POSIX 上执行")
def test_result_producer_payload_and_done_survive_new_process(tmp_path):
    import httpx

    data_dir = tmp_path / "result-restart-data"
    producer_path = tmp_path / "result-producer.json"
    token = "result-restart-token"
    payload = b"result restart source"
    process, port = _start_http_process(
        tmp_path, data_dir, PROBE_COMMIT_RESULT="1", PROBE_RESULT_PATH=str(producer_path),
    )
    with httpx.Client(trust_env=False, timeout=10) as client:
        base_url = f"http://127.0.0.1:{port}"
        upload_id = _create_upload(client, base_url, payload, token, "result-restart")
        patch = _patch_upload(client, base_url, upload_id, token, payload, 0)
        assert patch.status_code == 204
        committed = client.post(
            f"{base_url}/v1/uploads/{upload_id}/commit",
            headers={"Authorization": f"Bearer {token}", "Content-Length": "0"}, content=b"",
        )
        assert committed.status_code == 202
        job_id = committed.json()["job_id"]
        producer_payload = json.loads(producer_path.read_text(encoding="utf-8"))
        assert producer_payload["task_id"] == job_id
        assert producer_payload["is_final"] is True
    process.kill()
    process.communicate(timeout=10)

    process, port = _start_http_process(tmp_path, data_dir)
    try:
        with httpx.Client(trust_env=False, timeout=10) as client:
            base_url = f"http://127.0.0.1:{port}"
            status = client.get(
                f"{base_url}/v1/jobs/{job_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            result = client.get(
                f"{base_url}/v1/jobs/{job_id}/result",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert status.status_code == 200
            assert status.json()["state"] == "DONE"
            assert result.status_code == 200
            assert result.json() == producer_payload
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            assert process.wait(timeout=15) == 0
            process.communicate(timeout=5)


def test_real_process_body_idle_timeout_is_not_fatal_and_releases_port(tmp_path):
    """正式全量进程消费既有 CW_UPLOAD_IDLE_SECONDS：半开 body 408 后仍可 GET，SIGTERM 0。"""
    data_dir = tmp_path / "data"
    ws_port = _free_port()
    http_port = _free_port()
    process = _run_bare_probe(
        tmp_path,
        CW_PORT=str(ws_port),
        CW_ADDR="127.0.0.1",
        CW_HTTP_PORT=str(http_port),
        CW_HTTP_DATA_DIR=str(data_dir),
        CW_UPLOAD_IDLE_SECONDS="0.4",
    )
    sock = None
    try:
        _wait_for(process, "HTTP_LISTENER_READY=")
        sock = socket.create_connection(("127.0.0.1", http_port), timeout=2)
        sock.sendall((
            f"POST /v1/uploads HTTP/1.1\r\nHost: 127.0.0.1:{http_port}\r\n"
            "Authorization: Bearer process-idle\r\nIdempotency-Key: process-idle\r\n"
            "Content-Type: application/json\r\nContent-Length: 80\r\n\r\n"
        ).encode())
        raw = _read_http_from_socket(sock, timeout=2.0)
        assert b" 408 " in raw.split(b"\r\n", 1)[0], raw
        payload = json.loads(raw.split(b"\r\n\r\n", 1)[1])
        assert payload["code"] == "request_timeout"
        assert payload["request_id"]
        import httpx
        response = httpx.get(
            f"http://127.0.0.1:{http_port}/v1/jobs/missing",
            headers={"Authorization": "Bearer process-idle"},
            timeout=5,
        )
        assert response.status_code == 404
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=60) == 0
        leftover = process.stdout.read()
        assert "EXIT_CODE=0" in leftover
    finally:
        if sock is not None:
            sock.close()
        if process.poll() is None:
            process.kill()
    with socket.socket() as released:
        released.settimeout(2)
        with pytest.raises(OSError):
            released.connect(("127.0.0.1", http_port))
