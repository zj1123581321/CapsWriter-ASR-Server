# coding: utf-8
"""测试服务端共享状态和有界等待辅助方法。"""
from __future__ import annotations

import asyncio
import functools
import multiprocessing
import os
import queue
from pathlib import Path
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


class _FaultyPutQueue:
    """第 N 次真实入队就抛错的队列代理（只在注入故障时使用）。"""

    def __init__(self, queue, fail_at: int = 2):
        self._queue = queue
        self._fail_at = fail_at
        self._puts = 0

    def put(self, item, *args, **kwargs):
        if hasattr(item, "task_id"):
            self._puts += 1
            if self._puts >= self._fail_at:
                raise RuntimeError("injected background failure (enqueue)")
        return self._queue.put(item, *args, **kwargs)

    def get(self, *args, **kwargs):
        return self._queue.get(*args, **kwargs)


def _apply_fault(fault):
    """把未知后台异常注入 runner 的真实代码路径（真实数据流，非替身实现）。"""
    if fault == "pcm_chunks":
        from core.server.http_file_runner import FileSourceDecoder

        async def faulty_pcm_chunks(self):
            raise RuntimeError("injected background failure (pcm_chunks)")
            yield b""  # pragma: no cover - 只为保持 async generator 形态

        FileSourceDecoder.pcm_chunks = faulty_pcm_chunks
    elif fault is not None:
        raise ValueError(f"未知故障注入类型: {fault!r}")


def run_managed_http_server(
    info_queue, options, calls, received, queue_in, queue_out, data_dir,
    fault=None, ffmpeg_shim=None,
):
    """独立主进程：真 HTTP listener + 真文件 runner + 真 ws_send + 真识别子进程。

    SIGTERM/SIGINT 走与生产 app.stop() 同一收尾顺序并以 0 退出；
    runner 的未知后台异常上抛到 listener 监督链，进程非零退出。
    """
    import os
    import signal
    import sys
    import traceback

    if ffmpeg_shim is not None:
        os.environ["PATH"] = f"{ffmpeg_shim}{os.pathsep}{os.environ.get('PATH', '')}"
        os.environ["CW_TEST_ENV_MARKER"] = "runner-env-marker"

    from config_server import ServerConfig
    from core.server.http_file_runner import HttpFileRunner
    from core.server.http_server import HttpServer
    from tests.harness.worker import run_recording_worker

    ServerConfig.seg_cut_snap = False
    manager = multiprocessing.Manager()
    state = ServerState(queue_in=queue_in, queue_out=queue_out)
    state.sockets_id = manager.list()
    state.active_http_jobs = manager.list()
    _apply_fault(fault)
    if fault == "enqueue":
        state.queue_in = _FaultyPutQueue(queue_in)

    app = SimpleNamespace(state=state)
    worker = multiprocessing.Process(
        target=run_recording_worker,
        args=(queue_in, queue_out, state.sockets_id, state.active_http_jobs,
              options, calls, received),
        daemon=True,
    )
    worker.start()
    state.recognize_process = worker
    process_manager = ProcessManager(app)
    process_manager._process = worker
    process_manager.is_alive = True
    app.process_manager = process_manager
    process_manager._wait_for_models()

    http_server = HttpServer(app, "127.0.0.1", 0, data_dir)
    http_server.prepare()
    runner = HttpFileRunner(state, http_server)
    http_server.attach_runner(runner)
    state.http_result_sink = runner.result_sink

    async def shutdown():
        state.queue_out.put(None)
        process_manager.stop()
        await http_server.stop()

    async def serve():
        from core.tools.daemon_executor import SimpleDaemonExecutor

        loop = asyncio.get_running_loop()
        loop.set_default_executor(SimpleDaemonExecutor())
        stopping = asyncio.Event()
        for name in ("SIGTERM", "SIGINT"):
            signum = getattr(signal, name, None)
            if signum is not None:
                loop.add_signal_handler(signum, stopping.set)

        sender = asyncio.create_task(ws_send(app))
        monitor = asyncio.create_task(process_manager.monitor())
        listener = asyncio.create_task(http_server.serve())
        wait_stop = asyncio.ensure_future(stopping.wait())
        tasks = {sender, monitor, listener, wait_stop}
        while http_server._bound_port is None and not listener.done():
            await asyncio.sleep(0.01)
        if listener.done() and listener.exception() is not None:
            print(
                f"HTTP_LISTENER_FAILED={listener.exception()}",
                file=sys.stderr, flush=True,
            )
            raise listener.exception()
        info_queue.put((http_server._bound_port, worker.pid))

        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        error = None
        for task in done:
            if task is wait_stop or task.cancelled():
                continue
            exc = task.exception()
            if exc is not None and error is None:
                error = exc
        for task in tasks:
            task.cancel()
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout=15
        )
        if stopping.is_set():
            await shutdown()
        if error is not None:
            print(f"HTTP_SERVER_FAILURE={error!r}", file=sys.stderr, flush=True)
            traceback.print_exception(type(error), error, error.__traceback__)
            raise error

    asyncio.run(serve())


def _child_with_stderr(args, stderr_fd):
    """子进程先把 fd 2 与 sys.stderr 指到测试提供的日志文件，再进入真实服务主体。

    两处都要改：pytest 之类的捕获器可能已经把 sys.stderr 换成指向临时文件的对象，
    只 dup2 不足以让 rich/print/traceback 落到本文件。
    """
    import sys as _sys

    os.dup2(stderr_fd, 2)
    _sys.stderr = open(2, "w", buffering=1, errors="replace", closefd=False)
    run_managed_http_server(*args)


class ManagedHttpServerHarness:
    """跨进程 HTTP 文件任务服务端句柄（信号、崩溃与非零退出验收用）。"""

    def __init__(self):
        self.process = None
        self.stderr_path = None
        self._stderr_handle = None
        self.exitcode = None

    @classmethod
    async def start(cls, *, data_dir, options=None, fault=None, ffmpeg_shim=None,
                    stderr_path=None):
        self = cls()
        self.manager = multiprocessing.Manager()
        self.calls = self.manager.list()
        self.received = self.manager.list()
        self.info_queue = multiprocessing.Queue()
        self.queue_in = self.manager.Queue()
        self.queue_out = self.manager.Queue()
        self.stderr_path = stderr_path or (Path(data_dir).parent / "server-stderr.log")
        self.stderr_path.parent.mkdir(parents=True, exist_ok=True)
        self._stderr_handle = open(self.stderr_path, "wb")
        self.process = multiprocessing.Process(
            target=_child_with_stderr,
            args=(
                (
                    self.info_queue, options or {}, self.calls, self.received,
                    self.queue_in, self.queue_out, Path(data_dir), fault, ffmpeg_shim,
                ),
                self._stderr_handle.fileno(),
            ),
        )
        self.process.start()
        try:
            port, self.worker_pid = await asyncio.to_thread(self.info_queue.get, True, 30)
        except queue.Empty as exc:
            await self.cleanup()
            raise AssertionError(
                f"等待 HTTP 测试服务启动信息超时 (30s)：{self.stderr_tail()}"
            ) from exc
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        return self

    def read_db(self, sql: str, params: tuple = ()):
        import sqlite3

        conn = sqlite3.connect(f"file:{self.data_dir / 'http.sqlite3'}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    async def wait_for_exit(self, timeout: float = 20) -> int:
        await asyncio.to_thread(self.process.join, timeout)
        if self.process.is_alive():
            raise AssertionError(
                f"服务主进程在 {timeout}s 内未退出：{self.stderr_tail()}"
            )
        self.exitcode = self.process.exitcode
        return self.exitcode

    async def terminate(self, signum, timeout: float = 20) -> int:
        os.kill(self.process.pid, signum)
        return await self.wait_for_exit(timeout)

    async def stop(self, timeout: float = 20) -> int:
        if self.process.is_alive():
            import signal as signal_module

            return await self.terminate(signal_module.SIGTERM, timeout)
        return self.process.exitcode

    def stderr_tail(self, limit: int = 4000) -> str:
        if self.stderr_path is None or not self.stderr_path.exists():
            return ""
        return self.stderr_path.read_text(encoding="utf-8", errors="replace")[-limit:]

    async def cleanup(self) -> None:
        if self.process is not None and self.process.is_alive():
            self.process.kill()
            await asyncio.to_thread(self.process.join, 5)
        self.exitcode = self.process.exitcode if self.process is not None else None
        if self._stderr_handle is not None:
            self._stderr_handle.close()
            self._stderr_handle = None
        if self.info_queue is not None:
            self.info_queue.close()
        if self.manager is not None:
            self.manager.shutdown()
