# coding: utf-8
"""测试服务端共享状态和有界等待辅助方法。"""
from __future__ import annotations

import asyncio

from tests.harness.fake_engine import IDENTITY_MARKER


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
