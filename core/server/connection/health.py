# coding: utf-8
"""服务端 /health HTTP 响应。"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from config_server import ServerConfig as Config
from core.tools.build_info import get_git_sha


PROTOCOL_VERSION = 1


def _encodings() -> list[str]:
    if PROTOCOL_VERSION == 1:
        return ["f32le"]
    from .audio_decoder import available_encodings

    return available_encodings()


def build_health_payload(state, process_manager) -> dict:
    """从主进程的任务与子进程状态组装协议 §5 负载。"""
    process = state.recognize_process
    worker_alive = bool(
        process_manager.models_ready
        and process is not None
        and process.is_alive()
    )
    active_tasks = sum(
        record.status not in {"DONE", "FAILED"}
        for record in state.tasks.values()
    )
    queued_segments = sum(
        len(segments) for segments in state.pending_segments.values()
    )
    aligner = process_manager.aligner_status
    model = Config.model_type

    llama_build = None
    if model.lower() in {"qwen_asr", "fun_asr_nano"} or aligner == "loaded":
        from core.server.engines.llama.build_info import LLAMA_BUILD

        llama_build = LLAMA_BUILD

    return {
        "status": "ok" if worker_alive else "unavailable",
        "protocol_version": PROTOCOL_VERSION,
        "role": "server",
        "encodings": _encodings(),
        "model": model,
        "git_sha": get_git_sha(),
        "llama_build": llama_build,
        "worker_alive": worker_alive,
        "aligner": aligner,
        "active_tasks": active_tasks,
        "queued_segments": queued_segments,
    }


def process_request(connection, request, *, app):
    """只接管 /health，其它 HTTP 请求交给 websockets 默认处理。"""
    if urlsplit(request.path).path != "/health":
        return None

    from websockets.datastructures import Headers
    from websockets.http11 import Response

    payload = build_health_payload(app.state, app.process_manager)
    status_code = 200 if payload["status"] == "ok" else 503
    reason = "OK" if status_code == 200 else "Service Unavailable"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = Headers(
        [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
        ]
    )
    return Response(status_code, reason, headers, body)
