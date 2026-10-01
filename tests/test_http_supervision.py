# coding: utf-8
"""HTTP listener 的监督与启用可见性：真实子进程、真实信号、真实退出码。

覆盖：
  * 显式启用但装配失败（坏数据目录 / 端口冲突）必须以非零退出，不吞初始化错误；
  * 正常 SIGTERM 仍以 0 退出并释放端口；
  * 默认关闭时不创建 HTTP listener（旧 WS 行为不变）；
  * 同目录第二实例竞争失败且退出码明确。
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

# HTTP listener 默认关闭，aiohttp 只在显式启用时安装：缺它就跳过，不假装通过
pytest.importorskip("aiohttp", reason="未安装 aiohttp==3.14.3；HTTP 入口默认关闭")

REPO_ROOT = Path(__file__).resolve().parents[1]

PROBE = '''\
import asyncio, os, signal, sys, threading, time
sys.path.insert(0, "__REPO__")

import core.server.http_server as http_server

_original_serve = http_server.HttpServer.serve


async def announcing_serve(self):
    print("HTTP_LISTENER_READY", flush=True)
    return await _original_serve(self)

http_server.HttpServer.serve = announcing_serve

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