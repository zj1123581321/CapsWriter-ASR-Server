# coding: utf-8
"""识别子进程按任务轮转，麦克风任务优先。"""

from core.server.schema import Task
from core.server.state import WorkerState
from core.server.worker.task_handler import TaskBuffer


def make_task(task_id, segment, source="file"):
    return Task(
        type=source,
        data=f"{task_id}{segment}".encode(),
        offset=0.0,
        overlap=0.0,
        task_id=task_id,
        socket_id=f"socket-{task_id}",
        is_final=False,
        time_start=0.0,
        time_submit=0.0,
    )


def test_file_tasks_rotate_by_segment_fifo():
    buffer = TaskBuffer(WorkerState())
    for task_id in ("A", "B", "C"):
        for segment in (1, 2, 3):
            buffer.enqueue(make_task(task_id, segment))

    order = [buffer.pop().data.decode() for _ in range(9)]
    assert order == ["A1", "B1", "C1", "A2", "B2", "C2", "A3", "B3", "C3"]


def test_microphone_task_is_next_even_when_files_are_waiting():
    buffer = TaskBuffer(WorkerState())
    for task_id in ("A", "B", "C"):
        for segment in (1, 2, 3):
            buffer.enqueue(make_task(task_id, segment))

    assert [buffer.pop().data.decode() for _ in range(3)] == ["A1", "B1", "C1"]
    buffer.enqueue(make_task("mic", 1, source="mic"))
    assert buffer.pop().data == b"mic1"
    assert buffer.pop().data == b"A2"


def test_multiple_microphone_tasks_rotate_ahead_of_files():
    buffer = TaskBuffer(WorkerState())
    buffer.enqueue(make_task("A", 1))
    buffer.enqueue(make_task("mic-a", 1, source="mic"))
    buffer.enqueue(make_task("mic-b", 1, source="mic"))

    assert [buffer.pop().data.decode() for _ in range(3)] == ["mic-a1", "mic-b1", "A1"]
