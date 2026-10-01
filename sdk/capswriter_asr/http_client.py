"""CapsWriter ASR 持久 HTTP 文件任务客户端。"""
from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from .client import AsrError, Transcript

_MAX_CHUNK_BYTES = 1024 * 1024
_HASH_READ_BYTES = 64 * 1024
_RECOVERY_VERSION = 1
_UPLOAD_STATES = frozenset({"UPLOADING", "COMMITTED"})
_JOB_STATES = frozenset({"QUEUED", "RUNNING", "DONE", "FAILED"})


@dataclass(frozen=True)
class FileTaskHandle:
    """已提交或仍可显式恢复的文件任务句柄。"""

    upload_id: str
    job_id: str | None
    state: str
    size_bytes: int
    confirmed_offset: int
    expires_at: str | None
    recovery_path: Path | None = None

    @property
    def task_id(self) -> str | None:
        """与服务端任务编号同义，便于调用方从受理句柄取编号。"""
        return self.job_id


@dataclass(frozen=True)
class FileTaskStatus:
    """文件任务的服务端状态，不把未知状态解释为成功。"""

    job_id: str
    state: str
    result_available: bool
    source_available: bool
    error_code: str | None
    time_start: float | None
    time_submit: float | None
    time_complete: float | None
    raw: dict


def _as_path(path: str | os.PathLike[str]) -> Path:
    return Path(path)


def _base_url(value: str) -> str:
    if not isinstance(value, str):
        raise AsrError("invalid_base_url", "HTTP 地址必须是字符串")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise AsrError("invalid_base_url", "HTTP 地址必须使用 http 或 https")
    if parts.query or parts.fragment:
        raise AsrError("invalid_base_url", "HTTP 地址不能包含查询参数或片段")
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _url(base_url: str, path: str) -> str:
    return f"{base_url}{path}"


def _options(
    language: str | None,
    context: str | None,
    model: str | None,
    seg_duration: float,
    seg_overlap: float,
) -> dict:
    for name, value in (
        ("seg_duration", seg_duration),
        ("seg_overlap", seg_overlap),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AsrError("invalid_options", f"{name} 必须是有限数字")
        if not math.isfinite(value) or value < 0:
            raise AsrError("invalid_options", f"{name} 必须是非负有限数字")
    return {
        "language": language,
        "context": context,
        "model": model,
        "seg_duration": float(seg_duration),
        "seg_overlap": float(seg_overlap),
    }


def _chunk_size(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= _MAX_CHUNK_BYTES:
        raise AsrError("invalid_chunk_bytes", "chunk_bytes 必须在 1 到 1048576 之间")
    return value


def _file_identity(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    try:
        before = path.stat().st_size
        with path.open("rb") as source:
            size = 0
            while chunk := source.read(_HASH_READ_BYTES):
                size += len(chunk)
                digest.update(chunk)
        after = path.stat().st_size
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError) as exc:
        raise AsrError("source_unavailable", "无法读取本地源文件") from exc
    if before != after or before != size:
        raise AsrError("source_changed", "计算文件指纹期间源文件发生变化")
    if size <= 0:
        raise AsrError("invalid_source", "不能提交空文件")
    return size, digest.hexdigest()


def _write_recovery(path: Path, payload: dict) -> None:
    """以同目录临时文件、0600 和替换写入恢复信息。"""
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=directory
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _persist(payload: dict, recovery_path: Path) -> None:
    try:
        _write_recovery(recovery_path, payload)
    except (OSError, TypeError, ValueError) as exc:
        raise AsrError(
            "recovery_write_failed",
            "无法可靠写入恢复文件",
            recovery_path=recovery_path,
        ) from exc


def _load_recovery(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AsrError(
            "recovery_invalid",
            "恢复文件不存在、不可读或不是有效 JSON",
            recovery_path=path,
        ) from exc
    if not isinstance(payload, dict):
        raise AsrError("recovery_invalid", "恢复文件必须是 JSON 对象", recovery_path=path)
    required = {
        "version",
        "base_url",
        "create_key",
        "token",
        "size_bytes",
        "sha256",
        "options",
        "confirmed_offset",
    }
    if not required <= payload.keys():
        raise AsrError("recovery_invalid", "恢复文件缺少必要字段", recovery_path=path)
    if payload["version"] != _RECOVERY_VERSION:
        raise AsrError("recovery_invalid", "不支持此恢复文件版本", recovery_path=path)
    if (
        not isinstance(payload["base_url"], str)
        or not isinstance(payload["create_key"], str)
        or not payload["create_key"]
        or not isinstance(payload["token"], str)
        or not payload["token"]
        or not isinstance(payload["sha256"], str)
        or len(payload["sha256"]) != 64
        or any(char not in "0123456789abcdef" for char in payload["sha256"])
        or not isinstance(payload["options"], dict)
        or isinstance(payload["size_bytes"], bool)
        or not isinstance(payload["size_bytes"], int)
        or payload["size_bytes"] <= 0
        or isinstance(payload["confirmed_offset"], bool)
        or not isinstance(payload["confirmed_offset"], int)
        or not 0 <= payload["confirmed_offset"] <= payload["size_bytes"]
    ):
        raise AsrError("recovery_invalid", "恢复文件字段无效", recovery_path=path)
    options = payload["options"]
    if set(options) != {"language", "context", "model", "seg_duration", "seg_overlap"}:
        raise AsrError("recovery_invalid", "恢复文件选项字段无效", recovery_path=path)
    for field in ("language", "context", "model"):
        if options[field] is not None and not isinstance(options[field], str):
            raise AsrError("recovery_invalid", "恢复文件选项字段无效", recovery_path=path)
    for field in ("seg_duration", "seg_overlap"):
        value = options[field]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise AsrError("recovery_invalid", "恢复文件选项字段无效", recovery_path=path)
    try:
        payload["base_url"] = _base_url(payload["base_url"])
    except AsrError as exc:
        raise AsrError("recovery_invalid", "恢复文件中的 HTTP 地址无效", recovery_path=path) from exc
    return payload


def _local_recovery(
    path: Path,
    base_url: str,
    create_key: str,
    token: str,
    size_bytes: int,
    digest: str,
    options: dict,
) -> dict:
    return {
        "version": _RECOVERY_VERSION,
        "base_url": base_url,
        "create_key": create_key,
        "token": token,
        "size_bytes": size_bytes,
        "sha256": digest,
        "options": options,
        "source_name": path.name,
        "upload_id": None,
        "job_id": None,
        "confirmed_offset": 0,
        "expires_at": None,
    }


def _safe_message(value: object, token: str) -> str:
    if not isinstance(value, str) or not value:
        return "HTTP 服务端返回错误"
    return value.replace(token, "<redacted>")


def _error_response(response: httpx.Response, token: str, recovery_path: Path) -> AsrError:
    if 300 <= response.status_code < 400:
        return AsrError(
            "http_redirect",
            "服务端返回重定向，SDK不会跟随",
            recovery_path=recovery_path,
        )
    code: str | None = None
    message: object = None
    try:
        body = response.json()
    except (UnicodeDecodeError, json.JSONDecodeError):
        body = None
    if isinstance(body, dict):
        if isinstance(body.get("code"), str) and body["code"] and token not in body["code"]:
            code = body["code"]
        message = body.get("message")
    if code is None:
        code = {
            401: "permission_denied",
            403: "permission_denied",
            404: "not_found",
            409: "conflict",
            410: "expired",
            413: "payload_too_large",
            429: "overloaded",
            503: "service_unavailable",
        }.get(response.status_code, "http_error")
    return AsrError(
        code,
        _safe_message(message, token) if message is not None else f"HTTP 请求失败（状态码 {response.status_code}）",
        recovery_path=recovery_path,
    )


async def _request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    token: str,
    recovery_path: Path,
    headers: dict[str, str] | None = None,
    json_body: dict | None = None,
    content: bytes | None = None,
) -> httpx.Response:
    request_headers = {"Authorization": f"Bearer {token}"}
    if headers:
        request_headers.update(headers)
    try:
        return await client.request(
            method,
            url,
            headers=request_headers,
            json=json_body,
            content=content,
        )
    except httpx.TimeoutException as exc:
        raise AsrError("timeout", "HTTP 请求超时", recovery_path=recovery_path) from exc
    except httpx.TransportError as exc:
        raise AsrError("connection_lost", "HTTP 连接失败", recovery_path=recovery_path) from exc


def _json_success(response: httpx.Response, allowed: set[int], token: str, recovery_path: Path) -> dict:
    if response.status_code not in allowed:
        raise _error_response(response, token, recovery_path)
    try:
        payload = response.json()
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AsrError("invalid_response", "服务端成功响应不是有效 JSON", recovery_path=recovery_path) from exc
    if not isinstance(payload, dict):
        raise AsrError("invalid_response", "服务端成功响应不是 JSON 对象", recovery_path=recovery_path)
    return payload


def _required_string(payload: dict, field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(field)
    return value


def _required_integer(payload: dict, field: str) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(field)
    return value


def _upload_info(
    payload: dict,
    *,
    expected_size: int,
    recovery_path: Path,
    token: str,
) -> tuple[str, str, int, str | None, str | None]:
    try:
        upload_id = _required_string(payload, "upload_id")
        state = _required_string(payload, "state")
        size_bytes = _required_integer(payload, "size_bytes")
        offset = _required_integer(payload, "confirmed_offset")
        expires_at = payload.get("expires_at")
        if not isinstance(expires_at, str) or not expires_at:
            raise ValueError("expires_at")
        job_id = payload.get("job_id")
        if job_id is not None and (not isinstance(job_id, str) or not job_id):
            raise ValueError("job_id")
        if state not in _UPLOAD_STATES or size_bytes != expected_size or not 0 <= offset <= size_bytes:
            raise ValueError("identity/state")
    except (TypeError, ValueError) as exc:
        raise AsrError("invalid_response", "服务端上传响应缺少有效字段", recovery_path=recovery_path) from exc
    return upload_id, state, offset, expires_at, job_id


def _job_info(payload: dict, recovery_path: Path) -> tuple[str, str]:
    try:
        job_id = _required_string(payload, "job_id")
        state = _required_string(payload, "state")
        if state not in _JOB_STATES:
            raise ValueError("state")
    except (TypeError, ValueError) as exc:
        raise AsrError("invalid_response", "服务端受理响应缺少有效字段", recovery_path=recovery_path) from exc
    return job_id, state


def _handle(recovery: dict, recovery_path: Path) -> FileTaskHandle:
    upload_id = recovery.get("upload_id")
    if not isinstance(upload_id, str) or not upload_id:
        raise AsrError("invalid_response", "恢复信息中没有上传编号", recovery_path=recovery_path)
    return FileTaskHandle(
        upload_id=upload_id,
        job_id=recovery.get("job_id"),
        state=recovery.get("state", "UPLOADING"),
        size_bytes=recovery["size_bytes"],
        confirmed_offset=recovery["confirmed_offset"],
        expires_at=recovery.get("expires_at"),
        recovery_path=recovery_path,
    )


def _record_upload(
    recovery: dict,
    recovery_path: Path,
    *,
    upload_id: str,
    state: str,
    offset: int,
    expires_at: str | None,
    job_id: str | None,
) -> None:
    recovery.update(
        {
            "upload_id": upload_id,
            "state": state,
            "confirmed_offset": offset,
            "expires_at": expires_at,
        }
    )
    if job_id is not None:
        recovery["job_id"] = job_id
    _persist(recovery, recovery_path)


async def _create_upload(
    client: httpx.AsyncClient,
    base_url: str,
    recovery: dict,
    recovery_path: Path,
) -> tuple[str, str, int, str | None, str | None]:
    response = await _request(
        client,
        "POST",
        _url(base_url, "/v1/uploads"),
        token=recovery["token"],
        recovery_path=recovery_path,
        headers={"Idempotency-Key": recovery["create_key"]},
        json_body={
            "size_bytes": recovery["size_bytes"],
            "sha256": recovery["sha256"],
            "options": recovery["options"],
        },
    )
    payload = _json_success(response, {200, 201}, recovery["token"], recovery_path)
    return _upload_info(
        payload,
        expected_size=recovery["size_bytes"],
        recovery_path=recovery_path,
        token=recovery["token"],
    )


async def _get_upload(
    client: httpx.AsyncClient,
    base_url: str,
    recovery: dict,
    recovery_path: Path,
) -> tuple[str, str, int, str | None, str | None]:
    response = await _request(
        client,
        "GET",
        _url(base_url, f"/v1/uploads/{recovery['upload_id']}"),
        token=recovery["token"],
        recovery_path=recovery_path,
    )
    payload = _json_success(response, {200}, recovery["token"], recovery_path)
    try:
        if payload.get("sha256") != recovery["sha256"]:
            raise ValueError("sha256")
    except (TypeError, ValueError) as exc:
        raise AsrError("invalid_response", "服务端上传身份与恢复信息不一致", recovery_path=recovery_path) from exc
    return _upload_info(
        payload,
        expected_size=recovery["size_bytes"],
        recovery_path=recovery_path,
        token=recovery["token"],
    )


async def _commit_upload(
    client: httpx.AsyncClient,
    base_url: str,
    recovery: dict,
    recovery_path: Path,
) -> tuple[str, str]:
    response = await _request(
        client,
        "POST",
        _url(base_url, f"/v1/uploads/{recovery['upload_id']}/commit"),
        token=recovery["token"],
        recovery_path=recovery_path,
        content=b"",
    )
    payload = _json_success(response, {200, 202}, recovery["token"], recovery_path)
    return _job_info(payload, recovery_path)


async def _finish_upload(
    client: httpx.AsyncClient,
    base_url: str,
    path: Path,
    recovery: dict,
    recovery_path: Path,
    chunk_bytes: int,
    *,
    upload_info: tuple[str, str, int, str | None, str | None],
) -> FileTaskHandle:
    upload_id, state, offset, expires_at, job_id = upload_info
    _record_upload(
        recovery,
        recovery_path,
        upload_id=upload_id,
        state=state,
        offset=offset,
        expires_at=expires_at,
        job_id=job_id,
    )
    if state == "COMMITTED":
        if job_id is None:
            raise AsrError("invalid_response", "已提交上传缺少任务编号", recovery_path=recovery_path)
        return _handle(recovery, recovery_path)

    try:
        with path.open("rb") as source:
            source.seek(offset)
            while offset < recovery["size_bytes"]:
                chunk = source.read(min(chunk_bytes, recovery["size_bytes"] - offset))
                if not chunk:
                    break
                response = await _request(
                    client,
                    "PATCH",
                    _url(base_url, f"/v1/uploads/{upload_id}"),
                    token=recovery["token"],
                    recovery_path=recovery_path,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(len(chunk)),
                        "Upload-Offset": str(offset),
                    },
                    content=chunk,
                )
                if response.status_code != 204:
                    raise _error_response(response, recovery["token"], recovery_path)
                offset_header = response.headers.get("Upload-Offset")
                try:
                    new_offset = int(offset_header) if offset_header is not None else -1
                except ValueError:
                    new_offset = -1
                if new_offset != offset + len(chunk):
                    raise AsrError("invalid_response", "服务端确认了无效的上传位置", recovery_path=recovery_path)
                offset = new_offset
                recovery["confirmed_offset"] = offset
                _persist(recovery, recovery_path)
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError) as exc:
        raise AsrError("source_unavailable", "上传期间无法读取本地源文件", recovery_path=recovery_path) from exc

    current_size, current_digest = _file_identity(path)
    if current_size != recovery["size_bytes"] or current_digest != recovery["sha256"]:
        raise AsrError("source_changed", "上传期间源文件发生变化，未提交任务", recovery_path=recovery_path)
    job_id, state = await _commit_upload(client, base_url, recovery, recovery_path)
    recovery["job_id"] = job_id
    recovery["state"] = state
    _persist(recovery, recovery_path)
    return _handle(recovery, recovery_path)


async def submit_file_http(
    path,
    base_url,
    *,
    resume_path,
    language=None,
    context=None,
    model=None,
    seg_duration=15.0,
    seg_overlap=2.0,
    chunk_bytes=1048576,
) -> FileTaskHandle:
    """保存恢复凭据后提交原文件；返回受理句柄，不等待识别完成。"""
    recovery_path = _as_path(resume_path)
    if recovery_path.exists():
        raise AsrError(
            "recovery_exists",
            "恢复文件已存在，请使用 resume_file_http",
            recovery_path=recovery_path,
        )
    source_path = _as_path(path)
    base = _base_url(base_url)
    chunk = _chunk_size(chunk_bytes)
    options = _options(language, context, model, seg_duration, seg_overlap)
    size_bytes, digest = _file_identity(source_path)
    recovery = _local_recovery(
        source_path,
        base,
        secrets.token_hex(32),
        secrets.token_hex(32),
        size_bytes,
        digest,
        options,
    )
    _persist(recovery, recovery_path)
    try:
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(retries=0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            upload_info = await _create_upload(client, base, recovery, recovery_path)
            return await _finish_upload(
                client,
                base,
                source_path,
                recovery,
                recovery_path,
                chunk,
                upload_info=upload_info,
            )
    except AsrError as exc:
        if exc.recovery_path is None:
            exc.recovery_path = recovery_path
        raise


async def resume_file_http(path, base_url, *, resume_path) -> FileTaskHandle:
    """核对源文件后显式查询并从服务端确认位置继续上传。"""
    recovery_path = _as_path(resume_path)
    recovery = _load_recovery(recovery_path)
    base = _base_url(base_url)
    if recovery["base_url"] != base:
        raise AsrError("recovery_mismatch", "恢复文件绑定了不同的 HTTP 地址", recovery_path=recovery_path)
    source_path = _as_path(path)
    size_bytes, digest = _file_identity(source_path)
    if size_bytes != recovery["size_bytes"] or digest != recovery["sha256"]:
        raise AsrError("source_changed", "本地源文件身份与恢复信息不一致", recovery_path=recovery_path)
    try:
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(retries=0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            if recovery.get("upload_id"):
                upload_info = await _get_upload(client, base, recovery, recovery_path)
            else:
                upload_info = await _create_upload(client, base, recovery, recovery_path)
            return await _finish_upload(
                client,
                base,
                source_path,
                recovery,
                recovery_path,
                _MAX_CHUNK_BYTES,
                upload_info=upload_info,
            )
    except AsrError as exc:
        if exc.recovery_path is None:
            exc.recovery_path = recovery_path
        raise


def _recovery_for_job(base_url: str, resume_path) -> tuple[str, Path, dict]:
    path = _as_path(resume_path)
    recovery = _load_recovery(path)
    base = _base_url(base_url)
    if recovery["base_url"] != base:
        raise AsrError("recovery_mismatch", "恢复文件绑定了不同的 HTTP 地址", recovery_path=path)
    job_id = recovery.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise AsrError("job_not_available", "恢复文件中还没有受理任务编号", recovery_path=path)
    return base, path, recovery


async def get_file_job_http(base_url, *, resume_path) -> FileTaskStatus:
    """只查询服务端任务状态，不轮询、不改变任务。"""
    base, recovery_path, recovery = _recovery_for_job(base_url, resume_path)
    try:
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(retries=0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await _request(
                client,
                "GET",
                _url(base, f"/v1/jobs/{recovery['job_id']}"),
                token=recovery["token"],
                recovery_path=recovery_path,
            )
            payload = _json_success(response, {200}, recovery["token"], recovery_path)
    except AsrError as exc:
        if exc.recovery_path is None:
            exc.recovery_path = recovery_path
        raise
    try:
        job_id = _required_string(payload, "job_id")
        state = _required_string(payload, "state")
        result_available = payload["result_available"]
        source_available = payload["source_available"]
        error_code = payload["error_code"]
        time_start = payload["time_start"]
        time_submit = payload["time_submit"]
        time_complete = payload["time_complete"]
        if (
            job_id != recovery["job_id"]
            or state not in _JOB_STATES
            or not isinstance(result_available, bool)
            or not isinstance(source_available, bool)
            or (error_code is not None and not isinstance(error_code, str))
            or any(
                value is not None
                and (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                )
                for value in (time_start, time_submit, time_complete)
            )
        ):
            raise ValueError("job")
    except (KeyError, TypeError, ValueError) as exc:
        raise AsrError("invalid_response", "服务端任务状态缺少有效字段", recovery_path=recovery_path) from exc
    return FileTaskStatus(
        job_id=job_id,
        state=state,
        result_available=result_available,
        source_available=source_available,
        error_code=error_code,
        time_start=None if time_start is None else float(time_start),
        time_submit=None if time_submit is None else float(time_submit),
        time_complete=None if time_complete is None else float(time_complete),
        raw=payload,
    )


def _transcript_from_http(payload: dict, recovery_path: Path) -> Transcript:
    try:
        task_id = _required_string(payload, "task_id")
        is_final = payload["is_final"]
        duration = payload["duration"]
        time_start = payload["time_start"]
        time_submit = payload["time_submit"]
        time_complete = payload["time_complete"]
        text = payload["text"]
        text_accu = payload["text_accu"]
        tokens = payload["tokens"]
        timestamps = payload["timestamps"]
        numbers = (duration, time_start, time_submit, time_complete)
        if (
            not is_final
            or not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) for value in numbers)
            or not isinstance(text, str)
            or not isinstance(text_accu, str)
            or not isinstance(tokens, list)
            or not all(isinstance(token, str) for token in tokens)
            or not isinstance(timestamps, list)
            or not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) for value in timestamps)
            or len(tokens) != len(timestamps)
        ):
            raise ValueError("result")
    except (KeyError, TypeError, ValueError) as exc:
        raise AsrError("invalid_response", "服务端结果缺少完整字段", recovery_path=recovery_path) from exc
    return Transcript(
        text=text,
        tokens=tokens,
        timestamps=timestamps,
        duration=float(duration),
        raw=payload,
        task_id=task_id,
        is_final=is_final,
        time_start=float(time_start),
        time_submit=float(time_submit),
        time_complete=float(time_complete),
        text_accu=text_accu,
    )


async def get_file_result_http(base_url, *, resume_path) -> Transcript:
    """只领取 DONE 的完整结果；不自动查询、轮询或重试。"""
    base, recovery_path, recovery = _recovery_for_job(base_url, resume_path)
    try:
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(retries=0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await _request(
                client,
                "GET",
                _url(base, f"/v1/jobs/{recovery['job_id']}/result"),
                token=recovery["token"],
                recovery_path=recovery_path,
            )
            payload = _json_success(response, {200}, recovery["token"], recovery_path)
    except AsrError as exc:
        if exc.recovery_path is None:
            exc.recovery_path = recovery_path
        raise
    return _transcript_from_http(payload, recovery_path)


def submit_file_http_sync(
    path,
    base_url,
    *,
    resume_path,
    language=None,
    context=None,
    model=None,
    seg_duration=15.0,
    seg_overlap=2.0,
    chunk_bytes=1048576,
) -> FileTaskHandle:
    """submit_file_http 的同步入口。"""
    import asyncio

    return asyncio.run(
        submit_file_http(
            path,
            base_url,
            resume_path=resume_path,
            language=language,
            context=context,
            model=model,
            seg_duration=seg_duration,
            seg_overlap=seg_overlap,
            chunk_bytes=chunk_bytes,
        )
    )


def resume_file_http_sync(path, base_url, *, resume_path) -> FileTaskHandle:
    """resume_file_http 的同步入口。"""
    import asyncio

    return asyncio.run(resume_file_http(path, base_url, resume_path=resume_path))


def get_file_job_http_sync(base_url, *, resume_path) -> FileTaskStatus:
    """get_file_job_http 的同步入口。"""
    import asyncio

    return asyncio.run(get_file_job_http(base_url, resume_path=resume_path))


def get_file_result_http_sync(base_url, *, resume_path) -> Transcript:
    """get_file_result_http 的同步入口。"""
    import asyncio

    return asyncio.run(get_file_result_http(base_url, resume_path=resume_path))

