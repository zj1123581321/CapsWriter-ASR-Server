# coding: utf-8
"""测试识别子进程入口：构造真实 TaskHandler 并注入假引擎。"""
from core.server.state import WorkerState
from core.server.worker.task_handler import TaskHandler
from tests.harness.fake_engine import ProgrammableFakeEngine


def run_fake_worker(queue_in, queue_out, sockets_id, options, calls):
    handler = TaskHandler(queue_in, queue_out, sockets_id, WorkerState())
    handler.set_engine(ProgrammableFakeEngine(calls=calls, **options))
    queue_out.put(True)
    handler.loop()


def run_health_fake_worker(queue_in, queue_out, sockets_id, options, calls):
    """提供与生产 worker 相同的跨进程模型就绪负载。"""
    handler = TaskHandler(queue_in, queue_out, sockets_id, WorkerState())
    handler.set_engine(ProgrammableFakeEngine(calls=calls, **options))
    queue_out.put({"loaded": True, "aligner": "loaded"})
    handler.loop()
