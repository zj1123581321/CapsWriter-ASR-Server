"""显式 HTTP 文件 SDK 的真实 TCP 请求契约测试。"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "sdk"))

from capswriter_asr import (  # noqa: E402
    AsrError,
    FileTaskStatus,
    Transcript,
    get_file_job_http,
    get_file_result_http,
    resume_file_http,
    submit_file_http,
)
from capswriter_asr.http_client import _write_recovery  # noqa: E402


class TcpRequest:
    def __init__(self, method: str, target: str, headers: dict[str, str], body: bytes):
        self.method = method
        self.target = target
        self.headers = headers
        self.body = body


class TcpCapture:
    """只实现测试所需的 HTTP/1.1 子集，保留真实网络字节。"""

    def __init__(self, handler):
        self.handler = handler
        self.requests: list[TcpRequest] = []
        self.server: asyncio.AbstractServer | None = None

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *_):
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()

    @property
    def url(self) -> str:
        assert self.server is not None
        port = self.server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                method, target, _version = line.decode("ascii").rstrip("\r\n").split(" ", 2)
                headers: dict[str, str] = {}
                while True:
                    line = await reader.readline()
                    if line in (b"\r\n", b"\n", b""):
                        break
                    key, value = line.decode("iso-8859-1").rstrip("\r\n").split(":", 1)
                    headers[key.lower()] = value.strip()
                body = await reader.readexactly(int(headers.get("content-length", "0")))
                request = TcpRequest(method, target, headers, body)
                self.requests.append(request)
                response = await self.handler(request)
                if response is None:
                    return
                status, response_headers, response_body = response
                writer.write(
                    f"HTTP/1.1 {status} test\r\n".encode("ascii")
                    + b"".join(
                        f"{key}: {value}\r\n".encode("ascii")
                        for key, value in response_headers.items()
                    )
                    + b"\r\n"
                    + response_body
                )
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        finally:
            writer.close()
            await writer.wait_closed()


def response(status: int, payload: object | bytes, **headers: str):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return (
        status,
        {"Content-Length": str(len(body)), "Content-Type": "application/json", **headers},
        body,
    )


def identity(path: Path) -> tuple[int, str]:
    digest = sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(17):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def recovery_payload(path: Path, base_url: str, *, offset: int = 0) -> dict:
    size, digest = identity(path)
    return {
        "version": 1,
        "base_url": base_url,
        "create_key": "create-key",
        "token": "token-for-test-only",
        "size_bytes": size,
        "sha256": digest,
        "options": {
            "language": None,
            "context": None,
            "model": None,
            "seg_duration": 15.0,
            "seg_overlap": 2.0,
        },
        "source_name": path.name,
        "upload_id": "upload-1",
        "job_id": None,
        "confirmed_offset": offset,
    }


@pytest.mark.asyncio
async def test_submit_saves_recovery_before_real_binary_upload(tmp_path):
    source = tmp_path / "sample.mp3"
    source.write_bytes(b"0123456789")
    resume_path = tmp_path / "resume.json"
    seen: list[tuple[str, bytes]] = []

    async def handler(request: TcpRequest):
        seen.append((request.method + " " + request.target, request.body))
        if request.target == "/v1/uploads":
            assert request.method == "POST"
            assert request.headers["authorization"].startswith("Bearer ")
            assert request.headers["idempotency-key"]
            assert json.loads(request.body) == {
                "size_bytes": 10,
                "sha256": sha256(source.read_bytes()).hexdigest(),
                "options": {
                    "language": "zh",
                    "context": "ctx",
                    "model": "m1",
                    "seg_duration": 15.0,
                    "seg_overlap": 2.0,
                },
            }
            assert resume_path.exists()
            saved = json.loads(resume_path.read_text())
            assert saved["upload_id"] is None
            assert saved["job_id"] is None
            return response(
                201,
                {
                    "upload_id": "upload-1",
                    "state": "UPLOADING",
                    "size_bytes": 10,
                    "confirmed_offset": 0,
                    "expires_at": "2026-10-08T00:00:00Z",
                },
            )
        if request.target == "/v1/uploads/upload-1":
            assert request.method == "PATCH"
            assert request.headers["content-type"] == "application/octet-stream"
            previous = sum(
                len(body)
                for path, body in seen[:-1]
                if path == "PATCH /v1/uploads/upload-1"
            )
            assert request.headers["upload-offset"] == str(previous)
            assert int(request.headers["content-length"]) == len(request.body)
            new_offset = previous + len(request.body)
            return 204, {"Upload-Offset": str(new_offset), "Content-Length": "0"}, b""
        if request.target == "/v1/uploads/upload-1/commit":
            assert request.method == "POST"
            assert request.body == b""
            return response(202, {"job_id": "job-1", "state": "QUEUED"})
        raise AssertionError((request.method, request.target))

    async with TcpCapture(handler) as server:
        handle = await submit_file_http(
            source,
            server.url,
            resume_path=resume_path,
            language="zh",
            context="ctx",
            model="m1",
            chunk_bytes=4,
        )

    assert handle.upload_id == "upload-1"
    assert handle.job_id == "job-1"
    assert handle.state == "QUEUED"
    assert [path for path, _ in seen] == [
        "POST /v1/uploads",
        "PATCH /v1/uploads/upload-1",
        "PATCH /v1/uploads/upload-1",
        "PATCH /v1/uploads/upload-1",
        "POST /v1/uploads/upload-1/commit",
    ]
    assert [body for path, body in seen if path.startswith("PATCH")] == [
        b"0123",
        b"4567",
        b"89",
    ]
    saved = json.loads(resume_path.read_text())
    assert saved["confirmed_offset"] == 10
    assert saved["job_id"] == "job-1"
    assert stat.S_IMODE(resume_path.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_resume_queries_offset_and_only_sends_remaining_source_bytes(tmp_path):
    source = tmp_path / "source.m4a"
    source.write_bytes(b"abcdefghij")
    resume_path = tmp_path / "resume.json"
    async with TcpCapture(lambda _request: response(500, {"code": "unexpected"})) as unused:
        _write_recovery(resume_path, recovery_payload(source, unused.url, offset=4))

        async def handler(request: TcpRequest):
            if request.target == "/v1/uploads/upload-1":
                assert request.method == "GET"
                return response(
                    200,
                    {
                        "upload_id": "upload-1",
                        "state": "UPLOADING",
                        "size_bytes": 10,
                        "sha256": sha256(source.read_bytes()).hexdigest(),
                        "confirmed_offset": 4,
                        "expires_at": "2026-10-08T00:00:00Z",
                    },
                )
            if request.target == "/v1/uploads/upload-1" and request.method == "PATCH":
                raise AssertionError("unreachable")
            if request.target == "/v1/uploads/upload-1/commit":
                assert request.method == "POST"
                return response(202, {"job_id": "job-1", "state": "QUEUED"})
            raise AssertionError((request.method, request.target))

        # The handler above needs to distinguish the same path by method.
        async def actual_handler(request: TcpRequest):
            if request.method == "GET":
                return response(
                    200,
                    {
                        "upload_id": "upload-1",
                        "state": "UPLOADING",
                        "size_bytes": 10,
                        "sha256": sha256(source.read_bytes()).hexdigest(),
                        "confirmed_offset": 4,
                        "expires_at": "2026-10-08T00:00:00Z",
                    },
                )
            if request.method == "PATCH":
                assert request.body == b"efghij"
                assert request.headers["upload-offset"] == "4"
                return 204, {"Upload-Offset": "10", "Content-Length": "0"}, b""
            if request.target.endswith("/commit"):
                return response(202, {"job_id": "job-1", "state": "QUEUED"})
            raise AssertionError((request.method, request.target))

        unused.handler = actual_handler
        handle = await resume_file_http(source, unused.url, resume_path=resume_path)
        assert handle.job_id == "job-1"
        assert [request.method for request in unused.requests] == ["GET", "PATCH", "POST"]


@pytest.mark.asyncio
async def test_status_and_result_require_exact_fields_and_preserve_transcript(tmp_path):
    source = tmp_path / "source.opus"
    source.write_bytes(b"audio")
    resume_path = tmp_path / "resume.json"
    async with TcpCapture(lambda _request: response(500, {"code": "unexpected"})) as server:
        payload = recovery_payload(source, server.url)
        payload["job_id"] = "job-1"
        _write_recovery(resume_path, payload)

        async def handler(request: TcpRequest):
            if request.target == "/v1/jobs/job-1":
                return response(
                    200,
                    {
                        "job_id": "job-1",
                        "state": "DONE",
                        "result_available": True,
                        "source_available": True,
                        "error_code": None,
                        "time_start": 1.0,
                        "time_submit": 2.0,
                        "time_complete": 3.0,
                    },
                )
            assert request.target == "/v1/jobs/job-1/result"
            return response(
                200,
                {
                    "task_id": "job-1",
                    "is_final": True,
                    "duration": 2.5,
                    "time_start": 1.0,
                    "time_submit": 2.0,
                    "time_complete": 3.0,
                    "text": "你好",
                    "text_accu": "你好",
                    "tokens": ["你", "好"],
                    "timestamps": [0.1, 0.4],
                },
            )

        server.handler = handler
        status = await get_file_job_http(server.url, resume_path=resume_path)
        result = await get_file_result_http(server.url, resume_path=resume_path)

    assert isinstance(status, FileTaskStatus)
    assert status.state == "DONE"
    assert status.result_available is True
    assert isinstance(result, Transcript)
    assert result.text_accu == "你好"
    assert result.time_complete == 3.0
    assert result.raw["timestamps"] == [0.1, 0.4]


@pytest.mark.asyncio
async def test_source_mismatch_and_existing_recovery_make_zero_requests(tmp_path):
    source = tmp_path / "source.mp3"
    source.write_bytes(b"old")
    resume_path = tmp_path / "resume.json"
    async with TcpCapture(lambda _request: response(500, {"code": "must-not-request"})) as server:
        _write_recovery(resume_path, recovery_payload(source, server.url))
        source.write_bytes(b"new")
        with pytest.raises(AsrError) as caught:
            await resume_file_http(source, server.url, resume_path=resume_path)
        assert caught.value.code == "source_changed"
        assert server.requests == []
        with pytest.raises(AsrError) as caught:
            await submit_file_http(source, server.url, resume_path=resume_path)
        assert caught.value.code == "recovery_exists"
        assert server.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body,expected",
    [
        (302, b"", "http_redirect"),
        (200, b"not-json", "invalid_response"),
        (200, {"state": "NOT_A_STATE"}, "invalid_response"),
    ],
)
async def test_invalid_http_responses_are_visible(tmp_path, status, body, expected):
    source = tmp_path / "source.mp3"
    source.write_bytes(b"audio")
    resume_path = tmp_path / "resume.json"
    async def handler(_request: TcpRequest):
        return response(status, body, Location="http://elsewhere.invalid")

    async with TcpCapture(handler) as server:
        _write_recovery(resume_path, recovery_payload(source, server.url))
        payload = json.loads(resume_path.read_text())
        payload["job_id"] = "job-1"
        _write_recovery(resume_path, payload)
        with pytest.raises(AsrError) as caught:
            await get_file_job_http(server.url, resume_path=resume_path)
    assert caught.value.code == expected


def test_cli_http_result_has_nonzero_failure_without_token_in_stderr(tmp_path):
    resume_path = tmp_path / "broken.json"
    resume_path.write_text("{not json", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "capswriter_asr",
            "http",
            "status",
            "--url",
            "http://127.0.0.1:1",
            "--resume-file",
            str(resume_path),
        ],
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "sdk")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "not json" not in result.stderr
    assert "token-for-test-only" not in result.stderr


@pytest.mark.asyncio
async def test_cli_submit_subprocess_emits_real_http_requests_and_no_token(tmp_path):
    source = tmp_path / "clip.aac"
    source.write_bytes(b"012345")
    resume_path = tmp_path / "resume.json"

    async def handler(request: TcpRequest):
        if request.target == "/v1/uploads":
            payload = json.loads(request.body)
            assert payload["size_bytes"] == 6
            assert request.headers["authorization"].startswith("Bearer ")
            return response(
                201,
                {
                    "upload_id": "upload-cli",
                    "state": "UPLOADING",
                    "size_bytes": 6,
                    "confirmed_offset": 0,
                    "expires_at": "2026-10-08T00:00:00Z",
                },
            )
        if request.method == "PATCH":
            offset = int(request.headers["upload-offset"])
            assert request.body == source.read_bytes()[offset : offset + len(request.body)]
            return 204, {
                "Upload-Offset": str(offset + len(request.body)),
                "Content-Length": "0",
            }, b""
        assert request.target == "/v1/uploads/upload-cli/commit"
        return response(202, {"job_id": "job-cli", "state": "QUEUED"})

    async with TcpCapture(handler) as server:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "capswriter_asr",
            "http",
            "submit",
            str(source),
            "--url",
            server.url,
            "--resume-file",
            str(resume_path),
            "--chunk-bytes",
            "3",
            env={
                **os.environ,
                "PYTHONPATH": str(REPO_ROOT / "sdk"),
                "HTTP_PROXY": "http://127.0.0.1:1",
                "HTTPS_PROXY": "http://127.0.0.1:1",
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15)

    assert process.returncode == 0, stderr
    assert str(resume_path) in stdout.decode()
    assert b"Bearer" not in stdout + stderr
    assert [request.method for request in server.requests] == ["POST", "PATCH", "PATCH", "POST"]
