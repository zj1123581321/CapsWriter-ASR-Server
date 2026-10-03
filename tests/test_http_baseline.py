"""HTTP/WS 基线工具的最小归一化与 producer 计量契约。"""
from __future__ import annotations

import sys
import asyncio
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

import httpx
import numpy as np
import pytest
import soundfile as sf
import websockets

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "sdk"))

import _baseline_http_ws as baseline  # noqa: E402
from capswriter_asr.http_client import submit_file_http  # noqa: E402
from capswriter_asr import client as ws_client  # noqa: E402

FIXTURE = json.loads((ROOT / "tests/fixtures/http_baseline_producer.json").read_text())
SERVER_SHA = "820c3a2ee4fccc1b44187bd99c40cf99c16e1ca2"


def test_srt_normalization_removes_cues_and_merges_wrapped_text():
    source = (
        "1\n00:00:00,000 --> 00:00:01,000\nHello,\nWORLD!\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\n你好，\n世界。\n"
    )
    assert baseline.normalize_reference(source) == "helloworld你好世界"


def test_http_meter_counts_emitted_patch_and_repeated_offset_bytes():
    meter = baseline.PayloadMeter()
    meter.record_http("POST", "/v1/uploads", {}, b'{"size_bytes":4}')
    meter.record_http("PATCH", "/v1/uploads/u", {"upload-offset": "0"}, b"abcd")
    meter.record_http("PATCH", "/v1/uploads/u", {"upload-offset": "0"}, b"abcd")
    summary = meter.summary()
    assert summary.get("http_patch_body_bytes") == 8
    assert summary["http_control_json_bytes"] == 16
    assert summary["http_retransmitted_bytes"] == 4


class TcpCapture:
    """本机测试服务保留 SDK 真正写出的 HTTP 请求体。"""

    def __init__(self, handler):
        self.handler = handler
        self.requests = []
        self.server = None

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *_):
        self.server.close()
        await self.server.wait_closed()

    @property
    def url(self):
        port = self.server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    async def _serve(self, reader, writer):
        try:
            line = await reader.readline()
            if not line:
                return
            method, target, _ = line.decode("ascii").rstrip("\r\n").split(" ", 2)
            headers = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                key, value = line.decode("iso-8859-1").rstrip("\r\n").split(":", 1)
                headers[key.lower()] = value.strip()
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            request = {"method": method, "target": target, "headers": headers, "body": body}
            self.requests.append(request)
            status, response_headers, response_body = await self.handler(request)
            response_headers = {**response_headers, "Content-Length": str(len(response_body)), "Connection": "close"}
            writer.write(
                f"HTTP/1.1 {status} test\r\n".encode("ascii")
                + b"".join(f"{key}: {value}\r\n".encode("ascii") for key, value in response_headers.items())
                + b"\r\n" + response_body
            )
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        finally:
            writer.close()
            await writer.wait_closed()


def _json_response(payload, status=200, headers=None):
    return status, {"Content-Type": "application/json", **(headers or {})}, json.dumps(payload).encode()


def _http_response_for_fixture(request, *, source_size=None, job_state="QUEUED"):
    method, path = request["method"], request["target"]
    if method == "POST" and path == "/v1/uploads":
        create = json.loads(request["body"])
        return _json_response({
            "upload_id": "upload-fixture", "state": "UPLOADING",
            "size_bytes": create["size_bytes"], "confirmed_offset": 0,
            "expires_at": "2026-10-05T00:00:00Z",
        }, 201)
    if method == "PATCH" and path == "/v1/uploads/upload-fixture":
        new_offset = int(request["headers"]["upload-offset"]) + len(request["body"])
        return 204, {"Upload-Offset": str(new_offset)}, b""
    if method == "POST" and path.endswith("/commit"):
        return _json_response({"job_id": "job-fixture", "state": "QUEUED"}, 202)
    if method == "GET" and path == "/v1/jobs/job-fixture":
        return _json_response({
            "job_id": "job-fixture", "state": job_state,
            "result_available": job_state == "DONE", "source_available": True,
            "error_code": None, "time_start": 1.0, "time_submit": 2.0,
            "time_complete": 3.0 if job_state == "DONE" else None,
        })
    if method == "GET" and path == "/v1/jobs/job-fixture/result":
        return _json_response({
            "task_id": "job-fixture", "is_final": True, "duration": 0.01,
            "time_start": 1.0, "time_submit": 2.0, "time_complete": 3.0,
            "text": "private-transcript", "text_accu": "Hello, WORLD!",
            "tokens": ["private-token"], "timestamps": [0.002],
        })
    raise AssertionError((method, path))


@pytest.mark.asyncio
async def test_http_sdk_producer_matches_retained_request_fixture(tmp_path):
    fixture = FIXTURE["requests"]
    source = tmp_path / "eight.bin"
    source.write_bytes(bytes.fromhex(FIXTURE["input_bytes_hex"]))

    async def handler(request):
        return await _as_async(_http_response_for_fixture(request))

    async with TcpCapture(handler) as server:
        await submit_file_http(source, server.url, resume_path=tmp_path / "resume.json", chunk_bytes=4)

    captured = []
    for request in server.requests:
        path = request["target"]
        if path.startswith("/v1/uploads/") and path.endswith("/commit"):
            path = "/v1/uploads/{upload_id}/commit"
        elif path.startswith("/v1/uploads/"):
            path = "/v1/uploads/{upload_id}"
        item = {"method": request["method"], "path": path}
        if item["method"] == "POST" and path == "/v1/uploads":
            item["body_utf8"] = request["body"].decode()
        elif item["method"] == "PATCH":
            item["upload_offset"] = request["headers"]["upload-offset"]
            item["body_hex"] = request["body"].hex()
        else:
            item["body_hex"] = request["body"].hex()
        captured.append(item)
    assert captured == fixture


async def _as_async(value):
    return value


@pytest.mark.asyncio
async def test_ws_meter_observes_actual_sdk_v2_send_payload(monkeypatch, tmp_path):
    sent = []

    async def handler(websocket):
        message = await websocket.recv()
        sent.append(message)
        frame = json.loads(message)
        await websocket.send(json.dumps({
            "type": "result", "is_final": True, "task_id": frame["task_id"],
            "text": "synthetic", "text_accu": "synthetic", "tokens": ["x"],
            "timestamps": [0.0], "duration": 0.01,
            "time_start": frame["time_start"], "time_submit": frame["time_start"],
            "time_complete": frame["time_start"] + 0.01,
        }))

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        source = tmp_path / "synthetic.wav"
        sf.write(source, np.zeros(160, dtype=np.float32), 16000, subtype="PCM_16")
        monkeypatch.setattr(ws_client, "_transcode", lambda *_: _return(b"\x00\xff"))
        monkeypatch.setattr(ws_client, "_count_decoded_samples", lambda *_: _return(160))
        monkeypatch.setattr(ws_client, "_get_health", lambda *_: (200, b'{"protocol_version":2,"encodings":["flac"],"model":"paraformer"}'))
        monkeypatch.setattr(ws_client.uuid, "uuid4", lambda: "task-fixture")
        monkeypatch.setattr(ws_client.time, "time", lambda: 1700000000.25)
        args = type("Args", (), {
            "input": str(source),
            "server": f"ws://127.0.0.1:{port}",
            "expected_model": "paraformer", "seg_duration": 15.0,
            "seg_overlap": 2.0, "timeout": 5.0,
        })()
        meter = baseline.PayloadMeter()
        transcript, _ = await baseline.run_ws_v2(args, meter)

    assert sent == [FIXTURE["ws_v2"]["frame_utf8"]]
    assert transcript.text == "synthetic"
    assert meter.summary()["ws_json_utf8_bytes"] == len(sent[0].encode("utf-8"))
    assert meter.summary()["ws_audio_base64_bytes"] == 4
    assert meter.summary()["ws_audio_decoded_bytes"] == 2


async def _return(value):
    return value


@pytest.mark.asyncio
async def test_cli_http_producer_payload_and_private_json_bytes(tmp_path):
    source = tmp_path / "sample.wav"
    sf.write(source, np.linspace(-0.5, 0.5, 160, dtype=np.float32), 16000, subtype="PCM_16")
    reference = tmp_path / "reference.srt"
    reference.write_text("1\n00:00:00,000 --> 00:00:00,010\nHello,\nWORLD!\n", encoding="utf-8")
    private_dir = tmp_path / "private"
    polls = 0

    async def handler(request):
        nonlocal polls
        method, path = request["method"], request["target"]
        if method == "GET" and path == "/health":
            return _json_response({
                "status": "ok", "protocol_version": 2, "model": "paraformer",
                "git_sha": SERVER_SHA[:7], "worker_alive": True,
            })
        if method == "GET" and path == "/v1/jobs/job-fixture":
            polls += 1
            return await _as_async(_http_response_for_fixture(request, job_state="RUNNING" if polls == 1 else "DONE"))
        return await _as_async(_http_response_for_fixture(request))

    async with TcpCapture(handler) as server:
        env = os.environ.copy()
        env["CW_MODEL_TYPE"] = "must-not-be-read-or-printed"
        env["M7_BASELINE_TEST_SENTINEL"] = "private-env-sentinel"
        command = [
            sys.executable, str(ROOT / "scripts/_baseline_http_ws.py"),
            "--protocol", "http", "--server", server.url,
            "--health-url", server.url + "/health", "--input", str(source),
            "--fixture-id", "anonymous-http-01", "--expected-server-sha", SERVER_SHA,
            "--expected-model", "paraformer", "--private-dir", str(private_dir),
            "--reference", str(reference), "--reference-status", "unverified",
            "--timeout", "5",
        ]
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)

    assert process.returncode == 0, stderr.decode()
    assert stderr == b""
    summary = json.loads(stdout)
    assert summary["source_file_bytes"] == source.stat().st_size
    assert summary["decoded_pcm_bytes"] == 160 * 4
    assert summary["reference_status"] == "unverified"
    assert summary["cer_relative_to_reference"] == 0
    assert summary["producer_payloads"]["http_patch_request_count"] == 1
    assert summary["producer_payloads"]["http_retransmitted_bytes"] == 0
    assert b"private-transcript" not in stdout
    assert b"private-token" not in stdout
    assert b"sample.wav" not in stdout
    assert b"Hello" not in stdout
    assert b"private-env-sentinel" not in stdout

    create = next(item for item in server.requests if item["target"] == "/v1/uploads")
    patch_request = next(item for item in server.requests if item["method"] == "PATCH")
    assert json.loads(create["body"])["options"] == {
        "language": None, "context": None, "model": "paraformer",
        "seg_duration": 15.0, "seg_overlap": 2.0,
    }
    assert patch_request["body"] == source.read_bytes()
    private_file = private_dir / "anonymous-http-01-http.json"
    output_bytes = private_file.read_bytes()
    assert output_bytes == (json.dumps(json.loads(output_bytes), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    assert stat_mode(private_file) == 0o600
    private_document = json.loads(output_bytes)
    assert private_document["result"]["text"] == "private-transcript"
    assert private_document["result"]["tokens"] == ["private-token"]
    assert private_document["reference"]["text"].startswith("1\n")
    assert len([item for item in server.requests if item["method"] == "PATCH"]) == 1


def stat_mode(path):
    return path.stat().st_mode & 0o777


@pytest.mark.asyncio
async def test_empty_reference_fails_loudly_before_health_or_upload(tmp_path):
    source = tmp_path / "sample.wav"
    sf.write(source, np.zeros(160, dtype=np.float32), 16000, subtype="PCM_16")
    empty_reference = tmp_path / "empty.srt"
    empty_reference.write_text("  \n", encoding="utf-8")
    private_dir = tmp_path / "private"
    command = [
        sys.executable, str(ROOT / "scripts/_baseline_http_ws.py"),
        "--protocol", "http", "--server", "http://127.0.0.1:1",
        "--health-url", "http://127.0.0.1:1/health", "--input", str(source),
        "--fixture-id", "empty-reference", "--expected-server-sha", SERVER_SHA,
        "--expected-model", "paraformer", "--private-dir", str(private_dir),
        "--reference", str(empty_reference),
    ]
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    assert process.returncode != 0
    assert b"BASELINE_FAILED" in stderr
    assert b"ValueError" in stderr
    assert b"empty.srt" not in stderr
    assert stdout == b""
    assert list(private_dir.iterdir()) == []
