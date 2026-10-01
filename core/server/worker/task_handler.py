# coding: utf-8
"""
识别任务处理器

负责监听任务队列、执行识别流水线并将结果返回主进程。

公平调度：按任务轮转取段，防止文件转录饿死旧任务；麦克风段优先处理。
同一任务内保持 FIFO 顺序。
"""

from collections import OrderedDict, deque
from multiprocessing import Queue
from multiprocessing.managers import ListProxy
import queue
from .pipeline import TaskPipeline
from ..schema import Result
from ..state import TaskKey, WorkerState, task_key_from_task
from .gpu_boost import GpuBoostManager
from . import logger
from config_server import ServerConfig as Config


class TaskBuffer:
    """按 task_id 分组缓冲，支持跨 session 轮转出队。"""
    def __init__(self, state: WorkerState):
        self.state = state
        self._buffers: OrderedDict[tuple[str, str], deque] = OrderedDict()

    def enqueue(self, task):
        """将任务放入对应 task_id 的缓冲尾部（同 session 内 FIFO）。
        首次遇到新 task_id 时预创建 session。"""
        key = task_key_from_task(task)
        if key not in self._buffers:
            self._buffers[key] = deque()
            self.state.get_session(
                task.task_id,
                task.socket_id,
                task.type,
                task.owner_kind,
            )
        self._buffers[key].append(task)

    def pop(self):
        """优先轮转麦克风任务，否则轮转文件任务。"""
        if not self._buffers:
            return None

        key = next(
            (key for key, buf in self._buffers.items() if buf[0].type == 'mic'),
            next(iter(self._buffers)),
        )
        buf = self._buffers[key]
        task = buf.popleft()

        if not buf:
            del self._buffers[key]
        else:
            self._buffers.move_to_end(key)

        return task

    def cleanup_tasks(self):
        """清理已断开连接的 session 的缓冲任务。"""
        for key in list(self._buffers):
            if key not in self.state.sessions:
                logger.debug(f"清理断开连接的 session: {key[1][:8]}")
                del self._buffers[key]

    def discard(self, key):
        """丢弃一个已失败任务仍留在轮转缓冲中的段。"""
        self._buffers.pop(key, None)

    @property
    def is_empty(self) -> bool:
        return len(self._buffers) == 0


class TaskHandler:
    """
    任务处理器

    协调输入输出队列与识别引擎之间的任务流。
    支持跨 socket 公平轮转调度。
    """
    def __init__(
        self,
        queue_in: Queue,
        queue_out: Queue,
        sockets_id: ListProxy,
        state: WorkerState,
        active_http_jobs: ListProxy | None = None,
    ):
        self.queue_in = queue_in
        self.queue_out = queue_out
        self.sockets_id = sockets_id
        self.active_http_jobs = active_http_jobs
        self.state = state

        self.recognizer = None
        self.punc_model = None
        self.aligner = None
        self.pipeline = None

        self.buffer = TaskBuffer(state)
        self.gpu_boost = GpuBoostManager(state)
        self.failed_tasks: set[TaskKey] = set()

    def set_engine(self, recognizer, punc_model=None, aligner=None):
        """注入识别引擎实例并初始化管线"""
        self.recognizer = recognizer
        self.punc_model = punc_model
        self.aligner = aligner
        self.pipeline = TaskPipeline(recognizer, punc_model, aligner, self.state)

    def drain_queue(self) -> bool:
        """单轮最多取入配置数量的任务。Returns: False = 退出信号。"""
        drained = 0
        while True:
            # 获取任务
            try:
                if self.buffer.is_empty:
                    task = self.queue_in.get(timeout=1)
                else:
                    task = self.queue_in.get(timeout=0.02)
            except queue.Empty:
                if self.buffer.is_empty:
                    self.cleanup_engines()
                    continue
                else:
                    return True
            except InterruptedError:
                continue
            
            # 判断退出信号
            if task is None:
                return False
            drained += 1

            key = task_key_from_task(task)
            if not self._owner_is_active(key):
                logger.debug(
                    f"跳过非活动 {key[0]} 任务: {task.task_id[:8]}"
                )
                continue

            if key in self.failed_tasks:
                logger.debug(f"跳过已失败任务迟到片段: {task.task_id[:8]}")
                if task.is_final:
                    self.failed_tasks.discard(key)
                continue

            # 任务进入缓冲区
            self.buffer.enqueue(task)
            if drained >= Config.drain_batch:
                return True

    def cleanup(self):
        """清理失活 owner 的缓冲任务和 session。"""
        self.state.cleanup_sessions(self.sockets_id, self.active_http_jobs)
        self.buffer.cleanup_tasks()
        self.failed_tasks = {
            key for key in self.failed_tasks if self._owner_is_active(key)
        }

    def _owner_is_active(self, key: TaskKey) -> bool:
        if key[0] == 'ws':
            if self.sockets_id is None:
                raise RuntimeError('WS socket 活跃集合不可用')
            return key[1] in self.sockets_id
        if key[0] == 'http':
            if self.active_http_jobs is None:
                raise RuntimeError('HTTP 活跃任务集合不可用')
            return key[2] in self.active_http_jobs
        raise ValueError(f'未知任务归属类型: {key[0]!r}')

    def cleanup_engines(self):
        """闲置资源清理：对齐器卸载 + GPU 加速取消。"""
        if self.pipeline and self.pipeline.aligner:
            self.pipeline.aligner.check_idle()
        self.gpu_boost.check_idle()

    def handle_command_task(self, task):
        """处理命令任务。"""
        self.gpu_boost.handle_command(task)

    def handle_audio_task(self, task):
        """处理音频识别任务。"""
        key = task_key_from_task(task)
        if key in self.failed_tasks:
            return
        try:
            result = self.pipeline.process(task)
        except Exception as e:
            self.state.sessions.pop(key, None)
            self.failed_tasks.add(key)
            self.buffer.discard(key)
            result = Result(
                task_id=task.task_id,
                socket_id=task.socket_id,
                type=task.type,
                owner_kind=task.owner_kind,
                error_code='inference_failed',
                error_message=(
                    f"推理片段 offset={task.offset:.3f}s，"
                    f"{type(e).__name__}: {e}"
                ),
            )
        self.queue_out.put(result)
        if result.is_final:
            self.state.sessions.pop(key, None)

    def loop(self):
        """核心任务循环：drain 队列 → 清理断连 → 轮转执行一个。"""
        logger.info("TaskHandler 开始工作循环 (公平调度)")

        while True:
            try:
                if not self.drain_queue():
                    break

                task = self.buffer.pop()
                if task is None:
                    continue

                # 根据任务类型分派
                if task.type == 'cmd':
                    self.handle_command_task(task)
                else:
                    self.handle_audio_task(task)

                self.cleanup()
            except InterruptedError:
                continue
            except Exception:
                logger.error("TaskHandler 出现未预期异常，识别进程退出", exc_info=True)
                raise

        logger.info("TaskHandler 工作循环结束")
