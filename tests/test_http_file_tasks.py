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
from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "sdk"))

from capswriter_asr import AsrError, resume_file_http, submit_file_http  # noqa: E402
from capswriter_asr import http_client as sdk_http  # noqa: E402

from core.server.http_server import HttpServer  # noqa: E402


class _StubApp:
    """HttpServer 只用到 app.loop（socket manager 才用），这里给出真实事件循环引用。"""

    def __init__(self, loop):
        self.loop = loop


class _RunnerServer(HttpServer):
    """测试用：可注入真实推理协调者是否可用。"""

    inference = False

    def _inference_ready(self) -> bool:
        return self.inference



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


def _confirmed_total(data_dir: Path) -> int:
    rows = _read_db(data_dir, "SELECT COALESCE(SUM(confirmed_offset),0) AS total FROM uploads")
    return rows[0]["total"]

@asynccontextmanager
async def running_server(tmp_path: Path, inference: bool = False, pause_hook=None):
    """在 port 0 上起真实 aiohttp listener，退出时收尾。"""
    loop = asyncio.get_running_loop()
    server = _RunnerServer(_StubApp(loop), "127.0.0.1", 0, tmp_path / "httpdata")
    server.inference = inference
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
        "tokens": ["你", "好"],
        "timestamps": [0.1, 0.5],
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
            assert payload["tokens"] == ["你", "好"]
            assert payload["timestamps"] == [0.1, 0.5]
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
async def test_route_cancellation_does_not_end_underlying_io(tmp_path):
    """主动暂停真实 I/O + 取消 route：写保护仍到 I/O 结束，新 PATCH 接着写而不是互相覆盖。"""
    source = _source(tmp_path, size=2048)
    digest = sha256(source.read_bytes()).hexdigest()
    paused = asyncio.Event()
    release = asyncio.Event()
    calls = {"n": 0}

    async with running_server(tmp_path, inference=True) as (server, base_url):
        original = server._store.append_bytes

        def pausing(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                paused.set()
                # 在 I/O 线程里同步等主循环放行，模拟真实慢写
                asyncio.run_coroutine_threadsafe(release.wait(), server.app.loop).result()
            return original(*args, **kwargs)

        server._store.append_bytes = pausing
        token = "capability-token-value"
        headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "create-key-1"}
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            created = await client.post(
                base_url + "/v1/uploads",
                headers=headers,
                json={"size_bytes": source.stat().st_size, "sha256": digest, "options": {}},
            )
            assert created.status_code == 201
            upload_id = created.json()["upload_id"]
            patch_headers = {
                "Authorization": f"Bearer {token}",
                "Content-Length": "1024",
                "Upload-Offset": "0",
                "Content-Type": "application/octet-stream",
            }
            first = asyncio.ensure_future(
                client.patch(f"{base_url}/v1/uploads/{upload_id}", headers=patch_headers,
                             content=source.read_bytes()[:1024])
            )
            await asyncio.wait_for(paused.wait(), timeout=10)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            # 取消只结束等待方：底层 I/O 仍在进行，数据库 offset 未前进
            assert _confirmed_total(server.data_dir) == 0
            release.set()
            await asyncio.sleep(0.3)
            # I/O 真正结束后 offset 才前进
            assert _confirmed_total(server.data_dir) == 1024
            # 随后的新 PATCH 从可信 offset 接着写，两种顺序都不丢字节
            second = await client.patch(
                f"{base_url}/v1/uploads/{upload_id}",
                headers={**patch_headers, "Upload-Offset": "1024"},
                content=source.read_bytes()[1024:],
            )
            assert second.status_code == 204
            assert second.headers["Upload-Offset"] == "2048"
        disk = server.data_dir / "sources" / f"{upload_id}.bin"
        assert disk.read_bytes() == source.read_bytes()
        assert _confirmed_total(server.data_dir) == 2048


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