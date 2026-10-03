# coding: utf-8
"""E1 owner_kind 与真实 multiprocessing 队列边界验收。"""

from __future__ import annotations

import asyncio
import multiprocessing
import queue
from collections import deque
from types import SimpleNamespace

import pytest

from core.server.schema import Result, Task
from core.server.state import (
    ServerState,
    TaskLifecycle,
    WorkerState,
    begin_task,
    make_task_key,
    register_http_job,
    release_terminal_task,
    transition_terminal,
)
from core.server.connection.ws_send import ws_send
from core.server.worker.task_handler import TaskHandler
from tests.harness.fake_engine import ProgrammableFakeEngine


class TimestampFakeEngine(ProgrammableFakeEngine):
    """给最终 file 任务补最小真实时间戳，仍走 TaskPipeline。"""

    def decode_stream(self, stream, context=None, **kwargs):
        result = super().decode_stream(stream, context=context, **kwargs)
        result.tokens = ["x"]
        result.timestamps = [0.0]
        return result


class ObservingQueue:
    """在子进程消费真实 Queue payload，不另造 producer dict。"""

    def __init__(self, queue_in, observed):
        self.queue_in = queue_in
        self.observed = observed

    def get(self, *args, **kwargs):
        task = self.queue_in.get(*args, **kwargs)
        if task is not None:
            self.observed.put({
                "data": task.data,
                "offset": task.offset,
                "owner_kind": task.owner_kind,
                "socket_id": task.socket_id,
                "task_id": task.task_id,
                "is_final": task.is_final,
            })
        return task


def run_owner_worker(queue_in, queue_out, sockets_id, active_http_jobs, observed):
    handler = TaskHandler(
        ObservingQueue(queue_in, observed),
        queue_out,
        sockets_id,
        WorkerState(),
        active_http_jobs,
    )
    handler.set_engine(TimestampFakeEngine())
    queue_out.put(True)
    handler.loop()


def make_http_task(data: bytes, *, task_id: str = "job-1") -> Task:
    return Task(
        type="file",
        data=data,
        offset=1.25,
        overlap=0.5,
        task_id=task_id,
        socket_id="",
        is_final=True,
        time_start=2.0,
        time_submit=3.0,
        owner_kind="http",
    )


def test_http_task_has_explicit_owner_kind():
    task = make_http_task(b"pcm")

    assert task.owner_kind == "http"
    assert task.socket_id == ""
    assert make_task_key(task.owner_kind, task.task_id, task.socket_id) == (
        "http",
        "job-1",
        "job-1",
    )


def test_legacy_task_and_result_default_to_ws():
    task = Task(
        type="mic",
        data=b"pcm",
        offset=0.0,
        overlap=0.0,
        task_id="legacy-task",
        socket_id="socket-1",
        is_final=False,
        time_start=0.0,
        time_submit=0.0,
    )
    result = Result(
        task_id="legacy-task",
        socket_id="socket-1",
        type="mic",
    )

    assert task.owner_kind == "ws"
    assert result.owner_kind == "ws"
    assert make_task_key(task.owner_kind, task.task_id, task.socket_id) == (
        "ws",
        "socket-1",
        "legacy-task",
    )


def test_http_registration_precedes_task_lifecycle():
    state = ServerState(
        queue_in=queue.Queue(),
        queue_out=queue.Queue(),
        active_http_jobs=[],
    )
    register_http_job(state, "job-1")
    key = make_task_key("http", "job-1")
    begin_task(state, key)

    assert state.active_http_jobs == ["job-1"]
    assert key in state.tasks
    assert state.connection_tasks == {}


def test_unknown_owner_kind_fails_fast():
    with pytest.raises(ValueError, match="未知任务归属类型"):
        make_task_key("ftp", "job-1")


@pytest.mark.parametrize("active", [True, False])
def test_http_owner_gate_crosses_real_queue(active):
    manager = multiprocessing.Manager()
    incoming = multiprocessing.Queue()
    outgoing = multiprocessing.Queue()
    observed = multiprocessing.Queue()
    sockets_id = manager.list()
    active_http_jobs = manager.list(["job-1"] if active else [])
    process = multiprocessing.Process(
        target=run_owner_worker,
        args=(incoming, outgoing, sockets_id, active_http_jobs, observed),
        daemon=True,
    )
    payload = b"\x00\x00\x80?" * 16
    try:
        process.start()
        assert outgoing.get(timeout=5) is True
        incoming.put(make_http_task(payload))
        if active:
            actual = observed.get(timeout=5)
            assert actual == {
                "data": payload,
                "offset": 1.25,
                "owner_kind": "http",
                "socket_id": "",
                "task_id": "job-1",
                "is_final": True,
            }
            result = outgoing.get(timeout=5)
            assert result.owner_kind == "http"
            assert result.socket_id == ""
            assert result.task_id == "job-1"
            assert result.is_final is True
        else:
            actual = observed.get(timeout=5)
            assert actual["owner_kind"] == "http"
            with pytest.raises(queue.Empty):
                outgoing.get(timeout=0.5)
        incoming.put(None)
        process.join(5)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        incoming.close()
        outgoing.close()
        observed.close()
        manager.shutdown()


@pytest.mark.asyncio
async def test_parent_dispatcher_injects_http_sink_without_fake_socket():
    state = ServerState(queue_out=queue.Queue())
    state.active_http_jobs = ["job-1"]
    key = make_task_key("http", "job-1")
    state.tasks[key] = TaskLifecycle(segment_slots=asyncio.Semaphore(1))
    state.pending_segments[key] = deque([0.0])
    received = []

    async def sink(result):
        received.append(result)
        # 镜像真实 HttpResultSink 的终态责任：落库由存储层完成、这里由替身
        # 直接补内存终态（落库→转换→释放一体）。ws_send 的 HTTP 分支不再
        # 做任何转换——终态唯一收尾人是 sink。
        transition_terminal(state, key, "DONE")
        release_terminal_task(state, key)

    state.http_result_sink = sink
    state.queue_out.put(Result(
        task_id="job-1",
        socket_id="",
        type="file",
        owner_kind="http",
        is_final=True,
        text="done",
    ))
    state.queue_out.put(None)

    await ws_send(SimpleNamespace(state=state))

    assert len(received) == 1
    assert received[0].owner_kind == "http"
    assert received[0].socket_id == ""
    assert state.active_http_jobs == []
    # sink 收尾后运行态记录已按终态释放（与生产断言 state.tasks == {} 同口径）；
    # 终态事实由 transition_terminal 完成、release_terminal_task 弹出记录
    assert key not in state.tasks
