# coding: utf-8
"""HTTP 文件任务：真实 aiohttp TCP 六 route + 真实 SDK 的跨边界契约测试。

消费者一律是已合入的 SDK（sdk/capswriter_asr），不在消费侧自造 dict；
断言服务端真实落盘字节、数据库前缀与恢复文件内容。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import threading
from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "sdk"))

from capswriter_asr import AsrError, resume_file_http, submit_file_http  # noqa: E402
from capswriter_asr import http_client as sdk_http  # noqa: E402

from config_server import ServerConfig  # noqa: E402
from core.server.http_server import HttpServer  # noqa: E402
from core.server.http_store import HttpStoreError  # noqa: E402
# HTTP listener 默认关闭，aiohttp 只在显式启用时安装：缺它就跳过，不假装通过
pytest.importorskip("aiohttp", reason="未安装 aiohttp==3.14.3；HTTP 入口默认关闭")


class _StubApp:
    """HttpServer 只用到 app.loop 与 app.state（共享预算计数），这里给出真实引用。"""

    def __init__(self, loop):
        self.loop = loop
        self.state = SimpleNamespace(tasks={})



def _read_db(data_dir: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    """独立只读连接读真实落盘的 SQLite 文件（WAL 提交即见），不借用服务端连接。"""
    conn = sqlite3.connect(f"file:{data_dir / 'http.sqlite3'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


async def _async_chunks(*chunks: bytes):
    """真分块发送（无 Content-Length），验证服务端拒绝未声明长度的请求体。"""
    for chunk in chunks:
        yield chunk


@asynccontextmanager
async def running_server(tmp_path: Path, inference: bool = False, pause_hook=None):
    """在 port 0 上起真实 aiohttp listener，退出时收尾。"""
    loop = asyncio.get_running_loop()
    server = HttpServer(_StubApp(loop), "127.0.0.1", 0, tmp_path / "httpdata")
    # 只切换「协调者是否已装配」这一个开关，不替换被测实现
    server.inference_available = inference
    server.prepare()
    if pause_hook is not None:
        original = server._store.append_bytes

        def hooked(*args, **kwargs):
            pause_hook()
            return original(*args, **kwargs)

        server._store.append_bytes = hooked
    await server._runner.setup()
    site = server._web.TCPSite(server._runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    server._site = site
    try:
        yield server, f"http://127.0.0.1:{port}"
    finally:
        await server.stop()


def _source(tmp_path: Path, name: str = "sample.wav", size: int = 4096) -> Path:
    path = tmp_path / name
    payload = bytes((index * 7 + 13) % 251 for index in range(size))
    path.write_bytes(payload)
    return path


async def _expect_error(coro, code: str) -> AsrError:
    with pytest.raises(AsrError) as info:
        await coro
    assert info.value.code == code, info.value.code
    return info.value


async def _find_http_handler_task(path: str) -> asyncio.Task:
    """取真实 aiohttp route task；不把客户端请求 task 当服务端 handler。"""
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
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
        await asyncio.sleep(0.005)
    raise AssertionError(f"没有找到真实 aiohttp handler task：{path}")


async def _wait_for_worker_pending(worker, expected: int) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    while len(worker._pending) != expected and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.005)
    assert len(worker._pending) == expected


async def _read_raw_http_response(reader: asyncio.StreamReader, timeout: float) -> tuple[str, bytes]:
    """读完整 HTTP 响应；超时或半截必须变成 AssertionError，不能盲等挂死。"""
    try:
        header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=timeout)
        text = header.decode("iso-8859-1")
        status = text.split("\r\n", 1)[0]
        length = 0
        for line in text.split("\r\n"):
            if line.lower().startswith("content-length:"):
                length = int(line.split(":", 1)[1].strip())
        body = await asyncio.wait_for(reader.readexactly(length), timeout=timeout) if length else b""
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError) as exc:
        raise AssertionError(f"服务端未在期限内返回完整 HTTP 响应：{exc}") from exc
    return status, body


async def _hang_http_body(host: str, port: int, headers: bytes, prefix: bytes, timeout: float):
    """发送声明了长度的请求头与可选前缀后停住，对端不关闭。"""
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(headers + prefix)
    await writer.drain()
    try:
        status, body = await _read_raw_http_response(reader, timeout)
    except Exception:
        writer.close()
        raise
    return reader, writer, status, body


@pytest.mark.asyncio
async def test_io_mailbox_refuses_33rd_and_cancel_keeps_slot_until_future_done(tmp_path):
    async with running_server(tmp_path) as (server, _base_url):
        worker = server._worker
        started, release = threading.Event(), threading.Event()

        def blocked_operation():
            started.set()
            if not release.wait(5):
                raise RuntimeError("test I/O barrier timed out")
            return "done"

        calls = [asyncio.create_task(worker.run(blocked_operation))]
        assert await asyncio.to_thread(started.wait, 5)
        calls.extend(asyncio.create_task(worker.run(lambda: "queued")) for _ in range(31))
        try:
            await _wait_for_worker_pending(worker, 32)
            overflow = asyncio.create_task(worker.run(lambda: "must-reject"))
            await asyncio.sleep(0)
            assert overflow.done()
            error = overflow.exception()
            assert isinstance(error, HttpStoreError)
            assert error.status == 429

            calls[0].cancel()
            cancelled = await asyncio.gather(calls[0], return_exceptions=True)
            assert isinstance(cancelled[0], asyncio.CancelledError)
            assert len(worker._pending) == 32
            assert worker._mailbox._value == 0
        finally:
            release.set()
            await asyncio.gather(*calls, return_exceptions=True)
        await _wait_for_worker_pending(worker, 0)
        assert worker._mailbox._value == 32


@pytest.mark.asyncio
async def test_sdk_upload_reaches_disk_then_commit_is_explicitly_unavailable(tmp_path):
    """真实 SDK 提交：字节真正落盘、offset 可信，但无真实 runner 时 commit 明确 503。"""
    source = _source(tmp_path)
    recovery = tmp_path / "resume.json"
    async with running_server(tmp_path) as (server, base_url):
        error = await _expect_error(
            submit_file_http(source, base_url, resume_path=recovery, chunk_bytes=1024),
            "inference_unavailable",
        )
        assert error.recovery_path == recovery
        # 恢复文件里必须带 upload_id 与完整 confirmed_offset，客户端可显式继续
        stored = json.loads(recovery.read_text(encoding="utf-8"))
        assert stored["confirmed_offset"] == source.stat().st_size
        upload_id = stored["upload_id"]
        # 服务端文件字节与源文件逐字节一致（真实 producer 的落盘结果）
        sources = list((server.data_dir / "sources").iterdir())
        assert len(sources) == 1
        assert sources[0].read_bytes() == source.read_bytes()
        assert sha256(sources[0].read_bytes()).hexdigest() == stored["sha256"]
        assert sources[0].name == f"{upload_id}.bin"
        # 数据库里 confirmed_offset 同样是完整长度
        row = _read_db(
            server.data_dir,
            "SELECT state, confirmed_offset, size_bytes FROM uploads WHERE upload_id=?",
            (upload_id,),
        )[0]
        assert row["state"] == "UPLOADING"
        assert row["confirmed_offset"] == row["size_bytes"] == source.stat().st_size


@pytest.mark.asyncio
async def test_repeated_create_key_is_idempotent_and_conflict_never_overwrites(tmp_path):
    """同 key 同身份 200 同 upload_id；同 key 不同内容 409 且旧字节不变。"""
    source = _source(tmp_path)
    recovery = tmp_path / "resume.json"
    async with running_server(tmp_path) as (server, base_url):
        await _expect_error(
            submit_file_http(source, base_url, resume_path=recovery, chunk_bytes=4096),
            "inference_unavailable",
        )
        stored = json.loads(recovery.read_text(encoding="utf-8"))
        headers = {"Authorization": f"Bearer {stored['token']}"}
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            body = {
                "size_bytes": source.stat().st_size,
                "sha256": stored["sha256"],
                "options": stored["options"],
            }
            again = await client.post(
                base_url + "/v1/uploads", headers={**headers, "Idempotency-Key": stored["create_key"]}, json=body
            )
            assert again.status_code == 200, again.text
            assert again.json()["upload_id"] == stored["upload_id"]
            assert again.json()["confirmed_offset"] == stored["confirmed_offset"]
            assert again.json()["expires_at"].endswith("Z")
            conflict = await client.post(
                base_url + "/v1/uploads",
                headers={**headers, "Idempotency-Key": stored["create_key"]},
                json={**body, "size_bytes": body["size_bytes"] + 1},
            )
            assert conflict.status_code == 409
            assert conflict.json()["code"] == "idempotency_conflict"
        before = (server.data_dir / "sources" / f"{stored['upload_id']}.bin").read_bytes()
        assert before == source.read_bytes()


@pytest.mark.asyncio
async def test_resume_after_failed_patch_only_sends_unconfirmed_suffix(tmp_path):
    """中断后 resume 只补服务端确认位置之后的字节，落盘文件仍与源文件一致。"""
    source = _source(tmp_path, size=6000)
    recovery = tmp_path / "resume.json"
    calls = {"n": 0}

    async with running_server(tmp_path, inference=True) as (server, base_url):
        original = server._store.append_bytes

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("模拟 PATCH 中途 I/O 失败")
            return original(*args, **kwargs)

        server._store.append_bytes = flaky
        await _expect_error(
            submit_file_http(source, base_url, resume_path=recovery, chunk_bytes=2048),
            "internal_error",
        )
        partial = json.loads(recovery.read_text(encoding="utf-8"))
        assert partial["confirmed_offset"] == 2048
        server._store.append_bytes = original

        sent = {"bytes": 0}
        real_client = sdk_http._request

        async def counting_request(client, method, url, **kwargs):
            if method == "PATCH":
                sent["bytes"] += len(kwargs.get("content") or b"")
            return await real_client(client, method, url, **kwargs)

        sdk_http._request = counting_request
        try:
            handle = await resume_file_http(source, base_url, resume_path=recovery)
        finally:
            sdk_http._request = real_client

        assert handle.state == "QUEUED"
        assert handle.job_id is not None
        # 只重传了未确认的后缀，不是整份重发
        assert sent["bytes"] == source.stat().st_size - 2048
        stored = json.loads(recovery.read_text(encoding="utf-8"))
        disk = (server.data_dir / "sources" / f"{stored['upload_id']}.bin").read_bytes()
        assert disk == source.read_bytes()
        states = _read_db(
            server.data_dir, "SELECT state FROM uploads WHERE upload_id=?", (stored["upload_id"],)
        )
        assert states[0]["state"] == "COMMITTED"
        assert len(_read_db(server.data_dir, "SELECT job_id FROM jobs")) == 1


@pytest.mark.asyncio
async def test_http_admission_shares_ws_budget(tmp_path):
    """F4：HTTP 准入与 WS 共用 max_tasks=8 的总量预算（R7 规格）。

    WS 活动任务占满共享预算时 HTTP commit 必须 429 too_many_jobs（复用既有
    口径，不发明新错误码）；7 个 WS 时同一上传必须被受理——边界随共享计数
    移动。计数以内存 state.tasks 为准（与 ws_recv 的 overloaded 判定同一份）。
    """
    from core.server.state import TaskLifecycle, make_task_key

    source = _source(tmp_path)
    async with running_server(tmp_path, inference=True) as (server, base_url):
        tasks = server._app.state.tasks
        # 7 个 WS 活动任务：共享预算未满，HTTP 必须被受理
        for index in range(7):
            tasks[make_task_key("ws", f"ws-task-{index}", f"socket-{index}")] = (
                TaskLifecycle()
            )
        recovery = tmp_path / "resume7.json"
        handle = await submit_file_http(
            source, base_url, resume_path=recovery, chunk_bytes=1024
        )
        assert handle.job_id, "7 个 WS + 0 个 HTTP 时共享预算未满，commit 必须受理"

        # 第 8 个 WS 占满共享预算：HTTP 再收就会双向突破 max_tasks
        tasks[make_task_key("ws", "ws-task-7", "socket-7")] = TaskLifecycle()
        error = await _expect_error(
            submit_file_http(source, base_url, resume_path=tmp_path / "resume8.json", chunk_bytes=1024),
            "too_many_jobs",
        )
        assert error.code == "too_many_jobs"
        # 被拒的 commit 不留 Job 行（受理失败即不建立任务）
        assert len(_read_db(server.data_dir, "SELECT job_id FROM jobs")) == 1


@pytest.mark.asyncio
async def test_job_lifecycle_repeated_commit_and_result_not_ready(tmp_path):
    """唯一 Job：重复 commit 仍返回同一 Job；无持久结果时 409 result_not_ready。"""
    source = _source(tmp_path, size=2048)
    recovery = tmp_path / "resume.json"
    async with running_server(tmp_path, inference=True) as (_server, base_url):
        handle = await submit_file_http(source, base_url, resume_path=recovery, chunk_bytes=1024)
        assert handle.state == "QUEUED"
        stored = json.loads(recovery.read_text(encoding="utf-8"))
        headers = {"Authorization": f"Bearer {stored['token']}"}
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            repeat = await client.post(f"{base_url}/v1/uploads/{handle.upload_id}/commit", headers=headers, content=b"")
            assert repeat.status_code == 200
            assert repeat.json()["job_id"] == handle.job_id
            assert repeat.json()["state"] == "QUEUED"
            job = await client.get(f"{base_url}/v1/jobs/{handle.job_id}", headers=headers)
            assert job.status_code == 200
            payload = job.json()
            assert payload["source_available"] is True
            assert payload["result_available"] is False
            assert payload["error_code"] is None
            assert isinstance(payload["time_submit"], (int, float))
            not_ready = await client.get(f"{base_url}/v1/jobs/{handle.job_id}/result", headers=headers)
            assert not_ready.status_code == 409
            assert not_ready.json()["code"] == "result_not_ready"
            # 每个 error body 都必须带 request_id，不泄露令牌
            assert not_ready.json()["request_id"]
            assert stored["token"] not in json.dumps(not_ready.json())
            # GET /v1/uploads/{id} 可找回同一 Job
            upload = await client.get(f"{base_url}/v1/uploads/{handle.upload_id}", headers=headers)
            assert upload.json()["state"] == "COMMITTED"
            assert upload.json()["job_id"] == handle.job_id
            assert upload.json()["confirmed_offset"] == handle.size_bytes


@pytest.mark.asyncio
async def test_persisted_done_result_is_served_after_reopen(tmp_path):
    """真实持久 DONE 结果：换新进程/新连接仍能从同一 job_id 领到完整 RecognitionMessage。"""
    source = _source(tmp_path, size=1024)
    recovery = tmp_path / "resume.json"
    result = {
        "task_id": None,
        "type": "file",
        "socket_id": "",
        "owner_kind": "http",
        "duration": 1.5,
        "time_start": 1.0,
        "time_submit": 2.0,
        "time_complete": 3.0,
        "text": "你好世界",
        "text_accu": "你好世界",
        # 文件任务的 tokens 拼接必须与 text_accu 一致（storage 与 pipeline 同一约定）
        "tokens": ["你", "好", "世", "界"],
        "timestamps": [0.1, 0.5, 0.9, 1.3],
        "is_final": True,
    }
    async with running_server(tmp_path, inference=True) as (server, base_url):
        handle = await submit_file_http(source, base_url, resume_path=recovery, chunk_bytes=1024)
        result["task_id"] = handle.job_id
        # 真实持久提交：与 DONE 同一事务（放在 I/O 线程里，与生产路径一致）
        server._worker.run_sync(server._store.record_result, handle.job_id, result)

    # 新进程语义：全新 listener + 全新 store 连接读同一目录
    async with running_server(tmp_path) as (_server2, base_url2):
        # 同一客户端改指重启后的服务端地址（其余凭据/进度文件不变）
        stored = json.loads(recovery.read_text(encoding="utf-8"))
        stored["base_url"] = base_url2
        recovery.write_text(json.dumps(stored), encoding="utf-8")
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            token = json.loads(recovery.read_text(encoding="utf-8"))["token"]
            fetched = await client.get(
                f"{base_url2}/v1/jobs/{handle.job_id}/result",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert fetched.status_code == 200
            payload = fetched.json()
            assert payload["text"] == "你好世界"
            assert payload["tokens"] == ["你", "好", "世", "界"]
            assert payload["timestamps"] == [0.1, 0.5, 0.9, 1.3]
            assert payload["task_id"] == handle.job_id
            assert payload["is_final"] is True
        status = await sdk_http.get_file_job_http(base_url2, resume_path=recovery)
        assert status.state == "DONE"
        assert status.result_available is True
        transcript = await sdk_http.get_file_result_http(base_url2, resume_path=recovery)
        assert transcript.text == "你好世界"


@pytest.mark.asyncio
async def test_negative_matrix_keeps_old_bytes(tmp_path):
    """错误/缺令牌、未知资源、错 offset、超块、缺 Content-Length、Content-Encoding 全部显式拒绝。"""
    source = _source(tmp_path, size=3000)
    recovery = tmp_path / "resume.json"
    async with running_server(tmp_path, inference=True) as (server, base_url):
        await submit_file_http(source, base_url, resume_path=recovery, chunk_bytes=1024)
        stored = json.loads(recovery.read_text(encoding="utf-8"))
        upload_id = stored["upload_id"]
        disk_path = server.data_dir / "sources" / f"{upload_id}.bin"
        before = disk_path.read_bytes()
        auth = {"Authorization": f"Bearer {stored['token']}"}
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            missing = await client.get(f"{base_url}/v1/uploads/{upload_id}")
            assert missing.status_code == 401
            assert missing.json()["code"] == "unauthorized"
            # 未知编号与错误令牌同 404，不泄露编号本身是否赋权
            unknown = await client.get(
                f"{base_url}/v1/uploads/00000000-0000-4000-8000-000000000000", headers=auth
            )
            wrong = await client.get(
                f"{base_url}/v1/uploads/{upload_id}", headers={"Authorization": "Bearer wrong-token"}
            )
            assert unknown.status_code == wrong.status_code == 404
            stale = await client.patch(
                f"{base_url}/v1/uploads/{upload_id}",
                headers={**auth, "Content-Length": "4", "Upload-Offset": "0",
                         "Content-Type": "application/octet-stream"},
                content=b"junk",
            )
            assert stale.status_code == 409
            assert stale.json()["confirmed_offset"] == 3000
            oversize = await client.patch(
                f"{base_url}/v1/uploads/{upload_id}",
                headers={**auth, "Content-Length": "1048577", "Upload-Offset": "3000",
                         "Content-Type": "application/octet-stream"},
                content=b"x" * 1048577,
            )
            assert oversize.status_code == 413
            encoded = await client.patch(
                f"{base_url}/v1/uploads/{upload_id}",
                headers={**auth, "Content-Length": "4", "Upload-Offset": "3000",
                         "Content-Type": "application/octet-stream", "Content-Encoding": "gzip"},
                content=b"junk",
            )
            assert encoded.status_code == 415
            no_length = await client.request(
                "PATCH", f"{base_url}/v1/uploads/{upload_id}",
                headers={**auth, "Upload-Offset": "3000", "Content-Type": "application/octet-stream",
                         "Transfer-Encoding": "chunked"},
                content=_async_chunks(b"ju", b"nk"),
            )
            assert no_length.status_code == 411
            assert no_length.json()["code"] == "length_required"
            wrong_type = await client.patch(
                f"{base_url}/v1/uploads/{upload_id}",
                headers={**auth, "Content-Length": "4", "Upload-Offset": "3000",
                         "Content-Type": "application/json"},
                content=b"junk",
            )
            assert wrong_type.status_code == 415
        assert disk_path.read_bytes() == before


@pytest.mark.asyncio
async def test_body_and_handler_admission_rejects_without_waiting(tmp_path):
    """17 条真实 TCP 请求停在 body 时超限快速 429，GET 不进入 semaphore 等待队列。"""
    async with running_server(tmp_path) as (server, base_url):
        port = int(base_url.rsplit(":", 1)[1])
        pairs = []
        request = (
            f"POST /v1/uploads HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            "Authorization: Bearer admission-token\r\nIdempotency-Key: admission-key\r\n"
            "Content-Type: application/json\r\nContent-Length: 2\r\n\r\n"
        ).encode()
        try:
            for _ in range(17):
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(request)
                await writer.drain()
                pairs.append((reader, writer))
            reads = [asyncio.create_task(reader.readline()) for reader, _ in pairs]
            done, pending = await asyncio.wait(reads, timeout=1)
            status_lines = [task.result() for task in done]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            assert len(status_lines) == 15
            assert all(b" 429 " in line for line in status_lines)
            assert server._handler_slots._value == 14
            assert server._body_slots._value == 0
            assert not server._handler_slots._waiters
            assert not server._body_slots._waiters

            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write((f"GET /v1/jobs/missing HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                          "Authorization: Bearer admission-token\r\nConnection: close\r\n\r\n").encode())
            await writer.drain()
            response = await asyncio.wait_for(reader.readline(), timeout=1)
            assert b" 404 " in response
            writer.close()
            await writer.wait_closed()
        finally:
            for _, writer in pairs:
                writer.close()
            await asyncio.gather(*(writer.wait_closed() for _, writer in pairs),
                                 return_exceptions=True)
        deadline = asyncio.get_running_loop().time() + 2
        while server._body_slots._value != 2 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.005)
        assert server.fatal is None
        assert server._handler_slots._value == 16
        assert server._body_slots._value == 2


@pytest.mark.asyncio
async def test_handler_cancellation_and_io_completion_orders_preserve_confirmed_bytes(tmp_path):
    """真实 TCP route task 被取消；取消先/IO 先各五次，独立核对文件与 SQLite。"""
    token = "capability-token-value"
    async with running_server(tmp_path, inference=True) as (server, base_url):
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            for order in ("cancel-first", "io-first"):
                for trial in range(5):
                    source = bytes((65 + trial, 75 + trial, 85 + trial, 95 + trial))
                    created = await _create_upload_json(client, base_url, token, source, f"{order}-{trial}")
                    upload_id = created["upload_id"]
                    route_path = f"/v1/uploads/{upload_id}"
                    patch_headers = {"Authorization": f"Bearer {token}",
                                     "Content-Length": "2", "Upload-Offset": "0",
                                     "Content-Type": "application/octet-stream"}
                    io_started = threading.Event()
                    io_release = threading.Event()
                    io_finished = asyncio.Event()
                    route_release = asyncio.Event()
                    original_append = server._store.append_bytes
                    original_run = server._worker.run

                    def pause_before_io(upload, capability, offset, data):
                        io_started.set()
                        if not io_release.wait(5):
                            raise RuntimeError("test I/O barrier timed out")
                        return original_append(upload, capability, offset, data)

                    async def pause_after_io(func, *args, **kwargs):
                        result = await original_run(func, *args, **kwargs)
                        if func is original_append and args[0] == upload_id and args[2] == 0:
                            io_finished.set()
                            await route_release.wait()
                        return result

                    if order == "cancel-first":
                        server._store.append_bytes = pause_before_io
                    else:
                        server._worker.run = pause_after_io

                    first_request = asyncio.create_task(client.patch(
                        base_url + route_path, headers=patch_headers, content=source[:2]))
                    try:
                        if order == "cancel-first":
                            assert await asyncio.to_thread(io_started.wait, 5)
                        else:
                            # 原 worker Future 已完整执行文件写入、fsync 与 SQLite offset commit。
                            await asyncio.wait_for(io_finished.wait(), timeout=5)

                        handler = await _find_http_handler_task(route_path)
                        handler.cancel()
                        cancelled = await asyncio.gather(handler, return_exceptions=True)
                        assert isinstance(cancelled[0], asyncio.CancelledError)

                        pending_after_cancel = 1 if order == "cancel-first" else 0
                        await _wait_for_worker_pending(server._worker, pending_after_cancel)
                        expected_free = server._worker._mailbox._value
                        assert expected_free == (31 if order == "cancel-first" else 32)

                        try:
                            await asyncio.wait_for(first_request, timeout=5)
                        except httpx.HTTPError:
                            pass

                        if order == "cancel-first":
                            assert _read_db(server.data_dir,
                                            "SELECT confirmed_offset FROM uploads WHERE upload_id=?",
                                            (upload_id,))[0]["confirmed_offset"] == 0
                            assert (server.data_dir / "sources" / f"{upload_id}.bin").read_bytes() == b""
                            get_request = asyncio.create_task(client.get(
                                base_url + route_path,
                                headers={"Authorization": f"Bearer {token}"}))
                            stale_patch = asyncio.create_task(client.patch(
                                base_url + route_path,
                                headers={**patch_headers, "Upload-Offset": "0"}, content=source[:2]))
                            await _wait_for_worker_pending(server._worker, 3)
                            assert server._worker._mailbox._value == 29
                            io_release.set()
                            get_result, patch_result = await asyncio.gather(get_request, stale_patch)
                            assert get_result.status_code == 200
                            assert get_result.json()["confirmed_offset"] == 2
                            assert patch_result.status_code == 409
                            assert patch_result.json()["confirmed_offset"] == 2
                        else:
                            assert _read_db(server.data_dir,
                                            "SELECT confirmed_offset FROM uploads WHERE upload_id=?",
                                            (upload_id,))[0]["confirmed_offset"] == 2
                            assert (server.data_dir / "sources" / f"{upload_id}.bin").read_bytes() == source[:2]
                            route_release.set()
                            server._worker.run = original_run
                            get_request = asyncio.create_task(client.get(
                                base_url + route_path,
                                headers={"Authorization": f"Bearer {token}"}))
                            next_patch = asyncio.create_task(client.patch(
                                base_url + route_path,
                                headers={**patch_headers, "Upload-Offset": "2"}, content=source[2:]))
                            get_result, patch_result = await asyncio.gather(get_request, next_patch)
                            assert get_result.status_code == 200
                            assert get_result.json()["confirmed_offset"] == 2
                            assert patch_result.status_code == 204
                            assert patch_result.headers["Upload-Offset"] == "4"

                        if order == "cancel-first":
                            await _wait_for_worker_pending(server._worker, 0)
                            server._store.append_bytes = original_append
                            resumed = await client.patch(
                                base_url + route_path,
                                headers={**patch_headers, "Upload-Offset": "2"}, content=source[2:])
                            assert resumed.status_code == 204
                            assert resumed.headers["Upload-Offset"] == "4"
                        assert _read_db(server.data_dir,
                                        "SELECT confirmed_offset FROM uploads WHERE upload_id=?",
                                        (upload_id,))[0]["confirmed_offset"] == 4
                        assert (server.data_dir / "sources" / f"{upload_id}.bin").read_bytes() == source
                        await _wait_for_worker_pending(server._worker, 0)
                        assert server._worker._mailbox._value == 32
                    finally:
                        io_release.set()
                        route_release.set()
                        server._store.append_bytes = original_append
                        server._worker.run = original_run


@pytest.mark.asyncio
async def test_upload_identity_and_limit_validation(tmp_path):
    """创建参数校验：非法 sha/size/超限 JSON 一律 4xx，且不产生任何 Job 或文件。"""
    async with running_server(tmp_path) as (server, base_url):
        auth = {"Authorization": "Bearer token-value"}
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            bad_hash = await client.post(
                base_url + "/v1/uploads",
                headers={**auth, "Idempotency-Key": "k1"},
                json={"size_bytes": 10, "sha256": "ZZ", "options": {}},
            )
            assert bad_hash.status_code == 400
            too_big = await client.post(
                base_url + "/v1/uploads",
                headers={**auth, "Idempotency-Key": "k2"},
                json={"size_bytes": 2 * 1024 * 1024 * 1024, "sha256": "a" * 64, "options": {}},
            )
            assert too_big.status_code == 413
            oversized_json = await client.post(
                base_url + "/v1/uploads",
                headers={**auth, "Idempotency-Key": "k3"},
                content=b'{"pad":"' + b"x" * (17 * 1024) + b'"}',
            )
            assert oversized_json.status_code == 413
        assert _read_db(server.data_dir, "SELECT COUNT(*) AS n FROM uploads")[0]["n"] == 0
        assert _read_db(server.data_dir, "SELECT COUNT(*) AS n FROM jobs")[0]["n"] == 0
        assert list((server.data_dir / "sources").iterdir()) == []


@pytest.mark.asyncio
async def test_options_missing_and_none_default_but_falsy_wrong_types_are_rejected(tmp_path):
    source = b"keep"
    digest = sha256(source).hexdigest()
    async with running_server(tmp_path) as (server, base_url):
        auth = {"Authorization": "Bearer option-token"}
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            missing = await client.post(
                base_url + "/v1/uploads", headers={**auth, "Idempotency-Key": "missing"},
                json={"size_bytes": len(source), "sha256": digest},
            )
            explicit_none = await client.post(
                base_url + "/v1/uploads", headers={**auth, "Idempotency-Key": "none"},
                json={"size_bytes": len(source), "sha256": digest, "options": None},
            )
            assert missing.status_code == 201
            assert explicit_none.status_code == 201
            upload_id = missing.json()["upload_id"]
            written = await client.patch(
                f"{base_url}/v1/uploads/{upload_id}",
                headers={**auth, "Content-Length": str(len(source)), "Upload-Offset": "0",
                         "Content-Type": "application/octet-stream"},
                content=source,
            )
            assert written.status_code == 204
            old_bytes = (server.data_dir / "sources" / f"{upload_id}.bin").read_bytes()
            old_row = dict(_read_db(server.data_dir,
                                    "SELECT confirmed_offset, options_json, updated_at, expires_at"
                                    " FROM uploads WHERE upload_id=?", (upload_id,))[0])
            # 与 WS 共用 segmenter.validate_segment_params 的原始 R3 范围：时长下限、重叠半开、引擎/snap 预算
            range_bad = (
                {"seg_duration": 0}, {"seg_duration": 4},
                {"seg_duration": 5, "seg_overlap": 2.5},
                {"seg_duration": 100},
                {"seg_duration": 74, "seg_overlap": 1.5},
            )
            # 不可信输入的可预期表示域错误：正巨大整数转 float 抛 OverflowError，同样局部 400
            overflow_bad = ({"seg_duration": 10 ** 400}, {"seg_overlap": 10 ** 400})
            for index, wrong_type in enumerate(([], "", 0) + range_bad + overflow_bad):
                rejected_existing = await client.post(
                    base_url + "/v1/uploads", headers={**auth, "Idempotency-Key": "missing"},
                    json={"size_bytes": len(source), "sha256": digest, "options": wrong_type},
                )
                rejected_new = await client.post(
                    base_url + "/v1/uploads", headers={**auth, "Idempotency-Key": f"bad-{index}"},
                    json={"size_bytes": len(source), "sha256": digest, "options": wrong_type},
                )
                assert rejected_existing.status_code == 400, rejected_existing.text
                assert rejected_new.status_code == 400, rejected_new.text
                body = rejected_new.json()
                assert body["code"] == "invalid_options"
                assert body["request_id"]
            # JSON 转义 lone surrogate 过了 str 类型检查但不可 UTF-8 表示：受理边界必须局部 400
            for index, field in enumerate(("model", "language", "context")):
                rejected = await client.post(
                    base_url + "/v1/uploads", headers={**auth, "Idempotency-Key": f"surrogate-{index}"},
                    content=b'{"size_bytes":4,"sha256":"' + digest.encode()
                            + b'","options":{"' + field.encode() + b'":"\\ud800"}}',
                )
                assert rejected.status_code == 400, rejected.text
                body = rejected.json()
                assert body["code"] == "invalid_options"
                assert body["request_id"]
            assert (server.data_dir / "sources" / f"{upload_id}.bin").read_bytes() == old_bytes
            assert dict(_read_db(server.data_dir,
                                 "SELECT confirmed_offset, options_json, updated_at, expires_at"
                                 " FROM uploads WHERE upload_id=?", (upload_id,))[0]) == old_row
            legal = ({"seg_duration": 5, "seg_overlap": 0},
                     {"seg_duration": 7.5, "seg_overlap": 1.25},
                     {"seg_duration": 74, "seg_overlap": 1})
            for index, good in enumerate(legal):
                accepted = await client.post(
                    base_url + "/v1/uploads", headers={**auth, "Idempotency-Key": f"legal-{index}"},
                    json={"size_bytes": len(source), "sha256": digest, "options": good},
                )
                assert accepted.status_code == 201, accepted.text
            assert _read_db(server.data_dir, "SELECT COUNT(*) AS n FROM uploads")[0]["n"] == 2 + len(legal)
            assert _read_db(server.data_dir, "SELECT COUNT(*) AS n FROM jobs")[0]["n"] == 0
            assert {p.name for p in (server.data_dir / "sources").iterdir()} == {
                f"{row['upload_id']}.bin" for row in _read_db(server.data_dir, "SELECT upload_id FROM uploads")
            }
            assert old_row["options_json"] == _read_db(
                server.data_dir,
                "SELECT options_json FROM uploads WHERE create_key='none'",
            )[0]["options_json"]
            assert json.loads(old_row["options_json"]) == {
                "model": None, "language": None, "context": None,
                "seg_duration": 15.0, "seg_overlap": 2.0,
            }
        assert server.fatal is None


@pytest.mark.asyncio
async def test_non_utf8_header_bytes_are_local_4xx_not_fatal(tmp_path):
    """Authorization/Idempotency-Key 原始非法字节经 surrogateescape 直达服务端：局部 4xx，不 fatal。

    路径侧实测相反：原始非 ASCII 字节被 aiohttp 解析器 400 拒绝，百分号编码被替换为
    可编码字符后 404，均到不了 SQL，所以防御只加在两个实测可触发的 header 上。
    """
    digest = sha256(b"keep").hexdigest()
    async with running_server(tmp_path) as (server, base_url):
        host, port = "127.0.0.1", int(base_url.rsplit(":", 1)[1])
        body = b'{"size_bytes":4,"sha256":"' + digest.encode() + b'","options":{}}'

        async def post_raw(extra_headers: bytes):
            reader, writer = await asyncio.open_connection(host, port)
            writer.write(b"POST /v1/uploads HTTP/1.1\r\nHost: x\r\n"
                         b"Content-Type: application/json\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\n" + extra_headers + b"\r\n\r\n" + body)
            await writer.drain()
            status, resp_body = await _read_raw_http_response(reader, 5)
            writer.close()
            return status, json.loads(resp_body)

        status, resp = await post_raw(b"Authorization: Bearer t\xffk\r\nIdempotency-Key: h1")
        assert status.startswith("HTTP/1.1 401"), status
        assert resp["code"] == "unauthorized"
        assert resp["request_id"]
        status, resp = await post_raw(b"Authorization: Bearer ok\r\nIdempotency-Key: k\xff1")
        assert status.startswith("HTTP/1.1 400"), status
        assert resp["code"] == "invalid_request"
        assert resp["request_id"]
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            good = await client.post(
                base_url + "/v1/uploads",
                headers={"Authorization": "Bearer ok", "Idempotency-Key": "h-ok"},
                json={"size_bytes": 4, "sha256": digest},
            )
            assert good.status_code == 201, good.text
        assert server.fatal is None


@pytest.mark.asyncio
async def test_create_rejects_int_literal_over_digit_limit_as_invalid_json(tmp_path):
    """16 KiB 内有界 raw JSON：整数字面量超解释器位数上限是预期 ValueError，局部 400 不 fatal。

    与坏语法/坏 UTF-8 同属 json.loads 表达式已知输入错误（ValueError 家族）；
    不触发任何文件/SQL 写入，后续合法请求仍受理，listener 无 fatal。
    """
    async with running_server(tmp_path) as (server, base_url):
        auth = {"Authorization": "Bearer json-token"}
        oversized_int = b'{"size_bytes":' + b"1" * 4301 + b"}"
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            rejected = await client.post(
                base_url + "/v1/uploads",
                headers={**auth, "Idempotency-Key": "json-digits"},
                content=oversized_int,
            )
            assert rejected.status_code == 400, rejected.text
            body = rejected.json()
            assert body["code"] == "invalid_json"
            assert body["request_id"]
            assert _read_db(server.data_dir, "SELECT COUNT(*) AS n FROM uploads")[0]["n"] == 0
            assert _read_db(server.data_dir, "SELECT COUNT(*) AS n FROM jobs")[0]["n"] == 0
            assert list((server.data_dir / "sources").iterdir()) == []
            good = await client.post(
                base_url + "/v1/uploads",
                headers={**auth, "Idempotency-Key": "json-good"},
                json={"size_bytes": 4, "sha256": sha256(b"keep").hexdigest()},
            )
            assert good.status_code == 201, good.text
        assert server.fatal is None


def _short_idle(monkeypatch, seconds: float = 0.4) -> float:
    monkeypatch.setattr(ServerConfig, "upload_idle_seconds", seconds)
    return seconds


async def _create_upload_json(client, base_url: str, token: str, payload: bytes, key: str):
    response = await client.post(
        base_url + "/v1/uploads",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
        json={"size_bytes": len(payload), "sha256": sha256(payload).hexdigest(), "options": {}},
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize("route", ("create", "patch", "commit"))
@pytest.mark.asyncio
async def test_body_idle_timeout_is_408_on_create_patch_commit(tmp_path, monkeypatch, route):
    """三共同入口：对端不关、客户端超时足够大时，每次等下一块超过既有空闲值返回 408。"""
    idle = _short_idle(monkeypatch)
    token = "idle-timeout-token"
    async with running_server(tmp_path) as (server, base_url):
        host, port = "127.0.0.1", int(base_url.rsplit(":", 1)[1])
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=10) as client:
            for trial in range(5):
                payload = bytes((trial + 3, trial + 7, trial + 11, trial + 13))
                created = await _create_upload_json(client, base_url, token, payload, f"{route}-{trial}")
                upload_id = created["upload_id"]
                if route == "patch":
                    first = await client.patch(
                        f"{base_url}/v1/uploads/{upload_id}",
                        headers={"Authorization": f"Bearer {token}", "Content-Length": "2",
                                 "Upload-Offset": "0", "Content-Type": "application/octet-stream"},
                        content=payload[:2],
                    )
                    assert first.status_code == 204
                    before = dict(_read_db(
                        server.data_dir,
                        "SELECT confirmed_offset, source_name, updated_at, expires_at FROM uploads WHERE upload_id=?",
                        (upload_id,),
                    )[0])
                    source_bytes = (server.data_dir / "sources" / before["source_name"]).read_bytes()
                    headers = (
                        f"PATCH /v1/uploads/{upload_id} HTTP/1.1\r\nHost: {host}:{port}\r\n"
                        f"Authorization: Bearer {token}\r\nContent-Length: 2\r\n"
                        f"Upload-Offset: 2\r\nContent-Type: application/octet-stream\r\n\r\n"
                    ).encode()
                    prefix = b""
                elif route == "commit":
                    headers = (
                        f"POST /v1/uploads/{upload_id}/commit HTTP/1.1\r\nHost: {host}:{port}\r\n"
                        f"Authorization: Bearer {token}\r\nContent-Length: 8\r\n"
                        f"Content-Type: application/json\r\n\r\n"
                    ).encode()
                    prefix = b""
                    before = dict(_read_db(
                        server.data_dir,
                        "SELECT confirmed_offset, source_name, updated_at, expires_at FROM uploads WHERE upload_id=?",
                        (upload_id,),
                    )[0])
                    source_bytes = (server.data_dir / "sources" / before["source_name"]).read_bytes()
                else:
                    headers = (
                        f"POST /v1/uploads HTTP/1.1\r\nHost: {host}:{port}\r\n"
                        f"Authorization: Bearer {token}\r\nIdempotency-Key: hang-{route}-{trial}\r\n"
                        f"Content-Type: application/json\r\nContent-Length: 80\r\n\r\n"
                    ).encode()
                    prefix = b""
                    before = None
                    source_bytes = None
                reader, writer, status, body = await _hang_http_body(
                    host, port, headers, prefix, timeout=idle + 1.5,
                )
                try:
                    assert " 408 " in status, status
                    payload_json = json.loads(body.decode("utf-8"))
                    assert payload_json["code"] == "request_timeout"
                    assert payload_json["request_id"]
                    assert server.fatal is None
                    if before is not None:
                        after = dict(_read_db(
                            server.data_dir,
                            "SELECT confirmed_offset, source_name, updated_at, expires_at FROM uploads WHERE upload_id=?",
                            (upload_id,),
                        )[0])
                        assert after == before
                        assert (server.data_dir / "sources" / after["source_name"]).read_bytes() == source_bytes
                finally:
                    writer.close()
                    await writer.wait_closed()
                if route == "patch":
                    resume = await client.patch(
                        f"{base_url}/v1/uploads/{upload_id}",
                        headers={"Authorization": f"Bearer {token}", "Content-Length": "2",
                                 "Upload-Offset": "2", "Content-Type": "application/octet-stream"},
                        content=payload[2:],
                    )
                    assert resume.status_code == 204
                    assert resume.headers["Upload-Offset"] == "4"
                    assert (server.data_dir / "sources" / f"{upload_id}.bin").read_bytes() == payload
                    get = await client.get(
                        f"{base_url}/v1/uploads/{upload_id}",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    assert get.status_code == 200
                    assert get.json()["confirmed_offset"] == 4
        assert server.fatal is None
        assert server._body_slots._value == 2
        assert server._handler_slots._value == 16


@pytest.mark.asyncio
async def test_two_half_open_bodies_timeout_then_new_requests_succeed(tmp_path, monkeypatch):
    """两半开占满 body 名额：第三请求立刻 429；deadline 后不关对端也能新 POST/PATCH。"""
    idle = _short_idle(monkeypatch)
    token = "half-open-token"
    async with running_server(tmp_path) as (server, base_url):
        host, port = "127.0.0.1", int(base_url.rsplit(":", 1)[1])
        hang = (
            f"POST /v1/uploads HTTP/1.1\r\nHost: {host}:{port}\r\n"
            f"Authorization: Bearer {token}\r\nIdempotency-Key: half-{{n}}\r\n"
            f"Content-Type: application/json\r\nContent-Length: 80\r\n\r\n"
        )
        held = []
        try:
            for index in range(2):
                reader, writer = await asyncio.open_connection(host, port)
                writer.write(hang.format(n=index).encode())
                await writer.drain()
                held.append((reader, writer))
            deadline = asyncio.get_running_loop().time() + 1
            while server._body_slots._value != 0 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.005)
            assert server._body_slots._value == 0
            third_reader, third_writer = await asyncio.open_connection(host, port)
            third_writer.write(hang.format(n=2).encode())
            await third_writer.drain()
            status, body = await _read_raw_http_response(third_reader, 1)
            third_writer.close()
            await third_writer.wait_closed()
            assert " 429 " in status, status
            assert json.loads(body)["code"] == "body_overloaded"
            async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
                missing = await client.get(
                    f"{base_url}/v1/jobs/missing",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert missing.status_code == 404
            for reader, writer in held:
                status, body = await _read_raw_http_response(reader, idle + 1.5)
                assert " 408 " in status, status
                assert json.loads(body)["code"] == "request_timeout"
                assert json.loads(body)["request_id"]
            assert server.fatal is None
            assert server._body_slots._value == 2
            assert server._handler_slots._value == 16
            payload = b"slow-ok"
            async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
                created = await _create_upload_json(client, base_url, token, payload, "after-idle")
                patched = await client.patch(
                    f"{base_url}/v1/uploads/{created['upload_id']}",
                    headers={"Authorization": f"Bearer {token}",
                             "Content-Length": str(len(payload)), "Upload-Offset": "0",
                             "Content-Type": "application/octet-stream"},
                    content=payload,
                )
                assert patched.status_code == 204
        finally:
            for _, writer in held:
                writer.close()
            await asyncio.gather(*(writer.wait_closed() for _, writer in held), return_exceptions=True)
        assert server.fatal is None


@pytest.mark.asyncio
async def test_slow_chunks_succeed_when_each_gap_is_below_idle(tmp_path, monkeypatch):
    """持续慢传：每块间隔 < idle、总历时 > idle，仍按整段成功，不是全 request 截止。"""
    idle = _short_idle(monkeypatch, 0.35)
    gap = 0.12
    token = "slow-token"
    async with running_server(tmp_path) as (server, base_url):
        host, port = "127.0.0.1", int(base_url.rsplit(":", 1)[1])
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            for trial in range(5):
                payload = bytes((trial + n * 17) % 251 for n in range(8))
                created = await _create_upload_json(client, base_url, token, payload, f"slow-{trial}")
                upload_id = created["upload_id"]
                started = asyncio.get_running_loop().time()
                reader, writer = await asyncio.open_connection(host, port)
                writer.write((
                    f"PATCH /v1/uploads/{upload_id} HTTP/1.1\r\nHost: {host}:{port}\r\n"
                    f"Authorization: Bearer {token}\r\nContent-Length: {len(payload)}\r\n"
                    f"Upload-Offset: 0\r\nContent-Type: application/octet-stream\r\n\r\n"
                ).encode())
                await writer.drain()
                for byte in payload:
                    writer.write(bytes([byte]))
                    await writer.drain()
                    await asyncio.sleep(gap)
                status, _ = await _read_raw_http_response(reader, idle + 2)
                writer.close()
                await writer.wait_closed()
                elapsed = asyncio.get_running_loop().time() - started
                assert elapsed > idle
                assert " 204 " in status, status
                row = _read_db(
                    server.data_dir,
                    "SELECT confirmed_offset, sha256 FROM uploads WHERE upload_id=?",
                    (upload_id,),
                )[0]
                disk = (server.data_dir / "sources" / f"{upload_id}.bin").read_bytes()
                assert disk == payload
                assert sha256(disk).hexdigest() == row["sha256"]
                assert row["confirmed_offset"] == len(payload)
        assert server.fatal is None
