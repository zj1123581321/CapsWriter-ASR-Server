# coding: utf-8
"""测试识别子进程入口：构造真实 TaskHandler 并注入假引擎。"""
import hashlib

from core.server.schema import Result
from core.server.state import WorkerState
from core.server.worker.task_handler import TaskHandler
from tests.harness.fake_engine import ProgrammableFakeEngine


def _configure_fake_pipeline(options):
    options = dict(options)
    if options.pop("break_final_sync", False):
        from core.server.worker import pipeline as pipeline_module

        pipeline_module.sync_tokens_from_text = lambda tokens, timestamps, text: ([], [])
    return options


def run_fake_worker(queue_in, queue_out, sockets_id, options, calls):
    handler = TaskHandler(queue_in, queue_out, sockets_id, WorkerState())
    handler.set_engine(
        ProgrammableFakeEngine(
            calls=calls, **_configure_fake_pipeline(options)
        )
    )
    queue_out.put(True)
    handler.loop()


def run_health_fake_worker(queue_in, queue_out, sockets_id, options, calls):
    """提供与生产 worker 相同的跨进程模型就绪负载。"""
    handler = TaskHandler(queue_in, queue_out, sockets_id, WorkerState())
    handler.set_engine(
        ProgrammableFakeEngine(
            calls=calls, **_configure_fake_pipeline(options)
        )
    )
    queue_out.put({"loaded": True, "aligner": "loaded"})
    handler.loop()


def received_task_record(task) -> dict:
    """识别子进程**真实收到**的 Task 摘要（跨 multiprocessing 序列化之后）。

    字段全部取自收到的对象本身，不接受调用方手造 dict 顶替。
    """
    return {
        "type": task.type,
        "owner_kind": task.owner_kind,
        "task_id": task.task_id,
        "socket_id": task.socket_id,
        "offset": task.offset,
        "overlap": task.overlap,
        "is_final": task.is_final,
        "samplerate": task.samplerate,
        "data_bytes": len(task.data),
        "samples": len(task.data) // 4,
        "language": task.language,
        "context": task.context,
        "time_start": task.time_start,
        "time_submit": task.time_submit,
        "data_sha256_prefix": hashlib.sha256(task.data).hexdigest()[:16],
    }


def result_payload(result) -> dict:
    """识别子进程**真实发出**的 Result 字段（父进程消费前的原样字段）。"""
    assert isinstance(result, Result), type(result)
    return {
        "task_id": result.task_id,
        "socket_id": result.socket_id,
        "owner_kind": result.owner_kind,
        "type": result.type,
        "duration": result.duration,
        "time_start": result.time_start,
        "time_submit": result.time_submit,
        "time_complete": result.time_complete,
        "text": result.text,
        "text_accu": result.text_accu,
        "tokens": list(result.tokens),
        "timestamps": list(result.timestamps),
        "is_final": result.is_final,
        "error_code": result.error_code,
        "error_message": result.error_message,
    }


def run_recording_worker(
    queue_in, queue_out, sockets_id, active_http_jobs, options, calls, received
):
    """真实 TaskHandler 子进程：额外记录实际收到的 Task 与实际发出的 Result。

    记录发生在队列的两侧，字段直接取自对象，断言因此覆盖真实
    multiprocessing 序列化边界，而不是在测试里手造协议 payload。
    """
    handler = TaskHandler(
        queue_in, queue_out, sockets_id, WorkerState(), active_http_jobs
    )
    handler.set_engine(
        ProgrammableFakeEngine(
            calls=calls, **_configure_fake_pipeline(options)
        )
    )
    real_get = queue_in.get
    real_put = queue_out.put

    def recording_get(*args, **kwargs):
        task = real_get(*args, **kwargs)
        if hasattr(task, "task_id"):
            received.append({"event": "task", **received_task_record(task)})
        return task

    def recording_put(result, *args, **kwargs):
        if isinstance(result, Result):
            received.append({"event": "result", **result_payload(result)})
        return real_put(result, *args, **kwargs)

    queue_in.get = recording_get
    queue_out.put = recording_put
    queue_out.put({"loaded": True, "aligner": "loaded"})
    handler.loop()
