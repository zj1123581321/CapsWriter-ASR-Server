# coding: utf-8
"""HTTP 终态源清理：真实 listener、I/O worker、runner 与隔离 TCP 客户端。"""
from __future__ import annotations

import asyncio
import hashlib
import queue
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from core.server import http_file_runner as runner_module
from core.server import http_server as server_module
from core.server.http_server import HttpServer
from core.server.schema import Result

pytest.importorskip("aiohttp", reason="HTTP 入口测试需要 aiohttp==3.14.3")


class _StubApp:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.state = SimpleNamespace(
            tasks={},
            active_http_jobs=[],
            queue_in=queue.Queue(),
        )


async def _wait_until(predicate, message: str, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    assert predicate(), message


def _read_db(data_dir: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(f"file:{data_dir / 'http.sqlite3'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


async def _create_job(client: httpx.AsyncClient, base_url: str, payload: bytes):
    token = "cleanup-test-token"
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": "cleanup-test-key",
    }
    created = await client.post(
        f"{base_url}/v1/uploads",
        headers=headers,
        json={"size_bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()},
    )
    assert created.status_code == 201, created.text
    upload_id = created.json()["upload_id"]
    patched = await client.patch(
        f"{base_url}/v1/uploads/{upload_id}",
        headers={**headers, "Upload-Offset": "0"},
        content=payload,
    )
    assert patched.status_code == 204
    assert patched.headers["Upload-Offset"] == str(len(payload))
    committed = await client.post(
        f"{base_url}/v1/uploads/{upload_id}/commit",
        headers=headers,
        content=b"",
    )
    assert committed.status_code == 202, committed.text
    return token, headers, created.json(), committed.json()


@pytest.mark.asyncio
async def test_periodic_cleanup_waits_for_real_runner_reference_then_keeps_result(
    tmp_path, monkeypatch
):
    """终态落库后 decoder.close 阻塞期间保留源；释放 runner 后由周期任务删除。"""
    monkeypatch.setattr(server_module, "SOURCE_CLEANUP_INTERVAL_SECONDS", 0.02, raising=False)
    app = _StubApp()
    server = HttpServer(app, "127.0.0.1", 0, tmp_path / "httpdata").prepare()
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    decoder_payload = b"\x00\x00\x80\x3f" * 4

    class _Decoder:
        def __init__(self, _path):
            self.samples_emitted = 0

        async def start(self):
            return self

        async def pcm_chunks(self):
            self.samples_emitted += len(decoder_payload) // 4
            yield decoder_payload

        async def finish(self):
            return None

        def kill(self):
            return None

        async def close(self):
            close_started.set()
            await release_close.wait()

    monkeypatch.setattr(runner_module, "ffmpeg_path", lambda: "/test/ffmpeg")
    monkeypatch.setattr(runner_module, "FileSourceDecoder", _Decoder)
    runner = runner_module.HttpFileRunner(app.state, server)
    server.attach_runner(runner)
    app.state.http_result_sink = runner.result_sink
    serve_task = asyncio.create_task(server.serve())
    source_path = None
    headers = None
    job_id = None
    try:
        await _wait_until(lambda: server._bound_port is not None, "HTTP listener 未启动")
        base_url = f"http://127.0.0.1:{server._bound_port}"
        payload = b"registered-source-bytes"
        source_bytes = payload
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=5) as client:
            _token, headers, created, committed = await _create_job(client, base_url, payload)
            upload_id = created["upload_id"]
            job_id = committed["job_id"]
            source_path = server.data_dir / "sources" / f"{upload_id}.bin"
            assert source_path.read_bytes() == source_bytes

            submitted = await asyncio.wait_for(
                asyncio.to_thread(app.state.queue_in.get, True, 5), timeout=5
            )
            assert submitted.task_id == job_id
            assert submitted.owner_kind == "http"
            assert submitted.socket_id == ""
            assert submitted.data == decoder_payload
            text = "清理后仍可领取"
            result = Result(
                task_id=job_id,
                socket_id="",
                type="file",
                duration=1.25,
                time_start=10.0,
                time_submit=20.0,
                time_complete=30.0,
                text=text,
                text_accu=text,
                tokens=list(text),
                timestamps=[float(index) / 10 for index in range(len(text))],
                is_final=True,
                owner_kind="http",
            )
            await app.state.http_result_sink(result)
            await asyncio.wait_for(close_started.wait(), timeout=5)
            assert job_id in runner.active_jobs
            row = _read_db(
                server.data_dir,
                "SELECT state, terminal_at FROM jobs WHERE job_id=?",
                (job_id,),
            )[0]
            assert row["state"] == "DONE" and row["terminal_at"] is not None
            terminal_at = time.time() - 7 * 24 * 3600 - 1
            server._worker.run_sync(
                lambda: server._store.conn.execute(
                    "UPDATE jobs SET terminal_at=? WHERE job_id=?",
                    (terminal_at, job_id),
                )
            )
            # 真实 SQLite 终态已进入七天之外；清理仍须受 runner 的持有引用保护。
            row = _read_db(
                server.data_dir,
                "SELECT state, terminal_at FROM jobs WHERE job_id=?",
                (job_id,),
            )[0]
            assert row["state"] == "DONE" and row["terminal_at"] == terminal_at
            await asyncio.sleep(0.12)
            assert source_path.exists(), "周期清理在 decoder.close 释放前删除了仍被 runner 引用的源"
            assert job_id in runner.active_jobs

            release_close.set()
            await asyncio.wait_for(runner._jobs[job_id], timeout=5)
            await _wait_until(
                lambda: not source_path.exists(),
                "runner 释放引用后的下一轮周期清理没有删除已到期源",
            )
            assert _read_db(
                server.data_dir,
                "SELECT state, error_code, terminal_at FROM jobs WHERE job_id=?",
                (job_id,),
            )[0]["state"] == "DONE"
            assert _read_db(
                server.data_dir,
                "SELECT COUNT(*) AS n FROM results WHERE job_id=?",
                (job_id,),
            )[0]["n"] == 1
            fetched = await client.get(f"{base_url}/v1/jobs/{job_id}", headers=headers)
            assert fetched.status_code == 200
            assert fetched.json()["source_available"] is False
            assert fetched.json()["result_available"] is True
            result_response = await client.get(
                f"{base_url}/v1/jobs/{job_id}/result", headers=headers
            )
            assert result_response.status_code == 200
            assert result_response.json() == {
                "task_id": job_id,
                "type": "file",
                "socket_id": "",
                "owner_kind": "http",
                "is_final": True,
                "duration": 1.25,
                "time_start": 10.0,
                "time_submit": 20.0,
                "time_complete": 30.0,
                "text": text,
                "text_accu": text,
                "tokens": list(text),
                "timestamps": [float(index) / 10 for index in range(len(text))],
            }
    finally:
        release_close.set()
        server._fatal_event.set()
        await asyncio.gather(serve_task, return_exceptions=True)
