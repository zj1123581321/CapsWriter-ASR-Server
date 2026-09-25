# coding: utf-8
"""测试服务端共享状态和有界等待辅助方法。"""
from __future__ import annotations

import asyncio
import functools
import multiprocessing
import queue
from types import SimpleNamespace

import websockets

from core.server.connection.ws_recv import ws_recv
from core.server.connection.ws_send import ws_send
from core.server.connection.health import process_request as health_process_request
from core.server.state import ServerState
from core.server.worker.process_manager import ProcessManager

from tests.harness.fake_engine import IDENTITY_MARKER
from tests.harness.worker import run_health_fake_worker


class ObservedTaskQueue:
    """把实际 socket_id 附到任务上下文，同时保留 multiprocessing.Queue 边界。"""

    def __init__(self, queue, observed):
        self.queue = queue
        self.observed = observed

    def put(self, task, *args, **kwargs):
        if task is not None and hasattr(task, "task_id"):
            self.observed.append({"task_id": task.task_id, "socket_id": task.socket_id})
            task.context = f"{task.context}{IDENTITY_MARKER}{task.task_id}:{task.socket_id}"
        return self.queue.put(task, *args, **kwargs)

    def get(self, *args, **kwargs):
        return self.queue.get(*args, **kwargs)


class FakeServerHarness:
    def __init__(self, server, process, calls, observed):
        self.server = server
        self.process = process
        self.calls = calls
        self.observed = observed
        self.port = server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{self.port}"

    async def wait_for_calls(self, count: int, timeout: float = 5.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while len(self.calls) < count:
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError(
                    f"等待假引擎调用 {count} 次超时 ({timeout}s)，当前记录: {list(self.calls)!r}"
                )
            if not self.process.is_alive():
                raise AssertionError(
                    f"识别子进程提前退出，exitcode={self.process.exitcode}，调用记录: {list(self.calls)!r}"
                )
            await asyncio.sleep(0.01)


def run_managed_fake_server(
    info_queue, options, calls, observed, queue_in, queue_out, stall_first_send,
    monitor_interval,
):
    """在独立主进程中运行真 websocket、真 worker 和真实存活监控。"""
    from config_server import ServerConfig
    if monitor_interval is not None:
        from core.server.worker import process_manager as process_manager_module
        process_manager_module.PROCESS_MONITOR_INTERVAL_SECONDS = monitor_interval

    ServerConfig.seg_cut_snap = False
    manager = multiprocessing.Manager()
    state = ServerState(queue_in=queue_in, queue_out=queue_out)
    state.sockets_id = manager.list()
    state.queue_in = ObservedTaskQueue(queue_in, observed)
    state.queue_out = queue_out
    if stall_first_send:
        from websockets.asyncio.server import ServerConnection
        original_send = ServerConnection.send
        stalled = set()

        async def stall_first(websocket, message, *args, **kwargs):
            if not stalled:
                stalled.add(websocket.id)
                await asyncio.wait_for(asyncio.Future(), timeout=15)
            await original_send(websocket, message, *args, **kwargs)

        ServerConnection.send = stall_first
    app = SimpleNamespace(state=state)
    worker = multiprocessing.Process(
        target=run_health_fake_worker,
        args=(queue_in, queue_out, state.sockets_id, options, calls),
        daemon=True,
    )
    worker.start()
    state.recognize_process = worker
    process_manager = ProcessManager(app)
    process_manager._process = worker
    process_manager.is_alive = True
    app.process_manager = process_manager
    process_manager._wait_for_models()

    async def serve():
        from core.tools.daemon_executor import SimpleDaemonExecutor
        asyncio.get_running_loop().set_default_executor(SimpleDaemonExecutor())
        server = await websockets.serve(
            functools.partial(ws_recv, app=app),
            "127.0.0.1",
            0,
            max_size=None,
            ping_interval=None,
            process_request=functools.partial(health_process_request, app=app),
        )
        info_queue.put((server.sockets[0].getsockname()[1], worker.pid))
        sender = asyncio.create_task(ws_send(app))
        monitor = asyncio.create_task(process_manager.monitor())
        tasks = {sender, monitor}
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
            server.close()
            await asyncio.wait_for(server.wait_closed(), timeout=5)

    asyncio.run(serve())


def _raise_before_engine_setup(queue_out):
    raise RuntimeError("fake model load failure before set_engine")


def run_model_load_failure():
    """以 ProcessManager 的模型就绪监控验证主进程非零退出。"""
    state = SimpleNamespace(queue_out=multiprocessing.Queue())
    app = SimpleNamespace(state=state)
    manager = ProcessManager(app)
    manager.is_alive = True
    manager._process = multiprocessing.Process(target=_raise_before_engine_setup, args=(state.queue_out,))
    manager._process.start()
    manager._wait_for_models()


class ManagedFakeServerHarness:
    """跨进程监控验收用服务端句柄。"""
    @classmethod
    async def start(
        cls, options=None, *, stall_first_send=False, monitor_interval=None
    ):
        self = cls()
        self.manager = multiprocessing.Manager()
        self.calls = self.manager.list()
        self.observed = self.manager.list()
        self.info_queue = multiprocessing.Queue()
        self.queue_in = self.manager.Queue()
        self.queue_out = self.manager.Queue()
        self.process = multiprocessing.Process(
            target=run_managed_fake_server,
            args=(
                self.info_queue,
                options or {},
                self.calls,
                self.observed,
                self.queue_in,
                self.queue_out,
                stall_first_send,
                monitor_interval,
            ),
        )
        self.process.start()
        try:
            port, self.worker_pid = await asyncio.to_thread(self.info_queue.get, True, 20)
        except queue.Empty as exc:
            if self.process.is_alive():
                self.process.terminate()
            await asyncio.to_thread(self.process.join, 5)
            assert not self.process.is_alive(), "启动超时后服务主进程在 5 秒内未退出"
            self.info_queue.close()
            self.manager.shutdown()
            raise AssertionError("等待测试服务启动信息超时 (20s)") from exc
        self.url = f"ws://127.0.0.1:{port}"
        return self

    async def wait_for_calls(self, count, timeout=5):
        deadline = asyncio.get_running_loop().time() + timeout
        while len(self.calls) < count:
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError(f"等待假引擎 {count} 次调用超时: {list(self.calls)!r}")
            if not self.process.is_alive():
                raise AssertionError(f"服务主进程提前退出，exitcode={self.process.exitcode}")
            await asyncio.sleep(0.01)

    async def stop(self):
        if self.process.is_alive():
            self.queue_in.put(None)
            self.queue_out.put(None)
            await asyncio.to_thread(self.process.join, 5)
        graceful_timeout = self.process.is_alive()
        if graceful_timeout:
            self.process.terminate()
        await asyncio.to_thread(self.process.join, 5)
        assert not self.process.is_alive(), "服务主进程在 teardown 的 5 秒 join 后仍存活"
        self.info_queue.close()
        self.manager.shutdown()
        assert not graceful_timeout, "服务主进程未在 5 秒内响应 worker 停止信号"
