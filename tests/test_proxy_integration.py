# coding: utf-8

import asyncio
import json
import urllib.request

import pytest

websockets = pytest.importorskip("websockets")

from core.proxy.backend import BackendState
from core.proxy.proxy_server import ProxyServer


def make_audio(task_id: str, is_final: bool = False) -> str:
    return json.dumps(
        {
            "task_id": task_id,
            "source": "file",
            "data": "",
            "is_final": is_final,
            "time_start": 1.0,
        }
    )


def make_recognition(task_id: str, is_final: bool = True) -> str:
    return json.dumps(
        {
            "task_id": task_id,
            "is_final": is_final,
            "duration": 1.0,
            "time_start": 1.0,
            "time_submit": 2.0,
            "time_complete": 3.0,
            "text": f"done:{task_id}",
        }
    )


async def fetch_status(proxy_port: int, query: str = ""):
    def _fetch():
        with urllib.request.urlopen(f"http://127.0.0.1:{proxy_port}/status{query}", timeout=5) as response:
            return response.status, response.headers, response.read()

    return await asyncio.to_thread(_fetch)


@pytest.mark.asyncio
async def test_proxy_routes_different_tasks_to_least_loaded_backends():
    backend_hits = {"a": [], "b": []}
    release_backend_a = asyncio.Event()

    async def backend_a_handler(ws):
        raw = await ws.recv()
        data = json.loads(raw)
        backend_hits["a"].append(data["task_id"])
        await release_backend_a.wait()
        await ws.send(make_recognition(data["task_id"]))

    async def backend_b_handler(ws):
        raw = await ws.recv()
        data = json.loads(raw)
        backend_hits["b"].append(data["task_id"])
        await ws.send(make_recognition(data["task_id"]))

    async with websockets.serve(backend_a_handler, "127.0.0.1", 0, max_size=None) as server_a:
        async with websockets.serve(backend_b_handler, "127.0.0.1", 0, max_size=None) as server_b:
            port_a = server_a.sockets[0].getsockname()[1]
            port_b = server_b.sockets[0].getsockname()[1]
            proxy = ProxyServer(
                "127.0.0.1",
                0,
                [
                    BackendState(id="backend-0", url=f"ws://127.0.0.1:{port_a}"),
                    BackendState(id="backend-1", url=f"ws://127.0.0.1:{port_b}"),
                ],
            )
            async with proxy.serve() as proxy_ws_server:
                proxy_port = proxy_ws_server.sockets[0].getsockname()[1]
                async with websockets.connect(f"ws://127.0.0.1:{proxy_port}", max_size=None) as client:
                    await client.send(make_audio("task-a"))
                    await asyncio.sleep(0.05)
                    await client.send(make_audio("task-b"))
                    result_b = json.loads(await client.recv())
                    release_backend_a.set()
                    result_a = json.loads(await client.recv())

    assert backend_hits == {"a": ["task-a"], "b": ["task-b"]}
    assert {result_a["task_id"], result_b["task_id"]} == {"task-a", "task-b"}


@pytest.mark.asyncio
async def test_status_endpoint_returns_backend_info():
    proxy = ProxyServer(
        "127.0.0.1",
        0,
        [
            BackendState(id="backend-0", url="ws://127.0.0.1:6017", active_tasks=2),
            BackendState(
                id="backend-1",
                url="ws://127.0.0.1:6018",
                healthy=False,
                avg_latency=1.23,
                latency_samples=4,
                weight=2.0,
                consecutive_failures=3,
                last_failure_time=123.0,
            ),
        ],
    )
    async def skip_health_probe():
        return None

    proxy._probe_backends = skip_health_probe

    async with proxy.serve() as proxy_ws_server:
        proxy_port = proxy_ws_server.sockets[0].getsockname()[1]
        status, headers, body = await fetch_status(proxy_port)

    payload = json.loads(body)

    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    assert payload["active_tasks_total"] == 2
    assert payload["task_history"]["total"] == 0
    assert len(payload["backends"]) == 2
    assert payload["backends"][0] == {
        "id": "backend-0",
        "url": "ws://127.0.0.1:6017",
        "healthy": True,
        "protocol_version": 1,
        "git_sha": None,
        "active_tasks": 2,
        "avg_latency": 0.0,
        "latency_samples": 0,
        "weight": 1.0,
        "consecutive_failures": 0,
        "last_failure_time": 0.0,
    }
    assert payload["backends"][1] == {
        "id": "backend-1",
        "url": "ws://127.0.0.1:6018",
        "healthy": False,
        "protocol_version": 1,
        "git_sha": None,
        "active_tasks": 0,
        "avg_latency": 1.23,
        "latency_samples": 4,
        "weight": 2.0,
        "consecutive_failures": 3,
        "last_failure_time": 123.0,
    }


@pytest.mark.asyncio
async def test_task_completion_recorded_in_history():
    async def backend_handler(ws):
        raw = await ws.recv()
        data = json.loads(raw)
        await ws.send(make_recognition(data["task_id"]))

    async with websockets.serve(backend_handler, "127.0.0.1", 0, max_size=None) as backend_server:
        backend_port = backend_server.sockets[0].getsockname()[1]
        proxy = ProxyServer(
            "127.0.0.1",
            0,
            [BackendState(id="backend-0", url=f"ws://127.0.0.1:{backend_port}")],
        )
        async with proxy.serve() as proxy_ws_server:
            proxy_port = proxy_ws_server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{proxy_port}", max_size=None) as client:
                await client.send(make_audio("task-history"))
                result = json.loads(await client.recv())

            _, _, body = await fetch_status(proxy_port)

    payload = json.loads(body)

    assert result["task_id"] == "task-history"
    assert payload["task_history"]["total"] >= 1
    assert payload["task_history"]["completed"] >= 1
    assert payload["task_history"]["recent"][-1]["task_id"] == "task-history"
    assert payload["task_history"]["recent"][-1]["backend_id"] == "backend-0"
    assert payload["task_history"]["recent"][-1]["status"] == "completed"


@pytest.mark.asyncio
async def test_client_disconnect_records_failed_task_history():
    backend_started = asyncio.Event()
    release_backend = asyncio.Event()

    async def backend_handler(ws):
        await ws.recv()
        backend_started.set()
        await release_backend.wait()

    async with websockets.serve(backend_handler, "127.0.0.1", 0, max_size=None) as backend_server:
        backend_port = backend_server.sockets[0].getsockname()[1]
        proxy = ProxyServer(
            "127.0.0.1",
            0,
            [BackendState(id="backend-0", url=f"ws://127.0.0.1:{backend_port}")],
        )
        async with proxy.serve() as proxy_ws_server:
            proxy_port = proxy_ws_server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{proxy_port}", max_size=None) as client:
                await client.send(make_audio("task-disconnect"))
                await asyncio.wait_for(backend_started.wait(), timeout=5)

            _, _, body = await fetch_status(proxy_port)
            release_backend.set()

    payload = json.loads(body)

    assert payload["task_history"]["failed"] >= 1
    assert payload["task_history"]["recent"][-1]["task_id"] == "task-disconnect"
    assert payload["task_history"]["recent"][-1]["status"] == "failed"


@pytest.mark.asyncio
async def test_health_probe_recognizes_legacy_backend_http_426():
    """An old server's ordinary HTTP 426 response marks it as a healthy v1 backend."""

    async def backend_handler(ws):
        await ws.recv()

    backends = [
        BackendState(id="good", url="ws://127.0.0.1:1"),
        BackendState(id="down", url="ws://127.0.0.1:2", healthy=False),
    ]
    backends[1].consecutive_failures = 3
    proxy = ProxyServer("127.0.0.1", 0, backends, probe_interval=0.1)

    assert backends[1].healthy is False

    async with websockets.serve(backend_handler, "127.0.0.1", 0, max_size=None) as legacy_server:
        port = legacy_server.sockets[0].getsockname()[1]
        backends[1].url = f"ws://127.0.0.1:{port}"

        async with proxy.serve():
            await asyncio.sleep(0.3)

    assert backends[1].healthy is True
    assert backends[1].protocol_version == 1
    assert backends[1].consecutive_failures == 0
