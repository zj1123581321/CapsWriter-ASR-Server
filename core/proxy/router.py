# coding: utf-8
"""Task-aware routing and backend WebSocket lifecycle management."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from time import monotonic
from typing import Awaitable, Callable, Deque, Dict, Iterable, Optional

from core.logger import get_logger
from core.protocol import AudioMessage, RecognitionMessage

from .backend import BackendState

logger = get_logger("proxy")


class NoHealthyBackendError(RuntimeError):
    """Raised when no backend can accept a new task."""


class NoCompatibleBackendError(NoHealthyBackendError):
    """Raised when no healthy v2 backend supports the requested encoding."""

    def __init__(self, task_id: str | None, encoding):
        self.task_id = task_id
        self.encoding = encoding
        super().__init__(f"没有健康且支持编码 {encoding!r} 的 v2 后端")


def parse_audio_message(raw_message: str) -> AudioMessage:
    """Parse client AudioMessage JSON and validate the CapsWriter protocol."""
    data = json.loads(raw_message)
    return AudioMessage.from_dict(data)


def is_final_recognition_message(raw_message: str) -> bool:
    """Return True when a backend RecognitionMessage is the final result."""
    data = json.loads(raw_message)
    return bool(data.get("is_final", False))


def recognition_task_id(raw_message: str) -> Optional[str]:
    data = json.loads(raw_message)
    task_id = data.get("task_id")
    return str(task_id) if task_id is not None else None


ConnectFunc = Callable[[str], Awaitable[object]]


@dataclass
class TaskSession:
    task_id: str
    backend: BackendState
    backend_ws: object
    outbound_queue: asyncio.Queue
    client_to_backend_task: asyncio.Task
    backend_to_client_task: asyncio.Task
    start_time: float = 0.0


class TaskRouter:
    """Routes each task_id to one short-lived backend WebSocket connection."""

    def __init__(
        self,
        backends: Iterable[BackendState],
        connect_func: Optional[ConnectFunc] = None,
        latency_ttl_seconds: int = 300,
        task_history: Optional[Deque[dict]] = None,
    ):
        self.backends = list(backends)
        self.task_sessions: Dict[str, TaskSession] = {}
        self._connect_func = connect_func
        self.latency_ttl_seconds = latency_ttl_seconds
        self.task_history = task_history

    def select_backend(
        self,
        encoding=None,
        require_v2: bool = False,
        task_id: str | None = None,
    ) -> BackendState:
        if not self.backends:
            if require_v2:
                raise NoCompatibleBackendError(task_id, encoding)
            raise NoHealthyBackendError("No ASR backend is configured")

        healthy_backends = [backend for backend in self.backends if backend.healthy]
        if require_v2:
            healthy_backends = [
                backend
                for backend in healthy_backends
                if backend.protocol_version >= 2 and encoding in backend.encodings
            ]
            if not healthy_backends:
                raise NoCompatibleBackendError(task_id, encoding)
        if not healthy_backends:
            raise NoHealthyBackendError("所有后端均不健康，等待探活恢复")

        best_score = min(self.backend_score(b) for b in healthy_backends)
        tied = [b for b in healthy_backends if self.backend_score(b) == best_score]
        selected = min(tied, key=lambda b: b._last_selected)
        selected.mark_selected()
        logger.info(
            "后端选择: backend=%s active_tasks=%s weight=%.3f avg_latency=%.3f latency_samples=%s score=%.6f",
            selected.id,
            selected.active_tasks,
            selected.weight,
            selected.avg_latency,
            selected.latency_samples,
            self.backend_score(selected),
        )
        logger.debug(
            "后端诊断延迟(仅日志): %s",
            ", ".join(
                f"{backend.id}:avg_latency={backend.avg_latency:.3f},samples={backend.latency_samples}"
                for backend in self.backends
            ),
        )
        return selected

    def backend_score(self, backend: BackendState) -> float:
        return (backend.active_tasks + 1) / backend.weight

    def get_backend_for_task(self, task_id: str) -> Optional[BackendState]:
        session = self.task_sessions.get(task_id)
        return session.backend if session else None

    async def route_client_message(self, raw_message: str, client_ws) -> None:
        data = json.loads(raw_message)
        msg = AudioMessage.from_dict(data)
        session = self.task_sessions.get(msg.task_id)
        if session is None:
            session = await self._open_task_session(
                msg.task_id,
                client_ws,
                encoding=data.get("encoding"),
                require_v2="encoding" in data,
            )
        await session.outbound_queue.put(raw_message)
        if self.task_sessions.get(msg.task_id) is not session:
            return

    async def close_all(self) -> None:
        task_ids = list(self.task_sessions)
        await asyncio.gather(
            *(self.close_session(task_id, status="failed") for task_id in task_ids),
            return_exceptions=True,
        )

    async def close_session(
        self,
        task_id: str,
        status: Optional[str] = None,
        audio_duration: float = 0.0,
        inference_latency: float = 0.0,
    ) -> None:
        session = self.task_sessions.pop(task_id, None)
        if session is None:
            return

        session.backend.release_task()
        if status is not None:
            self._record_task_history(
                task_id=task_id,
                backend_id=session.backend.id,
                status=status,
                duration=monotonic() - session.start_time,
                audio_duration=audio_duration,
                inference_latency=inference_latency,
            )
        while not session.outbound_queue.empty():
            session.outbound_queue.get_nowait()

        current = asyncio.current_task()
        for task in (session.client_to_backend_task, session.backend_to_client_task):
            if task is not current and not task.done():
                task.cancel()

        try:
            await session.backend_ws.close()
        except Exception:
            logger.debug("后端连接关闭时出现异常，任务ID: %s", task_id, exc_info=True)

        logger.info(
            "任务连接已释放: task_id=%s backend=%s active_tasks=%s",
            task_id,
            session.backend.id,
            session.backend.active_tasks,
        )

    async def _open_task_session(
        self,
        task_id: str,
        client_ws,
        encoding=None,
        require_v2: bool = False,
    ) -> TaskSession:
        start_time = monotonic()
        tried = set()
        last_error = None
        while True:
            backend = self.select_backend(
                encoding=encoding,
                require_v2=require_v2,
                task_id=task_id,
            )
            if backend.id in tried:
                raise NoHealthyBackendError(
                    f"所有后端连接失败: task_id={task_id} tried={tried}"
                )
            tried.add(backend.id)
            backend.acquire_task()
            try:
                backend_ws = await self._connect_backend(backend.url)
            except asyncio.CancelledError:
                backend.release_task()
                self._record_task_history(
                    task_id=task_id,
                    backend_id=backend.id,
                    status="cancelled",
                    duration=monotonic() - start_time,
                )
                raise
            except Exception as exc:
                backend.release_task()
                backend.record_connect_failure()
                logger.warning(
                    "后端连接失败，尝试下一个: backend=%s url=%s failures=%s healthy=%s",
                    backend.id,
                    backend.url,
                    backend.consecutive_failures,
                    backend.healthy,
                    exc_info=True,
                )
                last_error = exc
                continue
            break

        backend.record_connect_success()
        outbound_queue = asyncio.Queue(maxsize=8)
        session = TaskSession(
            task_id=task_id,
            backend=backend,
            backend_ws=backend_ws,
            outbound_queue=outbound_queue,
            client_to_backend_task=asyncio.create_task(
                self._client_to_backend(task_id, backend_ws, outbound_queue)
            ),
            backend_to_client_task=asyncio.create_task(
                self._backend_to_client(task_id, backend_ws, client_ws)
            ),
            start_time=start_time,
        )
        self.task_sessions[task_id] = session
        logger.info(
            "新任务路由: task_id=%s backend=%s url=%s active_tasks=%s score=%.6f",
            task_id,
            backend.id,
            backend.url,
            backend.active_tasks,
            self.backend_score(backend),
        )
        return session

    async def _connect_backend(self, url: str):
        if self._connect_func is not None:
            return await self._connect_func(url)

        import websockets

        kwargs = {
            "uri": url,
            "max_size": None,
            "ping_interval": None,
        }
        version_parts = tuple(int(part) for part in websockets.__version__.split(".")[:2])
        if version_parts >= (14, 0):
            kwargs["proxy"] = None
        return await websockets.connect(**kwargs)

    async def _client_to_backend(self, task_id: str, backend_ws, outbound_queue: asyncio.Queue) -> None:
        try:
            while True:
                raw_message = await outbound_queue.get()
                await backend_ws.send(raw_message)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("客户端到后端转发失败: task_id=%s", task_id, exc_info=True)
            await self.close_session(task_id, status="failed")
            raise

    async def _backend_to_client(self, task_id: str, backend_ws, client_ws) -> None:
        try:
            async for raw_message in backend_ws:
                session = self.task_sessions.get(task_id)
                if session is None:
                    return
                session.backend.record_result()
                self._record_backend_latency(session.backend, raw_message)
                await client_ws.send(raw_message)
                if recognition_task_id(raw_message) == task_id and is_final_recognition_message(raw_message):
                    try:
                        msg = RecognitionMessage.from_dict(json.loads(raw_message))
                        inference_latency = float(msg.time_complete) - float(msg.time_submit)
                        audio_duration = float(msg.duration)
                    except Exception:
                        inference_latency = float("nan")
                        audio_duration = 0.0
                    end_to_end = monotonic() - session.start_time
                    logger.info(
                        "任务完成: task_id=%s backend=%s inference_latency=%.3f end_to_end=%.3f active_tasks=%s",
                        task_id,
                        session.backend.id,
                        inference_latency,
                        end_to_end,
                        session.backend.active_tasks,
                    )
                    await self.close_session(
                        task_id,
                        status="completed",
                        audio_duration=audio_duration,
                        inference_latency=inference_latency,
                    )
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("后端到客户端转发失败: task_id=%s", task_id, exc_info=True)
            await self.close_session(task_id, status="failed")
            try:
                await client_ws.close()
            except Exception:
                logger.debug("客户端连接关闭失败: task_id=%s", task_id, exc_info=True)
            raise
        await self.close_session(task_id, status="failed")

    def _record_backend_latency(self, backend: BackendState, raw_message: str) -> None:
        try:
            latency = self._processing_latency(raw_message)
            backend.record_processing_latency(latency, latency_ttl_seconds=self.latency_ttl_seconds)
        except Exception:
            logger.debug("无法解析后端 RecognitionMessage，跳过延迟统计: backend=%s", backend.id, exc_info=True)
            return

    def _processing_latency(self, raw_message: str) -> float:
        message = RecognitionMessage.from_dict(json.loads(raw_message))
        return float(message.time_complete) - float(message.time_submit)

    def _record_task_history(
        self,
        task_id: str,
        backend_id: str,
        status: str,
        duration: float,
        audio_duration: float = 0.0,
        inference_latency: float = 0.0,
    ) -> None:
        if self.task_history is None:
            return
        record: dict = {
            "task_id": task_id,
            "backend_id": backend_id,
            "status": status,
            "duration": duration,
            "timestamp": time.time(),
        }
        if audio_duration > 0:
            record["audio_duration"] = audio_duration
            record["inference_latency"] = inference_latency
            record["rtf"] = inference_latency / audio_duration if audio_duration > 0 else 0.0
            record["network_latency"] = duration - inference_latency
        self.task_history.append(record)
