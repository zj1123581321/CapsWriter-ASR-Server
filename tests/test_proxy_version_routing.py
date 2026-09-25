# coding: utf-8

import asyncio
import json
from contextlib import asynccontextmanager
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

websockets = pytest.importorskip("websockets")

from core.proxy.backend import BackendState
from core.proxy.proxy_server import ProxyServer
from core.proxy.router import TaskRouter
from core.tools import build_info


def make_audio(task_id: str, encoding: str | None = None, data: str = "") -> str:
    message = {
        "task_id": task_id,
        "source": "file",
        "data": data,
        "is_final": False,
        "time_start": 1.0,
    }
    if encoding is not None:
        message["encoding"] = encoding
    return json.dumps(message)


def make_recognition(task_id: str) -> str:
    return json.dumps(
        {
            "task_id": task_id,
            "is_final": True,
            "duration": 1.0,
            "time_start": 1.0,
            "time_submit": 2.0,
            "time_complete": 3.0,
            "text": "ok",
        }
    )


def _http_response(status: int, body: bytes = b""):
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    reason = {200: "OK", 426: "Upgrade Required", 503: "Service Unavailable"}[status]
    headers = Headers(
        [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
        ]
    )
    return Response(status, reason, headers, body)


@asynccontextmanager
async def fake_backend(handler, health_status=200, health_payload=None):
    state = {"status": health_status}
    payload = health_payload or {
        "protocol_version": 2,
        "encodings": ["f32le", "flac"],
        "model": "test-model",
        "git_sha": "backend-sha",
        "llama_build": "llama-test",
        "worker_alive": True,
        "aligner": "none",
        "status": "ok",
    }

    def process_request(connection, request):
        if request.path != "/health":
            return None
        status = state["status"]
        if status is None:
            return None
        body = json.dumps(payload).encode("utf-8") if status == 200 else b"{}"
        return _http_response(status, body)

    async with websockets.serve(
        handler,
        "127.0.0.1",
        0,
        max_size=None,
        process_request=process_request,
    ) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}", state


def _fetch(url: str, accept: str | None = None):
    request = Request(url, headers={"Accept": accept} if accept else {})
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, response.headers, response.read()
    except HTTPError as response:
        return response.code, response.headers, response.read()


async def fetch(url: str, accept: str | None = None):
    return await asyncio.to_thread(_fetch, url, accept)


def test_git_sha_unavailable_returns_unknown_and_logs_warning(monkeypatch):
    warnings = []

    def unavailable_git(*args, **kwargs):
        raise OSError("git missing")

    monkeypatch.setattr(build_info.subprocess, "run", unavailable_git)
    monkeypatch.setattr(
        build_info.logger,
        "warning",
        lambda message, *args: warnings.append(message % args),
    )

    assert build_info.get_git_sha() == "unknown"
    assert warnings == ["git_sha 不可用: git missing"]


@pytest.mark.asyncio
async def test_v2_encoding_routes_only_to_v2_and_v1_tasks_keep_both_candidates():
    hits = {"v2": [], "v1": []}
    unencoded_received = asyncio.Event()
    release_unencoded = asyncio.Event()

    def handler_for(name):
        async def handler(ws):
            raw = await ws.recv()
            message = json.loads(raw)
            hits[name].append(message["task_id"])
            if "encoding" not in message:
                if sum(len(hits[backend]) for backend in hits) == 20:
                    unencoded_received.set()
                await release_unencoded.wait()
            await ws.send(make_recognition(message["task_id"]))

        return handler

    async with fake_backend(handler_for("v2")) as (url_v2, _):
        async with fake_backend(handler_for("v1"), health_status=None) as (url_v1, _):
            backends = [
                BackendState(id="v2", url=url_v2),
                BackendState(id="v1", url=url_v1),
            ]
            proxy = ProxyServer("127.0.0.1", 0, backends)
            async with proxy.serve() as server:
                proxy_url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
                async with websockets.connect(proxy_url, max_size=None) as client:
                    for index in range(10):
                        await client.send(make_audio(f"v2-{index}", encoding="flac"))
                        await asyncio.wait_for(client.recv(), timeout=3)

                    assert hits["v2"] == [f"v2-{index}" for index in range(10)]
                    assert hits["v1"] == []

                    for index in range(10):
                        await client.send(make_audio(f"v1-{index}"))
                    await asyncio.wait_for(unencoded_received.wait(), timeout=3)
                    assert hits["v1"], "不带 encoding 的任务候选集应包含 v1 后端"
                    release_unencoded.set()
                    results = [json.loads(await asyncio.wait_for(client.recv(), timeout=3)) for _ in range(10)]

    assert {item["task_id"] for item in results} == {f"v1-{index}" for index in range(10)}


@pytest.mark.asyncio
async def test_v2_task_without_compatible_backend_returns_no_backend_and_closes_4000():
    async def unused_handler(ws):
        await ws.recv()

    async with fake_backend(unused_handler, health_status=None) as (url, _):
        backend = BackendState(id="legacy", url=url)
        proxy = ProxyServer("127.0.0.1", 0, [backend])
        async with proxy.serve() as server:
            proxy_url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            async with websockets.connect(proxy_url, max_size=None) as client:
                await client.send(make_audio("needs-v2", encoding="flac"))
                error = json.loads(await asyncio.wait_for(client.recv(), timeout=3))
                with pytest.raises(websockets.exceptions.ConnectionClosed) as closed:
                    await asyncio.wait_for(client.recv(), timeout=3)

    assert error["type"] == "error"
    assert error["task_id"] == "needs-v2"
    assert error["code"] == "no_backend"
    assert error["retryable"] is True
    assert closed.value.rcvd.code == 4000
    assert backend.active_tasks == 0


@pytest.mark.asyncio
async def test_v2_task_with_unsupported_encoding_returns_no_backend():
    async def unused_handler(ws):
        await ws.recv()

    payload = {"protocol_version": 2, "encodings": ["f32le"], "model": "test", "git_sha": "sha"}
    async with fake_backend(unused_handler, health_payload=payload) as (url, _):
        proxy = ProxyServer("127.0.0.1", 0, [BackendState(id="v2", url=url)])
        async with proxy.serve() as server:
            proxy_url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            async with websockets.connect(proxy_url, max_size=None) as client:
                await client.send(make_audio("needs-ogg", encoding="ogg_opus"))
                error = json.loads(await asyncio.wait_for(client.recv(), timeout=3))
                with pytest.raises(websockets.exceptions.ConnectionClosed) as closed:
                    await asyncio.wait_for(client.recv(), timeout=3)

    assert error["code"] == "no_backend"
    assert error["task_id"] == "needs-ogg"
    assert closed.value.rcvd.code == 4000


@pytest.mark.asyncio
async def test_backend_health_503_is_unhealthy_and_not_selected():
    hits = {"healthy": [], "dead": []}

    def handler_for(name):
        async def handler(ws):
            message = json.loads(await ws.recv())
            hits[name].append(message["task_id"])
            await ws.send(make_recognition(message["task_id"]))

        return handler

    async with fake_backend(handler_for("healthy")) as (healthy_url, _):
        async with fake_backend(handler_for("dead"), health_status=503) as (dead_url, _):
            healthy = BackendState(id="healthy", url=healthy_url)
            dead = BackendState(id="dead", url=dead_url)
            proxy = ProxyServer("127.0.0.1", 0, [healthy, dead])
            async with proxy.serve() as server:
                assert dead.healthy is False
                proxy_url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
                async with websockets.connect(proxy_url, max_size=None) as client:
                    await client.send(make_audio("healthy-task", encoding="flac"))
                    await client.recv()

    assert hits == {"healthy": ["healthy-task"], "dead": []}
    assert dead.active_tasks == 0


@pytest.mark.asyncio
async def test_failed_http_health_probe_falls_back_to_live_v1_websocket(monkeypatch):
    import core.proxy.proxy_server as proxy_module

    async def idle_handler(ws):
        try:
            await ws.recv()
        except websockets.exceptions.ConnectionClosed:
            return

    def failed_http_probe(_url):
        raise OSError("health endpoint is not available")

    monkeypatch.setattr(proxy_module, "_read_backend_health", failed_http_probe)
    async with fake_backend(idle_handler, health_status=None) as (url, _):
        backend = BackendState(id="legacy", url=url)
        proxy = ProxyServer("127.0.0.1", 0, [backend])
        async with proxy.serve():
            assert backend.healthy is True
            assert backend.protocol_version == 1


@pytest.mark.asyncio
async def test_proxy_health_and_status_expose_v2_metadata_and_unavailability():
    async def unused_handler(ws):
        await ws.recv()

    async with fake_backend(
        unused_handler,
        health_payload={
            "protocol_version": 2,
            "encodings": ["f32le", "flac"],
            "model": "model-a",
            "git_sha": "backend-a",
        },
    ) as (url_a, state_a):
        async with fake_backend(
            unused_handler,
            health_payload={
                "protocol_version": 2,
                "encodings": ["flac", "ogg_opus"],
                "model": "model-b",
                "git_sha": "backend-b",
            },
        ) as (url_b, state_b):
            proxy = ProxyServer(
                "127.0.0.1",
                0,
                [BackendState(id="a", url=url_a), BackendState(id="b", url=url_b)],
            )
            async with proxy.serve() as server:
                port = server.sockets[0].getsockname()[1]
                status, _, body = await fetch(f"http://127.0.0.1:{port}/health")
                health = json.loads(body)
                status_json, _, status_body = await fetch(f"http://127.0.0.1:{port}/status")
                status_html, _, html_body = await fetch(
                    f"http://127.0.0.1:{port}/status", "text/html"
                )

                assert status == 200
                assert health["status"] == "ok"
                assert health["protocol_version"] == 2
                assert health["role"] == "proxy"
                assert health["encodings"] == ["f32le", "flac", "ogg_opus"]
                assert health["git_sha"] == proxy.git_sha
                assert {item["git_sha"] for item in health["backends"]} == {
                    "backend-a",
                    "backend-b",
                }
                assert status_json == 200
                status_payload = json.loads(status_body)
                assert [item["protocol_version"] for item in status_payload["backends"]] == [2, 2]
                assert [item["git_sha"] for item in status_payload["backends"]] == [
                    "backend-a",
                    "backend-b",
                ]
                assert status_html == 200
                assert b"Protocol" in html_body and b"Git SHA" in html_body
                assert html_body.count(b"<td>2</td>") == 2
                assert b"backend-a" in html_body and b"backend-b" in html_body

                state_a["status"] = 503
                state_b["status"] = 503
                await proxy._probe_backends()
                unavailable_status, _, unavailable_body = await fetch(
                    f"http://127.0.0.1:{port}/health"
                )

    assert unavailable_status == 503
    assert json.loads(unavailable_body)["status"] == "unavailable"


@pytest.mark.asyncio
async def test_bounded_upload_queue_backpressures_client_send():
    backend_send_started = asyncio.Event()
    release_backend = asyncio.Event()
    backend_closed = asyncio.Event()

    class BackendThatDoesNotRead:
        async def send(self, message):
            backend_send_started.set()
            await release_backend.wait()

        def __aiter__(self):
            return self

        async def __anext__(self):
            await backend_closed.wait()
            raise StopAsyncIteration

        async def close(self):
            backend_closed.set()

    backend_ws = BackendThatDoesNotRead()

    async def connect(_url):
        return backend_ws

    backend = BackendState(id="slow", url="ws://unused")
    router = TaskRouter([backend], connect_func=connect)
    client_ws = object()

    async def client_send(raw_message):
        await router.route_client_message(raw_message, client_ws)

    try:
        await client_send(make_audio("slow-task"))
        await asyncio.wait_for(backend_send_started.wait(), timeout=1)
        session = router.task_sessions["slow-task"]
        for _ in range(8):
            await client_send(make_audio("slow-task"))
        assert session.outbound_queue.qsize() == 8

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(client_send(make_audio("slow-task")), timeout=0.05)
        assert session.outbound_queue.qsize() <= 8
    finally:
        release_backend.set()
        await router.close_all()
    assert backend.active_tasks == 0


@pytest.mark.asyncio
async def test_backend_error_frame_is_forwarded_unchanged_and_releases_task():
    error_frame = json.dumps(
        {
            "type": "error",
            "task_id": "backend-error",
            "code": "decode_failed",
            "message": "decoder failed",
            "retryable": False,
        },
        separators=(",", ":"),
    )

    async def backend_handler(ws):
        await ws.recv()
        await ws.send(error_frame)
        await ws.close()

    async with fake_backend(backend_handler) as (url, _):
        backend = BackendState(id="v2", url=url)
        proxy = ProxyServer("127.0.0.1", 0, [backend])
        async with proxy.serve() as server:
            proxy_url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            async with websockets.connect(proxy_url, max_size=None) as client:
                await client.send(make_audio("backend-error", encoding="flac"))
                received = await asyncio.wait_for(client.recv(), timeout=3)
                for _ in range(100):
                    if backend.active_tasks == 0:
                        break
                    await asyncio.sleep(0.01)

    assert received == error_frame
    assert backend.active_tasks == 0
