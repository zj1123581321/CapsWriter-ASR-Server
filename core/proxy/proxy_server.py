# coding: utf-8
"""WebSocket server entrypoint for the ASR load-balancing proxy."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import time
from collections import deque
from html import escape
from typing import Iterable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from core.tools.build_info import get_git_sha
from core.logger import setup_logger

from .backend import BackendState
from .router import NoCompatibleBackendError, NoHealthyBackendError, TaskRouter


def _read_backend_health(url: str) -> tuple[int, bytes]:
    parts = urlsplit(url)
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme)
    health_url = urlunsplit((scheme, parts.netloc, "/health", "", ""))
    request = Request(health_url, method="GET")
    try:
        with urlopen(request, timeout=5.0) as response:
            return response.status, response.read()
    except HTTPError as exc:
        return exc.code, exc.read()


class _ProbeContextManager:
    def __init__(self, ws_server_cm, proxy):
        self._ws_cm = ws_server_cm
        self._proxy = proxy
        self._probe_task = None

    async def __aenter__(self):
        await self._proxy._probe_backends()
        server = await self._ws_cm.__aenter__()
        self._probe_task = asyncio.create_task(self._proxy._health_probe_loop())
        return server

    async def __aexit__(self, *exc):
        if self._probe_task:
            self._probe_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._probe_task
        return await self._ws_cm.__aexit__(*exc)


class ProxyServer:
    """Accepts client WebSockets and proxies task streams to ASR backends."""

    def __init__(
        self,
        listen_addr: str,
        listen_port: int,
        backends: Iterable[BackendState],
        cooldown_seconds: int = 60,
        log_level: str = "DEBUG",
        probe_interval: float = 30.0,
        max_probe_interval: float = 300.0,
    ):
        self.listen_addr = listen_addr
        self.listen_port = listen_port
        self.backends = list(backends)
        self.cooldown_seconds = cooldown_seconds
        self.probe_interval = probe_interval
        self.max_probe_interval = max_probe_interval
        self.logger = setup_logger("proxy", level=log_level, log_filename="proxy")
        self.git_sha = get_git_sha()
        self._server = None
        self._probe_task = None
        self.task_history = deque(maxlen=1000)

    def serve(self):
        import websockets

        ws_server = websockets.serve(
            self.handle_client,
            self.listen_addr,
            self.listen_port,
            max_size=None,
            ping_interval=None,
            process_request=self.process_request,
        )
        return _ProbeContextManager(ws_server, self)

    def process_request(self, connection, request):
        from websockets.datastructures import Headers
        from websockets.http11 import Response

        parsed = urlsplit(request.path)
        if parsed.path == "/health":
            payload = self.health_payload()
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
        if parsed.path != "/status":
            return None

        payload = self.status_payload()
        query = parse_qs(parsed.query, keep_blank_values=True)
        accept = request.headers.get("Accept", "")
        wants_html = "html" in query or "text/html" in accept
        if wants_html:
            body = self.status_html(payload).encode("utf-8")
            headers = Headers([("Content-Type", "text/html; charset=utf-8")])
        else:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers = Headers([("Content-Type", "application/json; charset=utf-8")])

        headers["Content-Length"] = str(len(body))
        headers["Cache-Control"] = "no-store"
        return Response(200, "OK", headers, body)

    def status_payload(self) -> dict:
        history = list(self.task_history)
        return {
            "backends": [
                {
                    "id": backend.id,
                    "url": backend.url,
                    "healthy": backend.healthy,
                    "protocol_version": backend.protocol_version,
                    "git_sha": backend.git_sha,
                    "active_tasks": backend.active_tasks,
                    "avg_latency": backend.avg_latency,
                    "latency_samples": backend.latency_samples,
                    "weight": backend.weight,
                    "consecutive_failures": backend.consecutive_failures,
                    "last_failure_time": backend.last_failure_time,
                }
                for backend in self.backends
            ],
            "active_tasks_total": sum(backend.active_tasks for backend in self.backends),
            "task_history": {
                "total": len(history),
                "completed": sum(1 for item in history if item["status"] == "completed"),
                "failed": sum(1 for item in history if item["status"] == "failed"),
                "cancelled": sum(1 for item in history if item["status"] == "cancelled"),
                "recent": history[-20:],
            },
            "generated_at": time.time(),
        }

    def health_payload(self) -> dict:
        healthy_v2_backends = [
            backend
            for backend in self.backends
            if backend.healthy and backend.protocol_version >= 2
        ]
        encodings = sorted(
            {
                encoding
                for backend in healthy_v2_backends
                for encoding in backend.encodings
            }
        )
        return {
            "status": "ok" if healthy_v2_backends else "unavailable",
            "protocol_version": 2,
            "role": "proxy",
            "git_sha": self.git_sha,
            "encodings": encodings,
            "backends": [
                {
                    "url": backend.url,
                    "healthy": backend.healthy,
                    "protocol_version": backend.protocol_version,
                    "model": backend.model,
                    "encodings": list(backend.encodings),
                    "git_sha": backend.git_sha,
                }
                for backend in self.backends
            ],
        }

    def status_html(self, payload: dict) -> str:
        backend_rows = "\n".join(
            "<tr>"
            f"<td>{escape(backend['id'])}</td>"
            f"<td>{escape(backend['url'])}</td>"
            f"<td>{'yes' if backend['healthy'] else 'no'}</td>"
            f"<td>{backend['protocol_version']}</td>"
            f"<td>{escape(str(backend['git_sha'] or ''))}</td>"
            f"<td>{backend['active_tasks']}</td>"
            f"<td>{backend['avg_latency']:.3f}</td>"
            f"<td>{backend['latency_samples']}</td>"
            f"<td>{backend['weight']:.3f}</td>"
            f"<td>{backend['consecutive_failures']}</td>"
            f"<td>{backend['last_failure_time']:.3f}</td>"
            "</tr>"
            for backend in payload["backends"]
        )
        history_rows = "\n".join(
            "<tr>"
            f"<td>{escape(item['task_id'])}</td>"
            f"<td>{escape(item['backend_id'])}</td>"
            f"<td>{escape(item['status'])}</td>"
            f"<td>{item['duration']:.3f}</td>"
            f"<td>{item['timestamp']:.3f}</td>"
            "</tr>"
            for item in payload["task_history"]["recent"]
        )
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>CapsWriter Proxy Status</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 24px; color: #1f2937; }}
    h1 {{ font-size: 24px; margin: 0 0 16px; }}
    h2 {{ font-size: 18px; margin: 24px 0 8px; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #d1d5db; padding: 8px 10px; text-align: left; }}
    th {{ background: #f3f4f6; }}
    .summary {{ display: flex; gap: 16px; margin-bottom: 16px; }}
    .metric {{ border: 1px solid #d1d5db; padding: 10px 12px; }}
  </style>
</head>
<body>
  <h1>CapsWriter Proxy Status</h1>
  <div class="summary">
    <div class="metric">Active tasks: {payload['active_tasks_total']}</div>
    <div class="metric">History: {payload['task_history']['total']}</div>
    <div class="metric">Completed: {payload['task_history']['completed']}</div>
    <div class="metric">Failed: {payload['task_history']['failed']}</div>
  </div>
  <h2>Backends</h2>
  <table>
    <thead><tr><th>ID</th><th>URL</th><th>Healthy</th><th>Protocol</th><th>Git SHA</th><th>Active</th><th>Avg latency</th><th>Samples</th><th>Weight</th><th>Failures</th><th>Last failure</th></tr></thead>
    <tbody>{backend_rows}</tbody>
  </table>
  <h2>Recent Tasks</h2>
  <table>
    <thead><tr><th>Task ID</th><th>Backend</th><th>Status</th><th>Duration</th><th>Timestamp</th></tr></thead>
    <tbody>{history_rows}</tbody>
  </table>
</body>
</html>"""

    async def start(self) -> None:
        self.logger.info(
            "正在拉起 ASR 代理服务 (监听: %s:%s, 后端数: %s)",
            self.listen_addr,
            self.listen_port,
            len(self.backends),
        )
        async with self.serve() as server:
            self._server = server
            await asyncio.Future()

    async def _health_probe_loop(self) -> None:
        while True:
            await asyncio.sleep(self.probe_interval)
            await self._probe_backends()

    async def _probe_backends(self) -> None:
        await asyncio.gather(*(self._probe_backend(backend) for backend in self.backends))

    async def _probe_backend(self, backend: BackendState) -> None:
        try:
            status_code, body = await asyncio.wait_for(
                asyncio.to_thread(_read_backend_health, backend.url),
                timeout=5.0,
            )
        except (OSError, URLError, TimeoutError) as exc:
            await self._probe_legacy_websocket(backend, exc)
            return

        if status_code == 503:
            backend.record_health_failure()
            self.logger.debug("后端 /health 返回 503: backend=%s url=%s", backend.id, backend.url)
            return
        if status_code != 200:
            backend.record_legacy_health()
            return

        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            backend.record_legacy_health()
            return
        if not isinstance(payload, dict) or type(payload.get("protocol_version")) is not int:
            backend.record_legacy_health()
            return
        encodings = payload.get("encodings", [])
        payload["encodings"] = (
            encodings
            if isinstance(encodings, list) and all(isinstance(item, str) for item in encodings)
            else []
        )
        backend.record_health(payload)

    async def _probe_legacy_websocket(self, backend: BackendState, http_error: Exception) -> None:
        import websockets

        kwargs = {"uri": backend.url, "max_size": None, "ping_interval": None}
        version_parts = tuple(int(part) for part in websockets.__version__.split(".")[:2])
        if version_parts >= (14, 0):
            kwargs["proxy"] = None
        try:
            ws = await asyncio.wait_for(websockets.connect(**kwargs), timeout=5.0)
        except (OSError, TimeoutError, websockets.exceptions.WebSocketException) as exc:
            backend.record_health_failure()
            self.logger.debug(
                "后端 HTTP 探测失败且 WebSocket 不可用: backend=%s url=%s http_error=%s ws_error=%s",
                backend.id,
                backend.url,
                http_error,
                exc,
            )
            return
        await ws.close()
        backend.record_legacy_health()
        self.logger.debug("后端无 /health，按 v1 记录: backend=%s url=%s", backend.id, backend.url)

    async def handle_client(self, client_ws) -> None:
        remote = getattr(client_ws, "remote_address", None)
        self.logger.info("代理客户端已连接: %s", remote)
        router = TaskRouter(
            self.backends,
            task_history=self.task_history,
        )

        try:
            async for raw_message in client_ws:
                try:
                    await router.route_client_message(raw_message, client_ws)
                except NoCompatibleBackendError as exc:
                    await client_ws.send(
                        json.dumps(
                            {
                                "type": "error",
                                "task_id": exc.task_id,
                                "code": "no_backend",
                                "message": str(exc),
                                "retryable": True,
                            },
                            ensure_ascii=False,
                        )
                    )
                    await client_ws.close(code=4000, reason="no_backend")
                    break
                except NoHealthyBackendError:
                    self.logger.error("没有健康后端可用，关闭客户端连接: %s", remote)
                    await client_ws.close(code=1013, reason="No healthy ASR backend")
                    break
                except Exception:
                    self.logger.error("代理消息处理失败，关闭客户端连接: %s", remote, exc_info=True)
                    await client_ws.close(code=1011, reason="ASR proxy routing failed")
                    break
        finally:
            await router.close_all()
            self.logger.info("代理客户端已断开: %s", remote)


def build_proxy_from_config(config: Optional[object] = None) -> ProxyServer:
    if config is None:
        from config_proxy import ProxyConfig as config

    backends = [
        BackendState(
            id=f"backend-{index}",
            url=url,
            weight=weight,
            max_connect_failures=config.max_connect_failures,
        )
        for index, (url, weight) in enumerate(_parse_backend_config(config.backends))
    ]
    return ProxyServer(
        config.listen_addr,
        config.listen_port,
        backends,
        cooldown_seconds=getattr(config, "cooldown_seconds", 60),
        log_level=getattr(config, "log_level", "DEBUG"),
        probe_interval=getattr(config, "probe_interval", 30.0),
    )


def _parse_backend_config(backends_config):
    parsed = []
    for index, item in enumerate(backends_config):
        if isinstance(item, str):
            url = item
            weight = 1.0
        elif isinstance(item, dict):
            url = item["url"]
            weight = float(item.get("weight", 1.0))
        else:
            url, weight = item
            weight = float(weight)

        if not math.isfinite(weight) or weight <= 0:
            raise ValueError(f"backend weight must be > 0: index={index} url={url!r} weight={weight}")
        parsed.append((url, weight))
    return parsed


def run_proxy() -> None:
    proxy = build_proxy_from_config()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(proxy.start())
