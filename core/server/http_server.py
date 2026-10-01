# coding: utf-8
"""
HTTP 文件任务 listener（HttpServer）

六个 route 全部按 public 契约实现，错误体统一为 {code, message, request_id}，
offset 冲突额外带 confirmed_offset。

关键约束：
  * 二进制请求体不是 JSON/Base64/multipart，不接受额外 Content-Encoding；
  * 所有读写都要求 Authorization: Bearer，编号本身不赋权；
  * 文件写入与 SQLite 只发生在受监督的单 I/O worker 线程，不阻塞 WS 事件循环，
    也不使用无界 default executor；
  * 取消 HTTP 请求不结束已经开始的底层 I/O：route 取消只让等待方退出，
    线程内 I/O 仍跑到完成并由 done-callback 观察异常。
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from core.server.http_store import (
    IO_MAILBOX,
    MAX_BODY_CONCURRENCY,
    MAX_CHUNK_BYTES,
    MAX_HANDLERS,
    MAX_JSON_BYTES,
    READ_CHUNK_BYTES,
    HttpStore,
    HttpStoreError,
    validate_identity,
)

logger = logging.getLogger("server")


class HttpServerError(Exception):
    """HTTP 装配/监督失败：调用方必须以非零退出，不得吞掉。"""


def _import_aiohttp():
    """按需导入 aiohttp：HTTP 未启用时不强制该运行时存在。"""
    if sys.version_info < (3, 10):
        raise HttpServerError("HTTP 文件任务需要 Python >= 3.10")
    try:
        from aiohttp import web
    except ImportError as exc:  # 启用但不满足依赖：明确启动失败，不 fallback
        raise HttpServerError("启用 HTTP 文件任务需要 aiohttp==3.14.3") from exc
    return web


class HttpIoWorker:
    """单线程、受监督、有界 mailbox 的 I/O worker。

    mailbox 用信号量限流（同时在途操作 <= IO_MAILBOX），线程池固定为 1，
    因此 SQLite 连接与文件写入永远在同一个线程。
    """

    def __init__(self, mailbox: int = IO_MAILBOX):
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="http-io")
        self._mailbox = asyncio.Semaphore(mailbox)
        self._pending: set = set()
        self._closed = False

    def run_sync(self, func, *args, timeout: float = 30.0):
        """在 I/O 线程里同步执行（装配/收尾用）：SQLite 连接只在那个线程创建与使用。"""
        if self._closed:
            raise HttpServerError("HTTP I/O worker 已关闭")
        return self._executor.submit(func, *args).result(timeout=timeout)

    async def run(self, func, *args, **kwargs):
        """把同步 store 操作交给 I/O worker；route 被取消时底层 I/O 仍会跑完。"""
        if self._closed:
            raise HttpServerError("HTTP I/O worker 已关闭")
        await self._mailbox.acquire()
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executor, functools.partial(func, *args, **kwargs))
        task = asyncio.ensure_future(future)
        self._pending.add(task)
        task.add_done_callback(self._on_done)
        try:
            return await asyncio.shield(task)
        finally:
            self._mailbox.release()

    def _on_done(self, task: asyncio.Future) -> None:
        self._pending.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            # route 可能已取消等待方，这里是唯一观察真实 I/O 异常的地方
            logger.error("http io operation failed: %s", exc)

    def close(self) -> None:
        """关闭时等待在途 I/O 结束（已在跑的字节写入不会被半途抛弃）。"""
        self._closed = True
        self._executor.shutdown(wait=True)


class HttpServer:
    """aiohttp listener + I/O worker + 监督。"""

    def __init__(self, app, addr: str, port: int, data_dir: Path):
        self.app = app
        self.addr = addr
        self.port = port
        self.data_dir = Path(data_dir)
        self._web = None
        self._store: Optional[HttpStore] = None
        self._worker: Optional[HttpIoWorker] = None
        self._runner = None
        self._site = None
        self._handler_slots = asyncio.Semaphore(MAX_HANDLERS)
        self._body_slots = asyncio.Semaphore(MAX_BODY_CONCURRENCY)
        self.fatal: Optional[BaseException] = None
        # 真实推理协调者是否已装配（M3 注入实现）；默认 False → commit 明确 503
        self.inference_available = False
        self._bound_port: Optional[int] = None

    # ---------------- 装配 ----------------

    def prepare(self) -> "HttpServer":
        """在事件循环外完成装配：aiohttp 导入、数据目录、schema、独占锁。失败即抛。"""
        self._web = _import_aiohttp()
        self._worker = HttpIoWorker()
        try:
            # 存储装配（含 OS 独占锁与 schema 校验）也在 I/O 线程里完成，失败即抛
            self._store = self._worker.run_sync(self._open_store)
        except HttpStoreError:
            self._worker.close()
            raise
        except (OSError, RuntimeError) as exc:
            self._worker.close()
            raise HttpServerError(f"HTTP 存储初始化失败：{exc}") from exc
        application = self._web.Application()
        self._add_routes(application)
        self._runner = self._web.AppRunner(application, access_log=None)
        return self

    def _open_store(self) -> HttpStore:
        return HttpStore(self.data_dir, inference_ready=self._inference_ready).open()

    def _inference_ready(self) -> bool:
        """实际推理协调者是否可用。

        本增量没有真实文件 runner（属于 E3），因此恒为 False：外部 commit 明确
        503 inference_unavailable，而不是受理后永远排队。E3 注入真实实现即可。
        """
        return bool(self.inference_available)

    def _add_routes(self, application) -> None:
        web = self._web
        application.router.add_post("/v1/uploads", self._wrap(self._create_upload))
        application.router.add_get("/v1/uploads/{upload_id}", self._wrap(self._get_upload))
        application.router.add_patch("/v1/uploads/{upload_id}", self._wrap(self._patch_upload))
        application.router.add_post("/v1/uploads/{upload_id}/commit", self._wrap(self._commit_upload))
        application.router.add_get("/v1/jobs/{job_id}", self._wrap(self._get_job))
        application.router.add_get("/v1/jobs/{job_id}/result", self._wrap(self._get_result))

    # ---------------- 监督 ----------------

    async def serve(self) -> None:
        """启动监听并挂起，直到被 stop() 关闭或装配失败（由 prepare 抛出）。"""
        if self._runner is None or self._store is None:
            raise HttpServerError("HTTP listener 未完成装配")
        await self._runner.setup()
        try:
            self._site = self._web.TCPSite(self._runner, self.addr, self.port)
            await self._site.start()
        except OSError as exc:
            raise HttpServerError(f"HTTP 监听失败：{self.addr}:{self.port}（{exc}）") from exc
        sockets = getattr(self._site, "_server", None)
        if sockets is not None and sockets.sockets:
            self._bound_port = sockets.sockets[0].getsockname()[1]
        logger.info(f"HTTP 文件任务 listener 已就绪 (监听: {self.addr}:{self._bound_port or self.port})")
        try:
            await asyncio.Event().wait()
        finally:
            await self.stop()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self._worker is not None and self._store is not None:
            self._worker.run_sync(self._store.close)
        if self._worker is not None:
            self._worker.close()
            self._worker = None
        self._store = None
        logger.info("HTTP 文件任务 listener 已停止")

    # ---------------- 请求基础设施 ----------------

    def _wrap(self, handler):
        async def wrapped(request):
            async with self._handler_slots:
                request_id = uuid.uuid4().hex
                request["request_id"] = request_id
                try:
                    return await handler(request)
                except HttpStoreError as exc:
                    return self._error_response(exc.status, exc.code, exc.message, request_id, exc.confirmed_offset)
                except asyncio.CancelledError:
                    # route 取消只结束等待方，底层 I/O 由 HttpIoWorker 跑完
                    raise
                except Exception as exc:  # 未知错误：显式失败并让监督看到，不静默成功
                    logger.error("http request failed: %s", exc)
                    self.fatal = exc
                    return self._error_response(500, "internal_error", "服务端内部错误", request_id)

        return wrapped

    def _error_response(self, status, code, message, request_id, confirmed_offset=None):
        payload = {"code": code, "message": message, "request_id": request_id}
        if confirmed_offset is not None:
            payload["confirmed_offset"] = confirmed_offset
        return self._json(status, payload)

    def _json(self, status, payload, headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        base = {"Content-Type": "application/json; charset=utf-8"}
        base.update(headers or {})
        return self._web.Response(status=status, body=body, headers=base)

    @staticmethod
    def _token(request) -> str:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise HttpStoreError("unauthorized", "缺少 Bearer 凭据", status=401)
        token = header[len("Bearer "):].strip()
        if not token:
            raise HttpStoreError("unauthorized", "缺少 Bearer 凭据", status=401)
        return token

    @staticmethod
    def _reject_encoding(request) -> None:
        encoding = request.headers.get("Content-Encoding")
        if encoding and encoding.strip().lower() not in ("", "identity"):
            raise HttpStoreError("unsupported_encoding", "不接受压缩或其他 Content-Encoding", status=415)

    @staticmethod
    def _content_length(request) -> int:
        raw = request.headers.get("Content-Length")
        if raw is None:
            raise HttpStoreError("length_required", "必须显式声明 Content-Length", status=411)
        try:
            return int(raw)
        except ValueError as exc:
            raise HttpStoreError("length_required", "Content-Length 必须是整数", status=400) from exc

    async def _read_body(self, request, limit: int, expected: Optional[int] = None) -> bytes:
        """按 64 KiB 有界读取，绝不把整份 1 GiB 上传 append 进内存。"""
        async with self._body_slots:
            buffer = bytearray()
            while True:
                chunk = await request.content.read(READ_CHUNK_BYTES)
                if not chunk:
                    break
                buffer.extend(chunk)
                if len(buffer) > limit:
                    raise HttpStoreError("payload_too_large", "请求体超过允许上限", status=413)
            if expected is not None and len(buffer) != expected:
                raise HttpStoreError("length_mismatch", "实际请求体长度与 Content-Length 不一致", status=400)
            return bytes(buffer)

    # ---------------- 六个 route ----------------

    async def _create_upload(self, request):
        token = self._token(request)
        self._reject_encoding(request)
        create_key = request.headers.get("Idempotency-Key", "")
        if not create_key:
            raise HttpStoreError("idempotency_key_required", "缺少 Idempotency-Key", status=400)
        content_type = (request.headers.get("Content-Type") or "application/json").split(";")[0].strip()
        if content_type != "application/json":
            raise HttpStoreError("unsupported_media_type", "创建上传只接受 application/json", status=415)
        length = self._content_length(request)
        if length > MAX_JSON_BYTES:
            raise HttpStoreError("payload_too_large", "小 JSON 超过 16 KiB", status=413)
        raw = await self._read_body(request, MAX_JSON_BYTES, length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpStoreError("invalid_json", "请求体不是有效 JSON", status=400) from exc
        if not isinstance(payload, dict):
            raise HttpStoreError("invalid_json", "请求体必须是 JSON 对象", status=400)
        validate_identity(payload.get("size_bytes"), payload.get("sha256"))
        existing_key_seen = await self._worker.run(
            lambda: self._store.conn.execute(
                "SELECT 1 FROM uploads WHERE create_key=?", (create_key,)
            ).fetchone() is not None
        )
        record = await self._worker.run(
            self._store.create_upload,
            size_bytes=payload["size_bytes"],
            sha256=payload["sha256"],
            options=payload.get("options") or {},
            token=token,
            create_key=create_key,
        )
        status = 200 if existing_key_seen else 201
        return self._json(status, record.public(), headers={"X-Request-Id": request["request_id"]})

    async def _get_upload(self, request):
        token = self._token(request)
        record = await self._worker.run(self._store.get_upload, request.match_info["upload_id"], token)
        return self._json(200, record.public(), headers={"X-Request-Id": request["request_id"]})

    async def _patch_upload(self, request):
        token = self._token(request)
        self._reject_encoding(request)
        content_type = request.headers.get("Content-Type")
        if content_type is not None:
            media = content_type.split(";")[0].strip()
            if media != "application/octet-stream":
                raise HttpStoreError("unsupported_media_type", "PATCH 只接受原始二进制请求体", status=415)
        raw_offset = request.headers.get("Upload-Offset")
        if raw_offset is None:
            raise HttpStoreError("offset_required", "缺少 Upload-Offset", status=400)
        try:
            offset = int(raw_offset)
        except ValueError as exc:
            raise HttpStoreError("offset_required", "Upload-Offset 必须是整数", status=400) from exc
        length = self._content_length(request)
        if length > MAX_CHUNK_BYTES:
            raise HttpStoreError("payload_too_large", "单次 PATCH 超过 1 MiB", status=413)
        data = await self._read_body(request, MAX_CHUNK_BYTES, length)
        new_offset = await self._worker.run(
            self._store.append_bytes, request.match_info["upload_id"], token, offset, data
        )
        return self._web.Response(
            status=204, headers={"Upload-Offset": str(new_offset), "X-Request-Id": request["request_id"]}
        )

    async def _commit_upload(self, request):
        token = self._token(request)
        self._reject_encoding(request)
        length = self._content_length(request)
        await self._read_body(request, MAX_CHUNK_BYTES, length)
        existed = await self._worker.run(
            lambda: self._store.conn.execute(
                "SELECT state FROM uploads WHERE upload_id=?", (request.match_info["upload_id"],)
            ).fetchone()
        )
        job = await self._worker.run(self._store.commit_upload, request.match_info["upload_id"], token)
        status = 200 if existed is not None and existed["state"] == "COMMITTED" else 202
        return self._json(status, job.public(), headers={"X-Request-Id": request["request_id"]})

    async def _get_job(self, request):
        token = self._token(request)
        job = await self._worker.run(self._store.job_record, request.match_info["job_id"], token)
        return self._json(200, job.public(), headers={"X-Request-Id": request["request_id"]})

    async def _get_result(self, request):
        token = self._token(request)
        result = await self._worker.run(self._store.get_result, request.match_info["job_id"], token)
        return self._json(200, result, headers={"X-Request-Id": request["request_id"]})