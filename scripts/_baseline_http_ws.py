# coding: utf-8
"""真实 SDK HTTP 与协议 v2 WebSocket 基线采集器。"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import tomllib
import unicodedata
import urllib.request
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "sdk"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import httpx
import soundfile as sf
import websockets
from capswriter_asr import http_client, client as ws_client
from _baseline_asr import _cer

SCHEMA_VERSION = 1
BASELINE_FAILED = "BASELINE_FAILED"
SAMPLE_RATE = 16000
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
FIXTURE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SRT_TIME_RE = re.compile(
    r"^\s*\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*"
    r"\d{2}:\d{2}:\d{2}[,.]\d{3}(?:\s+.*)?\s*$"
)


def normalize_reference(text: str) -> str:
    """去 SRT 序号/时间轴，拼接断行，去空白和标点并折叠大小写。"""
    lines = text.lstrip("\ufeff").splitlines()
    kept: list[str] = []
    for index, line in enumerate(lines):
        if SRT_TIME_RE.fullmatch(line):
            continue
        if line.strip().isdigit() and index + 1 < len(lines) and SRT_TIME_RE.fullmatch(lines[index + 1]):
            continue
        kept.append(line)
    return "".join(
        char.casefold()
        for char in "".join(kept)
        if not char.isspace() and not unicodedata.category(char).startswith("P")
    )


class PayloadMeter:
    """只计 SDK 实际交给 HTTP/WebSocket transport 的应用层 payload。"""

    def __init__(self) -> None:
        self.http_requests = 0
        self.http_patch_requests = 0
        self.http_patch_body_bytes = 0
        self.http_control_json_bytes = 0
        self.http_retransmitted_bytes = 0
        self.ws_text_frames = 0
        self.ws_json_utf8_bytes = 0
        self.ws_binary_frames = 0
        self.ws_binary_bytes = 0
        self.ws_audio_base64_bytes = 0
        self.ws_audio_decoded_bytes = 0
        self.events: list[dict] = []
        self._patches: dict[int, tuple[int, str]] = {}

    @staticmethod
    def _route(path: str) -> str:
        if path == "/v1/uploads":
            return path
        if path.startswith("/v1/uploads/") and path.endswith("/commit"):
            return "/v1/uploads/{upload_id}/commit"
        if path.startswith("/v1/uploads/"):
            return "/v1/uploads/{upload_id}"
        if path.startswith("/v1/jobs/") and path.endswith("/result"):
            return "/v1/jobs/{job_id}/result"
        if path.startswith("/v1/jobs/"):
            return "/v1/jobs/{job_id}"
        return path

    def record_http(
        self, method: str, path: str, headers: dict[str, str], body: bytes
    ) -> None:
        body = bytes(body)
        method = method.upper()
        route = self._route(path)
        offset = headers.get("upload-offset")
        digest = hashlib.sha256(body).hexdigest()
        self.http_requests += 1
        if method == "POST" and route == "/v1/uploads":
            self.http_control_json_bytes += len(body)
        if method == "PATCH" and route == "/v1/uploads/{upload_id}":
            if offset is None or not offset.isdecimal():
                raise ValueError("HTTP PATCH 缺少有效 Upload-Offset")
            numeric_offset = int(offset)
            self.http_patch_requests += 1
            self.http_patch_body_bytes += len(body)
            previous = self._patches.get(numeric_offset)
            current = (len(body), digest)
            if previous is None:
                self._patches[numeric_offset] = current
            elif previous != current:
                raise ValueError("同一 HTTP 上传位置重复发送了不同字节")
            else:
                self.http_retransmitted_bytes += len(body)
        self.events.append({
            "protocol": "http",
            "method": method,
            "route": route,
            "body_bytes": len(body),
            "body_sha256": digest,
            "upload_offset": offset if method == "PATCH" else None,
        })

    def record_ws(self, message: str | bytes) -> None:
        if isinstance(message, str):
            payload = message.encode("utf-8")
            frame = json.loads(message)
            if not isinstance(frame, dict):
                raise ValueError("SDK WebSocket 文本帧不是 JSON 对象")
            encoded_audio = frame.get("data", "")
            audio_bytes = base64.b64decode(encoded_audio, validate=True) if encoded_audio else b""
            self.ws_text_frames += 1
            self.ws_json_utf8_bytes += len(payload)
            self.ws_audio_base64_bytes += len(encoded_audio.encode("ascii"))
            self.ws_audio_decoded_bytes += len(audio_bytes)
            kind = "json_utf8"
        elif isinstance(message, bytes):
            payload = message
            self.ws_binary_frames += 1
            self.ws_binary_bytes += len(payload)
            kind = "binary"
        else:
            raise TypeError("SDK WebSocket 发送了非文本/二进制 payload")
        self.events.append({
            "protocol": "ws-v2",
            "payload_kind": kind,
            "payload_bytes": len(payload),
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
        })

    def summary(self) -> dict:
        return {
            "http_request_count": self.http_requests,
            "http_patch_request_count": self.http_patch_requests,
            "http_patch_body_bytes": self.http_patch_body_bytes,
            "http_control_json_bytes": self.http_control_json_bytes,
            "http_retransmitted_bytes": self.http_retransmitted_bytes,
            "ws_text_frame_count": self.ws_text_frames,
            "ws_json_utf8_bytes": self.ws_json_utf8_bytes,
            "ws_binary_frame_count": self.ws_binary_frames,
            "ws_binary_bytes": self.ws_binary_bytes,
            "ws_audio_base64_bytes": self.ws_audio_base64_bytes,
            "ws_audio_decoded_bytes": self.ws_audio_decoded_bytes,
        }


class _MeteredWebSocket:
    def __init__(self, websocket, meter: PayloadMeter) -> None:
        self._websocket = websocket
        self._meter = meter

    def __getattr__(self, name):
        return getattr(self._websocket, name)

    async def send(self, message, *args, **kwargs):
        self._meter.record_ws(message)
        return await self._websocket.send(message, *args, **kwargs)


class _MeteredConnect:
    def __init__(self, connect, args, kwargs, meter: PayloadMeter) -> None:
        self._connect = connect(*args, **kwargs)
        self._meter = meter

    async def __aenter__(self):
        self._websocket = await self._connect.__aenter__()
        return _MeteredWebSocket(self._websocket, self._meter)

    async def __aexit__(self, *args):
        return await self._connect.__aexit__(*args)


def _meter_ws(meter: PayloadMeter):
    """暂时包住 SDK 使用的 connect，只记录其实际 send 参数。"""
    original = ws_client.websockets.connect

    @wraps(original)
    def connect(*args, **kwargs):
        return _MeteredConnect(original, args, kwargs, meter)

    ws_client.websockets.connect = connect
    return original


def _meter_http(meter: PayloadMeter):
    """在 HTTPX 完成实际 Request 序列化后、发送前读取 producer payload。"""
    original = httpx.AsyncClient.send

    async def send(client, request, *args, **kwargs):
        body = await request.aread()
        meter.record_http(request.method, request.url.path, dict(request.headers), body)
        return await original(client, request, *args, **kwargs)

    httpx.AsyncClient.send = send
    return original


async def run_ws_v2(args, meter: PayloadMeter):
    original = _meter_ws(meter)
    started = time.monotonic()
    try:
        transcript = await ws_client.transcribe_file(
            args.input,
            args.server,
            encoding="flac",
            model=args.expected_model,
            seg_duration=args.seg_duration,
            seg_overlap=args.seg_overlap,
            deadline_total=args.timeout,
            idle_timeout=args.timeout,
        )
    finally:
        ws_client.websockets.connect = original
    return transcript, time.monotonic() - started


async def run_http(args, meter: PayloadMeter, recovery_path: Path):
    original = _meter_http(meter)
    started = time.monotonic()
    deadline = started + args.timeout
    try:
        async with asyncio.timeout(args.timeout):
            handle = await http_client.submit_file_http(
                args.input,
                args.server,
                resume_path=recovery_path,
                model=args.expected_model,
                seg_duration=args.seg_duration,
                seg_overlap=args.seg_overlap,
            )
            while True:
                status = await http_client.get_file_job_http(
                    args.server, resume_path=recovery_path
                )
                if status.state == "FAILED":
                    raise RuntimeError("HTTP 服务端任务失败")
                if status.state == "DONE":
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("HTTP Job 等待超过基线时限")
                await asyncio.sleep(min(0.25, remaining))
            transcript = await http_client.get_file_result_http(
                args.server, resume_path=recovery_path
            )
    finally:
        httpx.AsyncClient.send = original
    recovery_path.unlink(missing_ok=True)
    return transcript, time.monotonic() - started, handle, status


def _loopback_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https", "ws", "wss"} or not parts.hostname:
        raise ValueError("服务地址必须是完整 HTTP 或 WebSocket URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("服务地址不能包含凭据、查询参数或片段")
    host = parts.hostname.lower()
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise ValueError("基线工具只连接本机 loopback 服务")
    return value


def _health_url(args) -> str:
    if args.health_url:
        value = args.health_url
    elif args.protocol == "ws-v2":
        parts = urlsplit(args.server)
        scheme = {"ws": "http", "wss": "https"}.get(parts.scheme)
        if scheme is None:
            raise ValueError("WS v2 服务地址必须使用 ws 或 wss")
        value = urlunsplit((scheme, parts.netloc, "/health", "", ""))
    else:
        raise ValueError("HTTP 模式必须用 --health-url 指向同实例的 WebSocket health 端口")
    _loopback_url(value)
    parts = urlsplit(value)
    if parts.scheme in {"ws", "wss"}:
        value = urlunsplit(({"ws": "http", "wss": "https"}[parts.scheme], parts.netloc, "/health", "", ""))
    elif parts.path != "/health":
        raise ValueError("--health-url 必须以 /health 结尾")
    return value


def validate_health(args) -> dict:
    request = urllib.request.Request(_health_url(args), method="GET")
    with urllib.request.urlopen(request, timeout=10) as response:
        if response.status != 200:
            raise ValueError("服务 health 状态不是 HTTP 200")
        payload = json.loads(response.read())
    if (
        not isinstance(payload, dict)
        or payload.get("status") != "ok"
        or payload.get("worker_alive") is not True
        or payload.get("model") != args.expected_model
        or payload.get("protocol_version", 0) < 2
    ):
        raise ValueError("服务 health 未满足基线前置条件")
    server_sha = payload.get("git_sha")
    if not isinstance(server_sha, str) or len(server_sha) < 7 or not args.expected_server_sha.startswith(server_sha):
        raise ValueError("运行服务 SHA 与要求版本不匹配")
    return {
        "status": payload["status"],
        "protocol_version": payload["protocol_version"],
        "model": payload["model"],
        "git_sha": server_sha,
        "worker_alive": payload["worker_alive"],
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _decode_pcm_bytes(path: Path) -> tuple[int, float]:
    """计本机 ffmpeg/soundfile 解码出的 16 kHz mono float32 PCM 字节。"""
    if path.suffix.lower() == ".wav":
        audio, rate = sf.read(path, dtype="float32", always_2d=True)
        if rate == SAMPLE_RATE:
            count = len(audio)
            return count * 4, count / SAMPLE_RATE
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path),
        "-map", "0:a:0", "-ar", str(SAMPLE_RATE), "-ac", "1", "-f", "f32le", "pipe:1",
    ]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=True)
    if len(result.stdout) % 4:
        raise ValueError("解码 PCM 未按 float32 样本对齐")
    return len(result.stdout), len(result.stdout) / (SAMPLE_RATE * 4)


def _private_dir(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("--private-dir 必须是绝对路径")
    path = path.resolve()
    if path == REPO_ROOT or REPO_ROOT in path.parents:
        raise ValueError("私有结果目录不得位于仓库内")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise ValueError("私有结果目录必须禁止 group/other 访问")
    return path


def _write_private_json(path: Path, payload: dict) -> bytes:
    raw = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(raw)
    return raw


def _result_metrics(transcript, source_duration_s: float) -> dict:
    tokens = list(transcript.tokens)
    timestamps = [float(value) for value in transcript.timestamps]
    monotonic = all(left <= right for left, right in zip(timestamps, timestamps[1:]))
    covered = bool(timestamps) and len(tokens) == len(timestamps)
    in_server_duration = bool(timestamps) and all(0 <= value <= float(transcript.duration) for value in timestamps)
    in_source_duration = bool(timestamps) and all(0 <= value <= source_duration_s for value in timestamps)
    text = transcript.text_accu or transcript.text
    return {
        "task_id": transcript.task_id,
        "is_final": transcript.is_final,
        "text": transcript.text,
        "text_accu": transcript.text_accu,
        "tokens": tokens,
        "timestamps_s": timestamps,
        "duration_s": float(transcript.duration),
        "time_start": transcript.time_start,
        "time_submit": transcript.time_submit,
        "time_complete": transcript.time_complete,
        "token_count": len(tokens),
        "timestamp_count": len(timestamps),
        "timestamps_monotonic": monotonic,
        "timestamps_cover_tokens": covered,
        "timestamps_within_duration": in_server_duration,
        "timestamps_within_source_duration": in_source_duration,
        "normalized_hypothesis_chars": len(normalize_reference(text)),
        "_hypothesis_normalized": normalize_reference(text),
    }


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="以真实 SDK producer 测量 loopback HTTP 与 WS v2 应用层基线")
    parser.add_argument("--protocol", choices=("ws-v2", "http"), required=True)
    parser.add_argument("--server", required=True, help="本机 ws:// 或 http:// 服务地址")
    parser.add_argument("--health-url", help="HTTP 模式下同一实例的 WS health 地址")
    parser.add_argument("--input", required=True, help="本机输入媒体文件；私有路径不写入 stdout")
    parser.add_argument("--fixture-id", required=True, help="匿名素材 ID，不得使用文件名或正文")
    parser.add_argument("--expected-server-sha", required=True, help="运行服务完整 40 位 Git SHA")
    parser.add_argument("--expected-model", required=True)
    parser.add_argument("--private-dir", required=True, help="仓库外且权限为 0700 的私有结果目录")
    parser.add_argument("--reference", help="可选参考稿路径；正文只存私有结果")
    parser.add_argument("--reference-status", choices=("unverified", "verified"), default="unverified")
    parser.add_argument("--seg-duration", type=float, default=15.0)
    parser.add_argument("--seg-overlap", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()
    if not FIXTURE_ID_RE.fullmatch(args.fixture_id):
        raise ValueError("匿名素材 ID 格式无效")
    if not SHA_RE.fullmatch(args.expected_server_sha):
        raise ValueError("--expected-server-sha 必须是 40 位小写 SHA")
    if args.reference_status == "verified" and not args.reference:
        raise ValueError("verified 状态必须同时提供参考稿")
    if args.timeout <= 0 or args.seg_duration <= 0 or args.seg_overlap < 0:
        raise ValueError("时限与分段参数必须是正值，overlap 不得为负")
    _loopback_url(args.server)
    return args


async def _run(args) -> tuple[dict, dict]:
    private_dir = _private_dir(args.private_dir)
    source_path = Path(args.input).resolve(strict=True)
    if not source_path.is_file():
        raise ValueError("输入必须是本机普通文件")
    source_sha = _file_sha256(source_path)
    source_size = source_path.stat().st_size
    pcm_bytes, source_duration = _decode_pcm_bytes(source_path)
    reference_text = Path(args.reference).read_text(encoding="utf-8-sig") if args.reference else None
    normalized_reference = normalize_reference(reference_text) if reference_text is not None else None
    if normalized_reference is not None and not normalized_reference:
        raise ValueError("参考稿归一化后为空")
    health = validate_health(args)
    meter = PayloadMeter()
    recovery_path = private_dir / f"{args.fixture_id}.http-recovery.json"
    if args.protocol == "http":
        transcript, elapsed, handle, final_status = await run_http(args, meter, recovery_path)
        http_job = {"job_id": handle.job_id, "state": final_status.state}
    else:
        transcript, elapsed = await run_ws_v2(args, meter)
        http_job = None
    result = _result_metrics(transcript, source_duration)
    if result["is_final"] is not True or result["timestamp_count"] != result["token_count"]:
        raise ValueError("服务端未返回完整 DONE/final payload 或 token/timestamp 数组不匹配")
    if _file_sha256(source_path) != source_sha:
        raise ValueError("基线期间源文件发生变化")
    hypothesis = result.pop("_hypothesis_normalized")
    if normalized_reference is None:
        comparison = {"status": "not_provided", "cer": None}
    else:
        comparison = {
            "status": args.reference_status,
            "cer": _cer(normalized_reference, hypothesis),
            "normalized_reference_chars": len(normalized_reference),
            "normalized_hypothesis_chars": result["normalized_hypothesis_chars"],
        }
    try:
        ffmpeg_version = subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True, timeout=5, check=True
        ).stdout.splitlines()[0]
    except FileNotFoundError:
        ffmpeg_version = None
    sdk_pyproject = tomllib.loads((REPO_ROOT / "sdk/pyproject.toml").read_text(encoding="utf-8"))
    tool_sha = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=5, check=True,
    ).stdout.strip()
    private_document = {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "fixture_id": args.fixture_id,
        "protocol": args.protocol,
        "server_url": args.server,
        "health": health,
        "expected_server_sha": args.expected_server_sha,
        "expected_model": args.expected_model,
        "source": {
            "path": str(source_path),
            "sha256": source_sha,
            "file_bytes": source_size,
            "decoded_pcm_bytes": pcm_bytes,
            "decoded_pcm_format": "f32le/16000/mono",
            "duration_s": source_duration,
            "input_kind": "wav" if source_path.suffix.lower() == ".wav" else source_path.suffix.lower().lstrip(".") or "container",
        },
        "options": {
            "encoding": "flac" if args.protocol == "ws-v2" else "source-file",
            "seg_duration": args.seg_duration,
            "seg_overlap": args.seg_overlap,
            "automatic_retries": False,
            "retransmission_scope": "HTTP PATCH repeated offset; SDK WS has no application resend path",
        },
        "producer_payloads": {"summary": meter.summary(), "events": meter.events},
        "http_job": http_job,
        "result": result,
        "reference": {
            "status": comparison["status"],
            "path": str(Path(args.reference).resolve()) if args.reference else None,
            "text": reference_text,
            "normalized_text": normalized_reference,
            "comparison": comparison,
        },
        "timing": {"elapsed_s": elapsed},
        "environment": {
            "benchmark_tool_sha": tool_sha,
            "sdk_version": sdk_pyproject["project"]["version"],
            "python": platform.python_version(),
            "websockets": websockets.__version__,
            "httpx": httpx.__version__,
            "ffmpeg": ffmpeg_version,
        },
        "unmeasured": ["TCP/TLS/WebSocket frame overhead", "CPU/RSS peak", "cross-platform variance"],
    }
    report_path = private_dir / f"{args.fixture_id}-{args.protocol}.json"
    _write_private_json(report_path, private_document)
    safe_summary = {
        "status": "ok",
        "schema_version": SCHEMA_VERSION,
        "fixture_id": args.fixture_id,
        "protocol": args.protocol,
        "input_kind": private_document["source"]["input_kind"],
        "source_file_bytes": source_size,
        "decoded_pcm_bytes": pcm_bytes,
        "source_duration_s": source_duration,
        "token_count": result["token_count"],
        "timestamps_monotonic": result["timestamps_monotonic"],
        "timestamps_cover_tokens": result["timestamps_cover_tokens"],
        "timestamps_within_duration": result["timestamps_within_duration"],
        "elapsed_s": elapsed,
        "reference_status": comparison["status"],
        "cer_relative_to_reference": comparison["cer"],
        "producer_payloads": meter.summary(),
        "output_file": report_path.name,
    }
    return safe_summary, private_document


def main() -> int:
    try:
        args = _arguments()
        safe_summary, _ = asyncio.run(_run(args))
        print(json.dumps(safe_summary, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        error = {"error_type": type(exc).__name__}
        code = getattr(exc, "code", None)
        if isinstance(code, str):
            error["error_code"] = code
        print(f"{BASELINE_FAILED} " + json.dumps(error, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
