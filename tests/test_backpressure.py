# coding: utf-8
"""T4 服务端背压、任务配额与上传空闲回收验收。"""
from __future__ import annotations

import asyncio
import base64
import functools
import json
import multiprocessing
import queue
import socket
import time
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
import websockets

from config_server import ServerConfig
from core.server.schema import Task
from core.server.state import ServerState, WorkerState
from core.server.worker.task_handler import TaskHandler
from core.server.connection.ws_recv import ws_recv
from core.server.connection.ws_send import ws_send
from tests.harness.fake_engine import ProgrammableFakeEngine


class BackpressureFakeEngine(ProgrammableFakeEngine):
    def __init__(self, *, calls, release_event=None, delay_seconds=0.0):
        super().__init__(calls=calls)
        self.release_event = release_event
        self.delay_seconds = delay_seconds

    def decode_stream(self, stream, context=None, **kwargs):
        result = super().decode_stream(stream, context=context, **kwargs)
        if self.call_count == 1 and self.release_event is not None:
            self.release_event.wait(60)
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        return result


def run_backpressure_worker(queue_in, queue_out, sockets_id, calls, release_event, delay_seconds):
    handler = TaskHandler(queue_in, queue_out, sockets_id, WorkerState())
    handler.set_engine(BackpressureFakeEngine(
        calls=calls, release_event=release_event, delay_seconds=delay_seconds
    ))
    queue_out.put(True)
    handler.loop()


class LocalServer:
    @classmethod
    async def start(cls, *, release_event=None, delay_seconds=0.0):
        self = cls()
        self.manager = multiprocessing.Manager()
        self.calls = self.manager.list()
        self.queue_in = multiprocessing.Queue()
        self.queue_out = multiprocessing.Queue()
        self.state = ServerState(queue_in=self.queue_in, queue_out=self.queue_out)
        self.state.sockets_id = self.manager.list()
        self.app = SimpleNamespace(state=self.state)
        self.process = multiprocessing.Process(
            target=run_backpressure_worker,
            args=(
                self.queue_in,
                self.queue_out,
                self.state.sockets_id,
                self.calls,
                release_event,
                delay_seconds,
            ),
            daemon=True,
        )
        self.process.start()
        ready = await asyncio.to_thread(self.queue_out.get, True, 10)
        assert ready is True
        self.server = await websockets.serve(
            functools.partial(ws_recv, app=self.app),
            "127.0.0.1",
            0,
            max_size=None,
            max_queue=1,
            ping_interval=None,
        )
        self.sender = asyncio.create_task(ws_send(self.app))
        self.url = f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        return self

    async def stop(self):
        self.server.close()
        await asyncio.wait_for(self.server.wait_closed(), 5)
        if self.process.is_alive():
            self.queue_in.put(None)
            await asyncio.to_thread(self.process.join, 5)
        assert not self.process.is_alive(), "测试 Worker 未在 5 秒内退出"
        self.queue_out.put(None)
        await asyncio.wait_for(self.sender, 5)
        self.queue_in.close()
        self.queue_out.close()
        self.manager.shutdown()


@pytest.fixture
def server_factory():
    return LocalServer.start


def audio_frame(task_id, *, data, is_final, seg_duration=15.0):
    return json.dumps({
        "task_id": task_id,
        "source": "mic",
        "data": data,
        "is_final": is_final,
        "time_start": 0.0,
        "seg_duration": seg_duration,
        "seg_overlap": 0.0,
    })


async def collect_results(websocket, expected):
    results = []
    while len(results) < expected:
        message = json.loads(await asyncio.wait_for(websocket.recv(), 40))
        assert message["type"] == "result", message
        results.append(message)
        if message["is_final"]:
            break
    return results


async def connect_with_small_write_buffer(url):
    parsed = urlparse(url)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 64 * 1024)
    sock.setblocking(False)
    await asyncio.get_running_loop().sock_connect(sock, (parsed.hostname, parsed.port))
    return await websockets.connect(
        url,
        sock=sock,
        proxy=None,
        max_size=None,
        ping_interval=None,
        write_limit=1024,
        compression=None,
    )


def active_task_count(state):
    return sum(record.status not in {"DONE", "FAILED"} for record in state.tasks.values())


@pytest.mark.asyncio
async def test_inflight_limit_stops_reads_and_returns_all_results(monkeypatch, server_factory):
    monkeypatch.setattr(ServerConfig, "seg_cut_snap", False)
    monkeypatch.setattr(ServerConfig, "max_inflight_segments", 2)
    monkeypatch.setattr(ServerConfig, "max_tasks", 8)
    release = multiprocessing.Event()
    server = await server_factory(release_event=release, delay_seconds=1)
    sending = None
    receiver = None
    try:
        task_id = "twenty-segments"
        data = base64.b64encode(bytes(15 * 16000 * 4)).decode("ascii")
        websocket = await connect_with_small_write_buffer(server.url)
        async with websocket:
            receiver = asyncio.create_task(collect_results(websocket, 20))

            async def upload():
                for index in range(20):
                    await websocket.send(audio_frame(
                            task_id,
                            data=data,
                            is_final=index == 19,
                            seg_duration=15.0,
                    ))

            sending = asyncio.create_task(upload())
            deadline = asyncio.get_running_loop().time() + 5
            while len(server.calls) < 1 or sum(
                len(pending)
                for (socket_id, pending_task_id), pending in server.state.pending_segments.items()
                if pending_task_id == task_id
            ) < 2:
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.01)

            await asyncio.sleep(0.2)
            assert not sending.done(), f"背压生效前发送协程应挂起；实际 done={sending.done()}"

            in_flight_samples = []
            for _ in range(20):
                in_flight_samples.append(sum(
                    len(pending) for pending in server.state.pending_segments.values()
                ))
                await asyncio.sleep(0.02)
            assert max(in_flight_samples) <= 2, in_flight_samples

            release.set()
            await asyncio.wait_for(sending, timeout=40)
            results = await asyncio.wait_for(receiver, timeout=45)
            assert len(results) == 20
            assert results[-1]["is_final"] is True
    finally:
        release.set()
        for task in (sending, receiver):
            if task is not None and not task.done():
                task.cancel()
        pending_tasks = [task for task in (sending, receiver) if task is not None]
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        await server.stop()


@pytest.mark.asyncio
async def test_global_task_limit_reopens_after_completion(monkeypatch, server_factory):
    monkeypatch.setattr(ServerConfig, "seg_cut_snap", False)
    monkeypatch.setattr(ServerConfig, "max_tasks", 2)
    monkeypatch.setattr(ServerConfig, "max_inflight_segments", 4)
    server = await server_factory()
    try:
        first = await websockets.connect(server.url, max_size=None, ping_interval=None)
        second = await websockets.connect(server.url, max_size=None, ping_interval=None)
        third = await websockets.connect(server.url, max_size=None, ping_interval=None)
        blank = base64.b64encode(b"").decode("ascii")
        for socket, task_id in ((first, "one"), (second, "two")):
            await socket.send(audio_frame(task_id, data=blank, is_final=False, seg_duration=5))
        await third.send(audio_frame("three", data=blank, is_final=False, seg_duration=5))
        overloaded = json.loads(await asyncio.wait_for(third.recv(), 5))
        assert overloaded["code"] == "overloaded"
        assert overloaded["retryable"] is True
        with pytest.raises(websockets.ConnectionClosed):
            await asyncio.wait_for(third.recv(), 5)

        final_data = base64.b64encode(bytes(16000 * 4)).decode("ascii")
        await first.send(audio_frame("one", data=final_data, is_final=True, seg_duration=5))
        first_result = json.loads(await asyncio.wait_for(first.recv(), 5))
        assert first_result["is_final"] is True

        fourth = await websockets.connect(server.url, max_size=None, ping_interval=None)
        await fourth.send(audio_frame("four", data=blank, is_final=False, seg_duration=5))
        await fourth.send(audio_frame("four", data=final_data, is_final=True, seg_duration=5))
        fourth_result = json.loads(await asyncio.wait_for(fourth.recv(), 5))
        assert fourth_result["is_final"] is True

        await second.send(audio_frame("two", data=final_data, is_final=True, seg_duration=5))
        second_result = json.loads(await asyncio.wait_for(second.recv(), 5))
        assert second_result["is_final"] is True
        await asyncio.gather(first.close(), second.close(), fourth.close())
    finally:
        await server.stop()


def test_drain_queue_caps_each_round_at_sixteen(monkeypatch):
    from core.server.worker import task_handler

    monkeypatch.setattr(task_handler.Config, "drain_batch", 16)
    incoming = queue.Queue()
    outgoing = queue.Queue()
    sockets = ["socket"]
    for index in range(50):
        incoming.put(Task(
            type="file", data=b"", offset=0, overlap=0,
            task_id=f"task-{index}", socket_id="socket", is_final=False,
            time_start=0, time_submit=0,
        ))
    handler = TaskHandler(incoming, outgoing, sockets, WorkerState())
    assert handler.drain_queue() is True
    assert sum(map(len, handler.buffer._buffers.values())) == 16
    assert incoming.qsize() == 34


@pytest.mark.asyncio
async def test_upload_idle_returns_bad_request(monkeypatch, server_factory):
    monkeypatch.setattr(ServerConfig, "seg_cut_snap", False)
    monkeypatch.setattr(ServerConfig, "upload_idle_seconds", 2)
    server = await server_factory()
    try:
        async with websockets.connect(server.url, max_size=None, ping_interval=None) as websocket:
            await websocket.send(audio_frame("idle", data="", is_final=False, seg_duration=5))
            error = json.loads(await asyncio.wait_for(websocket.recv(), 4))
            assert error["code"] == "bad_request"
            assert "upload idle" in error["message"]
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_backpressure_pauses_upload_idle_timer(monkeypatch, server_factory):
    monkeypatch.setattr(ServerConfig, "seg_cut_snap", False)
    monkeypatch.setattr(ServerConfig, "max_inflight_segments", 1)
    monkeypatch.setattr(ServerConfig, "upload_idle_seconds", 2)
    release = multiprocessing.Event()
    server = await server_factory(release_event=release)
    try:
        async with websockets.connect(server.url, max_size=None, ping_interval=None) as websocket:
            data = base64.b64encode(bytes(5 * 16000 * 4)).decode("ascii")
            await websocket.send(audio_frame("paused-idle", data=data, is_final=False, seg_duration=5))
            deadline = asyncio.get_running_loop().time() + 5
            while not server.calls:
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.01)
            await websocket.send(audio_frame("paused-idle", data=data, is_final=False, seg_duration=5))
            record = next(iter(server.state.tasks.values()))
            deadline = asyncio.get_running_loop().time() + 5
            while not record.segment_slots._waiters:
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.01)

            await asyncio.sleep(2.5)
            assert record.status == "RECEIVING"
            assert server.process.is_alive()

            release.set()
            await asyncio.sleep(0.05)
            await websocket.send(audio_frame("paused-idle", data=data, is_final=True, seg_duration=5))
            results = await collect_results(websocket, 3)
            assert len(results) == 3
            assert results[-1]["is_final"] is True
    finally:
        release.set()
        await server.stop()


@pytest.mark.asyncio
async def test_disconnect_while_waiting_for_slot_cleans_task(monkeypatch, server_factory):
    monkeypatch.setattr(ServerConfig, "seg_cut_snap", False)
    monkeypatch.setattr(ServerConfig, "max_inflight_segments", 1)
    release = multiprocessing.Event()
    server = await server_factory(release_event=release)
    try:
        websocket = await websockets.connect(server.url, max_size=None, ping_interval=None)
        data = base64.b64encode(bytes(5 * 16000 * 4)).decode("ascii")
        await websocket.send(audio_frame("disconnect", data=data, is_final=False, seg_duration=5))
        deadline = asyncio.get_running_loop().time() + 5
        while not server.calls:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)
        await websocket.send(audio_frame("disconnect", data=data, is_final=False, seg_duration=5))
        record = next(iter(server.state.tasks.values()))
        deadline = asyncio.get_running_loop().time() + 5
        while not record.segment_slots._waiters:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)
        await websocket.close()

        deadline = asyncio.get_running_loop().time() + 3
        while active_task_count(server.state):
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)
        assert active_task_count(server.state) == 0
    finally:
        release.set()
        await server.stop()
