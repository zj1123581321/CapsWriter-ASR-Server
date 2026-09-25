# coding: utf-8
"""
服务端状态管理模块

提供 ServerState (主进程) 和 WorkerState (子进程) 类。
"""

from __future__ import annotations
import asyncio
from collections import deque
from dataclasses import dataclass, field
from multiprocessing import Queue, Process
from multiprocessing.managers import ListProxy
from typing import TYPE_CHECKING, Dict, Optional

import websockets
from rich.console import Console

from core.server.schema import Result, RecognitionSession


TaskKey = tuple[str, str]


@dataclass
class TaskLifecycle:
    status: str = 'RECEIVING'


class SessionMap(dict):
    """以连接和任务组合键存会话，兼容管线当前的 task_id 成员检查。"""
    def __init__(self, owner):
        super().__init__()
        self.owner = owner

    def __contains__(self, key):
        if isinstance(key, str) and self.owner.current_socket_id:
            key = (self.owner.current_socket_id, key)
        return super().__contains__(key)

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

    tasks: Dict[TaskKey, TaskLifecycle] = field(default_factory=dict)
    connection_tasks: Dict[str, TaskKey] = field(default_factory=dict)
    out_queues: Dict[str, asyncio.Queue] = field(default_factory=dict)
    sender_tasks: Dict[str, asyncio.Task] = field(default_factory=dict)
    pending_segments: Dict[TaskKey, deque] = field(default_factory=dict)



@dataclass
class WorkerState:
    """
    识别子进程运行状态
    
    存储识别 Worker 进程运行时的状态：
    - sessions: 活跃识别会话，以 task_id 为键
    """
    # 识别会话集
    sessions: Dict[TaskKey, RecognitionSession] = field(init=False)
    current_socket_id: str = ''

    def __post_init__(self):
        self.sessions = SessionMap(self)
    
    # GPU 加速状态
    gpu_boosted: bool = False       # 当前是否已执行 GPU 加速
    gpu_last_active: float = 0.0    # 上次任务活跃时间，用于超时取消加速

    def get_session(self, task_id: str, socket_id: str = '', source: str = '') -> RecognitionSession:
        """获取或创建识别会话"""
        key = (socket_id, task_id)
        if key not in self.sessions:
            result = Result(task_id=task_id, socket_id=socket_id, type=source)
            self.sessions[key] = RecognitionSession(task_id=task_id, result=result)
        return self.sessions[key]
    
    def cleanup_sessions(self, sockets_id: ListProxy) -> int:
        """清理已断开连接的客户端 session"""
        stale_ids = [
            key for key, session in list(self.sessions.items())
            if session.result.socket_id not in sockets_id
        ]
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
    }
    for name, value in defaults.items():
        if not hasattr(state, name):
            setattr(state, name, value)


def begin_task(state, key: TaskKey) -> None:
    ensure_server_runtime(state)
    previous = state.connection_tasks.get(key[0])
    if previous is not None:
        state.tasks.pop(previous, None)
    state.tasks[key] = TaskLifecycle()
    state.connection_tasks[key[0]] = key
    state.pending_segments[key] = deque()


def set_task_draining(state, key: TaskKey) -> bool:
    record = state.tasks.get(key)
    if record is None or record.status != 'RECEIVING':
        return False
    record.status = 'DRAINING'
    return True


def transition_terminal(state, key: TaskKey, status: str) -> bool:
    """集中完成任务唯一终态转移；已终态或未知任务不重复转移。"""
    if status not in {'DONE', 'FAILED'}:
        raise ValueError(f'非法任务终态: {status}')
    ensure_server_runtime(state)
    record = state.tasks.get(key)
    if record is None or record.status in {'DONE', 'FAILED'}:
        return False
    record.status = status
    if state.connection_tasks.get(key[0]) == key:
        state.connection_tasks.pop(key[0], None)
    state.pending_segments.pop(key, None)
    return True


def register_segment_submission(state, key: TaskKey, submitted_at: float) -> None:
    ensure_server_runtime(state)
    state.pending_segments.setdefault(key, deque()).append(submitted_at)


def acknowledge_segment_result(state, key: TaskKey) -> None:
    ensure_server_runtime(state)
    pending = state.pending_segments.get(key)
    if pending:
        pending.popleft()
        if not pending:
            state.pending_segments.pop(key, None)
