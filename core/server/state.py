# coding: utf-8
"""
服务端状态管理模块

提供 ServerState (主进程) 和 WorkerState (子进程) 类。
"""

from __future__ import annotations
import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from multiprocessing import Queue, Process
from multiprocessing.managers import ListProxy
from typing import TYPE_CHECKING, Dict, Optional

import websockets
from rich.console import Console

from core.server.schema import Result, RecognitionSession
from core.tools.build_info import get_git_sha


OWNER_KINDS = frozenset({'ws', 'http'})
TaskKey = tuple[str, str, str]


def derive_owner_id(owner_kind: str, task_id: str, socket_id: str = '') -> str:
    """从真实任务字段推导归属 ID，不在状态中复制保存 owner_id。"""
    if owner_kind not in OWNER_KINDS:
        raise ValueError(f'未知任务归属类型: {owner_kind!r}')
    if not task_id:
        raise ValueError('task_id 不能为空')
    if owner_kind == 'ws':
        if not socket_id:
            raise ValueError('ws 任务必须携带 socket_id')
        return socket_id
    if socket_id:
        raise ValueError('http 任务的 socket_id 必须为空')
    return task_id


def make_task_key(owner_kind: str, task_id: str, socket_id: str = '') -> TaskKey:
    """所有主/子进程消费者共用的稳定任务键。"""
    return (
        owner_kind,
        derive_owner_id(owner_kind, task_id, socket_id),
        task_id,
    )


def task_key_from_task(task) -> TaskKey:
    return make_task_key(task.owner_kind, task.task_id, task.socket_id)


def task_key_from_result(result: Result) -> TaskKey:
    return make_task_key(result.owner_kind, result.task_id, result.socket_id)


@dataclass
class TaskLifecycle:
    status: str = 'RECEIVING'
    segment_slots: asyncio.Semaphore | None = None
    terminal_event: asyncio.Event = field(default_factory=asyncio.Event)
    idle_state_condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    idle_state_version: int = 0
    idle_deadline: float | None = None
    backpressured: bool = False
    started_at: float = field(default_factory=time.monotonic)
    samples_total: int = 0
    segments: int = 0
    declared_encoding: str | None = None
    encoding: str = 'v1'
    encoding_set: bool = False
    error_code: str | None = None


if TYPE_CHECKING:
    from .app import CapsWriterServer

# Rich console 用于控制台输出（服务端统一使用此实例）
console = Console(highlight=False)


@dataclass
class ServerState:
    """
    主进程运行状态
    
    存储服务端主进程运行时的共享状态：
    - sockets: WebSocket 连接字典，以 socket_id 为键
    - sockets_id: 跨进程的 socket ID 列表（由 Manager 创建）
    - queue_in: 任务输入队列（主进程 -> 识别进程）
    - queue_out: 结果输出队列（识别进程 -> 主进程）
    - recognize_process: 识别子进程句柄
    """
    app: Optional[CapsWriterServer] = None

    # WebSocket 连接池
    sockets: Dict[str, websockets.WebSocketServerProtocol] = field(default_factory=dict)
    
    # 跨进程共享的 socket ID 列表（需要用 Manager().list() 初始化）
    sockets_id: Optional[ListProxy] = None
    
    # 消息队列
    queue_in: Queue = field(default_factory=Queue)
    queue_out: Queue = field(default_factory=Queue)

    # 识别子进程
    recognize_process: Optional[Process] = None

    # 服务启动时读取的代码版本，供 /health 报告。
    git_sha: str = field(default_factory=get_git_sha)

    tasks: Dict[TaskKey, TaskLifecycle] = field(default_factory=dict)
    connection_tasks: Dict[str, TaskKey] = field(default_factory=dict)
    out_queues: Dict[str, asyncio.Queue] = field(default_factory=dict)
    sender_tasks: Dict[str, asyncio.Task] = field(default_factory=dict)
    pending_segments: Dict[TaskKey, deque] = field(default_factory=dict)
    # HTTP runner 注册的稳定 job_id；由 Manager().list() 跨进程共享。
    active_http_jobs: Optional[ListProxy] = None
    # E3 注入持久结果消费者；E1 不实现其存储语义。
    http_result_sink: object = None



@dataclass
class WorkerState:
    """
    识别子进程运行状态
    
    存储识别 Worker 进程运行时的状态：
    - sessions: 活跃识别会话，以 task_id 为键
    """
    # 识别会话集
    sessions: Dict[TaskKey, RecognitionSession] = field(init=False)

    def __post_init__(self):
        self.sessions = {}
    
    # GPU 加速状态
    gpu_boosted: bool = False       # 当前是否已执行 GPU 加速
    gpu_last_active: float = 0.0    # 上次任务活跃时间，用于超时取消加速

    def get_session(
        self,
        task_id: str,
        socket_id: str = '',
        source: str = '',
        owner_kind: str = 'ws',
    ) -> RecognitionSession:
        """获取或创建识别会话"""
        key = make_task_key(owner_kind, task_id, socket_id)
        if key not in self.sessions:
            result = Result(
                task_id=task_id,
                socket_id=socket_id,
                type=source,
                owner_kind=owner_kind,
            )
            self.sessions[key] = RecognitionSession(task_id=task_id, result=result)
        return self.sessions[key]
    
    def cleanup_sessions(
        self,
        sockets_id: ListProxy,
        active_http_jobs: Optional[ListProxy] = None,
    ) -> int:
        """按 owner 清理已断开 WS 与不再活跃的 HTTP session。"""
        stale_ids = []
        for key, session in list(self.sessions.items()):
            owner_kind = session.result.owner_kind
            if owner_kind == 'ws':
                stale = session.result.socket_id not in sockets_id
            elif owner_kind == 'http':
                if active_http_jobs is None:
                    raise RuntimeError('HTTP 活跃任务集合不可用')
                stale = session.result.task_id not in active_http_jobs
            else:
                raise ValueError(f'未知任务归属类型: {owner_kind!r}')
            if stale:
                stale_ids.append(key)
        for key in stale_ids:
            self.sessions.pop(key, None)
        if stale_ids:
            from . import logger
            logger.debug(f"清理了 {len(stale_ids)} 个已断开连接的 session")
        return len(stale_ids)


def ensure_server_runtime(state) -> None:
    """为生产态与测试骨架补齐连接任务运行时字段。"""
    defaults = {
        'tasks': {},
        'connection_tasks': {},
        'out_queues': {},
        'sender_tasks': {},
        'pending_segments': {},
        'active_http_jobs': None,
        'http_result_sink': None,
    }
    for name, value in defaults.items():
        if not hasattr(state, name):
            setattr(state, name, value)


def begin_task(state, key: TaskKey, max_inflight_segments: int = 4) -> None:
    ensure_server_runtime(state)
    owner_kind, owner_id, task_id = key
    if owner_kind == 'ws':
        derive_owner_id(owner_kind, task_id, owner_id)
    elif owner_kind == 'http':
        derive_owner_id(owner_kind, task_id)
        if state.active_http_jobs is None:
            raise RuntimeError('HTTP 活跃任务集合不可用')
        if task_id not in state.active_http_jobs:
            raise RuntimeError(f'HTTP 任务未注册为活跃任务: {task_id}')
    else:
        raise ValueError(f'未知任务归属类型: {owner_kind!r}')
    if owner_kind == 'ws':
        previous = state.connection_tasks.get(owner_id)
        if previous is not None:
            state.tasks.pop(previous, None)
    state.tasks[key] = TaskLifecycle(
        segment_slots=asyncio.Semaphore(max_inflight_segments)
    )
    if owner_kind == 'ws':
        state.connection_tasks[owner_id] = key
    state.pending_segments[key] = deque()


def register_http_job(state, job_id: str) -> None:
    """在 HTTP runner 首次入队前登记稳定 job_id。"""
    ensure_server_runtime(state)
    if state.active_http_jobs is None:
        raise RuntimeError('HTTP 活跃任务集合不可用')
    if not job_id:
        raise ValueError('HTTP job_id 不能为空')
    if job_id not in state.active_http_jobs:
        state.active_http_jobs.append(job_id)


def set_task_draining(state, key: TaskKey) -> bool:
    record = state.tasks.get(key)
    if record is None or record.status != 'RECEIVING':
        return False
    record.status = 'DRAINING'
    return True


def transition_terminal(state, key: TaskKey, status: str, code: str | None = None) -> bool:
    """集中完成任务唯一终态转移；已终态或未知任务不重复转移。"""
    if status not in {'DONE', 'FAILED'}:
        raise ValueError(f'非法任务终态: {status}')
    if key[0] not in OWNER_KINDS:
        raise ValueError(f'未知任务归属类型: {key[0]!r}')
    derive_owner_id(key[0], key[2], key[1] if key[0] == 'ws' else '')
    ensure_server_runtime(state)
    record = state.tasks.get(key)
    if record is None or record.status in {'DONE', 'FAILED'}:
        return False
    record.status = status
    record.error_code = code or record.error_code
    record.terminal_event.set()
    record.segment_slots = None
    if key[0] == 'ws':
        if state.connection_tasks.get(key[1]) == key:
            state.connection_tasks.pop(key[1], None)
    else:
        if state.active_http_jobs is None:
            raise RuntimeError('HTTP 活跃任务集合不可用')
        if key[2] in state.active_http_jobs:
            state.active_http_jobs.remove(key[2])
    state.pending_segments.pop(key, None)
    from . import logger
    logger.info(
        f"task_end owner_kind={key[0]} owner={key[1]} task={key[2]} "
        f"status={'done' if status == 'DONE' else 'failed'} "
        f"code={record.error_code or '-'} "
        f"duration_s={record.samples_total / 16000:.3f} "
        f"elapsed_s={time.monotonic() - record.started_at:.3f} "
        f"segments={record.segments} encoding={record.encoding}"
    )
    return True


def register_segment_submission(state, key: TaskKey, submitted_at: float) -> None:
    ensure_server_runtime(state)
    state.pending_segments.setdefault(key, deque()).append(submitted_at)


def acknowledge_segment_result(state, key: TaskKey) -> None:
    ensure_server_runtime(state)
    pending = state.pending_segments.get(key)
    if pending:
        pending.popleft()
        record = state.tasks.get(key)
        if record is not None and record.segment_slots is not None:
            record.segment_slots.release()
        if not pending:
            state.pending_segments.pop(key, None)
