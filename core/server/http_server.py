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
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

from config_server import ServerConfig
from core.server.http_file_runner import RunnerUnavailable
from core.server.state import count_active_tasks
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

    def __init__(self, on_failure: Callable[[BaseException], None], mailbox: int = IO_MAILBOX):
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="http-io")
        self._mailbox = asyncio.Semaphore(mailbox)
        self._pending: set = set()
        self._closed = False
        self._on_failure = on_failure

    def run_sync(self, func, *args, timeout: float = 30.0):
        """在 I/O 线程里同步执行（装配/收尾用）：SQLite 连接只在那个线程创建与使用。"""
        if self._closed:
            raise HttpServerError("HTTP I/O worker 已关闭")
        return self._executor.submit(func, *args).result(timeout=timeout)

    async def run(self, func, *args, **kwargs):
        """把同步 store 操作交给 I/O worker；route 被取消时底层 I/O 仍会跑完。"""
        if self._closed:
            raise HttpServerError("HTTP I/O worker 已关闭")
        if self._mailbox.locked():
            raise HttpStoreError("io_overloaded", "HTTP I/O 操作名额已满", status=429)
        await self._mailbox.acquire()
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executor, functools.partial(func, *args, **kwargs))
        task = asyncio.ensure_future(future)
        self._pending.add(task)
        task.add_done_callback(self._on_done)
        # 等待方取消不代表线程里的 future 已完成，名额由 done callback 归还。
        return await asyncio.shield(task)

    def _on_done(self, task: asyncio.Future) -> None:
        self._pending.discard(task)
        self._mailbox.release()
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None and not isinstance(exc, HttpStoreError):
            # route 可能已取消等待方；未知线程异常仍进入 listener 的监督链。
            self._on_failure(exc)

    def close(self) -> None:
        """关闭时等待在途 I/O 结束（已在跑的字节写入不会被半途抛弃）。"""
        self._closed = True
        self._executor.shutdown(wait=True)


class HttpServer:
    """aiohttp listener + I/O worker + 监督。"""

    def __init__(self, app, addr: str, port: int, data_dir: Path):
        # 共享预算计数需要读 app.state.tasks（与 ws_recv 的 overloaded 判定同一份
        # 内存状态）；store 层只有 SQLite，跨层注入沿用 inference_ready 的锚点
        self._app = app
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
        self._fatal_event = asyncio.Event()
        self._client_payload_error = None
        # 真实推理协调者是否已装配（M3 注入实现）；默认 False → commit 明确 503
        self.inference_available = False
        # 已装配的文件 runner（E3）；为 None 时 commit 仍只看 inference_available
        self.file_runner = None
        self._bound_port: Optional[int] = None

    # ---------------- 装配 ----------------

    def prepare(self) -> "HttpServer":
        """在事件循环外完成装配：aiohttp 导入、数据目录、schema、独占锁。失败即抛。"""
        self._web = _import_aiohttp()
        from aiohttp import ClientPayloadError

        self._client_payload_error = ClientPayloadError
        self._worker = HttpIoWorker(self._mark_fatal)
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
        # 半开 body 超时后必须能写完 408 再结束连接：lingering drain 会把未完成
        # request 再挂 10 秒，这里用框架自带 lingering_time=0，不另做 timer。
        self._runner = self._web.AppRunner(application, access_log=None, lingering_time=0)
        return self

    def _open_store(self) -> HttpStore:
        return HttpStore(self.data_dir, inference_ready=self._inference_ready).open()

    def attach_runner(self, runner) -> None:
        """装配真实文件 runner：此后 commit 才受理并立即调度。"""
        self.file_runner = runner
        self.inference_available = True

    def report_fatal(self, exc: BaseException) -> None:
        """后台未知异常的唯一上抛口：停止 listener 并让进程非零退出。"""
        self._mark_fatal(exc)

    def _inference_ready(self) -> bool:
        """实际推理协调者是否可用。

        已装配 runner 时以 runner 为准（收尾后自动回到 503）；没有 runner 时
        沿用 E2 的开关（默认 False → commit 明确 503，而不是受理后永远排队）。
        """
        if self.file_runner is not None:
            return self.file_runner.available
        return bool(self.inference_available)

    # ---------------- runner 所需的受监督 I/O 入口 ----------------

    async def job_source(self, job_id: str) -> dict:
        """源文件路径与受理参数；只在 I/O worker 里读 SQLite。"""
        return await self._worker.run(self._store.job_source, job_id)

    async def record_result(self, job_id: str, payload: dict) -> None:
        await self._worker.run(self._store.record_result, job_id, payload)

    async def mark_running(self, job_id: str) -> bool:
        # 开始解码前的 QUEUED -> RUNNING 转移；已在运行或已终态返回 False
        return await self._worker.run(self._store.mark_running, job_id)

    async def fail_job(self, job_id: str, error_code: str) -> bool:
        return await self._worker.run(self._store.fail_job, job_id, error_code)

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
            await self._fatal_event.wait()
            if self.fatal is not None:
                raise self.fatal
        finally:
            await self.stop()

    async def stop(self) -> None:
        # 先停 runner：Job 调度与解码进程必须在 I/O worker 与存储关闭前收尾
        if self.file_runner is not None:
            await self.file_runner.stop()
            self.file_runner = None
            self.inference_available = False
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
            request_id = uuid.uuid4().hex
            request["request_id"] = request_id
            if self._handler_slots.locked():
                return self._error_response(429, "server_busy", "HTTP handler 名额已满", request_id)
            await self._handler_slots.acquire()
            try:
                try:
                    return await handler(request)
                except HttpStoreError as exc:
                    return self._error_response(
                        exc.status, exc.code, exc.message, request_id,
                        exc.confirmed_offset, exc.error_code,
                    )
                except asyncio.CancelledError:
                    # route 取消只结束等待方，底层 I/O 由 HttpIoWorker 跑完
                    raise
                except (ConnectionError, asyncio.IncompleteReadError, self._client_payload_error):
                    # 客户端断开或请求体不完整不影响已确认前缀，也不属于服务端 fatal。
                    return self._error_response(400, "incomplete_request", "请求未完整送达", request_id)
                except Exception as exc:  # 未知错误：显式失败并让监督看到，不静默成功
                    self._mark_fatal(exc)
                    return self._error_response(500, "internal_error", "服务端内部错误", request_id)
            finally:
                self._handler_slots.release()

        return wrapped

    def _mark_fatal(self, exc: BaseException) -> None:
        if self.fatal is None:
            self.fatal = exc
            logger.error("HTTP 未知 operation 失败，listener 将停止：%s", exc)
        self._fatal_event.set()

    def _error_response(self, status, code, message, request_id, confirmed_offset=None,
                        error_code=None):
        payload = {"code": code, "message": message, "request_id": request_id}
        if confirmed_offset is not None:
            payload["confirmed_offset"] = confirmed_offset
        if error_code is not None:
            # 终态失败必须带上已持久化的 Job error_code
            payload["error_code"] = error_code
        response = self._json(status, payload)
        if status == 408:
            response.force_close()
        return response

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
        try:
            token.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise HttpStoreError("unauthorized", "Bearer 凭据必须可被 UTF-8 表示", status=401) from exc
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

    @staticmethod
    def _read_idle_seconds() -> float:
        """每次等待下一块请求体的空闲期限：复用既有 CW_UPLOAD_IDLE_SECONDS。"""
        return float(ServerConfig.upload_idle_seconds)

    async def _read_body(self, request, limit: int, expected: Optional[int] = None) -> bytes:
        """按 64 KiB 有界读取，绝不把整份 1 GiB 上传 append 进内存。"""
        if self._body_slots.locked():
            raise HttpStoreError("body_overloaded", "HTTP 请求体名额已满", status=429)
        await self._body_slots.acquire()
        try:
            idle = self._read_idle_seconds()
            buffer = bytearray()
            while True:
                try:
                    chunk = await asyncio.wait_for(request.content.read(READ_CHUNK_BYTES), timeout=idle)
                except asyncio.TimeoutError as exc:
                    raise HttpStoreError(
                        "request_timeout",
                        "读取请求体超时：超过空闲等待期限未收到新数据",
                        status=408,
                    ) from exc
                if not chunk:
                    break
                buffer.extend(chunk)
                if len(buffer) > limit:
                    raise HttpStoreError("payload_too_large", "请求体超过允许上限", status=413)
            if expected is not None and len(buffer) != expected:
                raise HttpStoreError("length_mismatch", "实际请求体长度与 Content-Length 不一致", status=400)
            return bytes(buffer)
        finally:
            self._body_slots.release()

    # ---------------- 六个 route ----------------

    async def _create_upload(self, request):
        token = self._token(request)
        self._reject_encoding(request)
        create_key = request.headers.get("Idempotency-Key", "")
        if not create_key:
            raise HttpStoreError("idempotency_key_required", "缺少 Idempotency-Key", status=400)
        try:
            create_key.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise HttpStoreError("invalid_request", "Idempotency-Key 必须可被 UTF-8 表示", status=400) from exc
        content_type = (request.headers.get("Content-Type") or "application/json").split(";")[0].strip()
        if content_type != "application/json":
            raise HttpStoreError("unsupported_media_type", "创建上传只接受 application/json", status=415)
        length = self._content_length(request)
        if length > MAX_JSON_BYTES:
            raise HttpStoreError("payload_too_large", "小 JSON 超过 16 KiB", status=413)
        raw = await self._read_body(request, MAX_JSON_BYTES, length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError) as exc:
            # 有界输入下 json.loads 表达式本身的已知输入错误（解码/语法/整数字面量位数/纯 Python
            # 扫描器嵌套深度）；其余未知异常仍走 _wrap 的 fail-fast，不扩大捕获范围
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
            options={} if payload.get("options") is None else payload["options"],
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
        # R7：HTTP 准入与 WS 共用 max_tasks 的总量预算。计数走唯一原语
        # count_active_tasks（活动 = state.tasks 非终态，HTTP 与 WS 一并计入）；
        # HTTP 自己的表内计数由 store._check_job_admission 在事务里兜底。
        # 与 WS 判定同为内存侧建议性计数（TOCTOU 窗口相同），不发明新错误码。
        if count_active_tasks(self._app.state) >= ServerConfig.max_tasks:
            raise HttpStoreError(
                "too_many_jobs",
                f"服务端活动任务已达共享上限 {ServerConfig.max_tasks}",
                status=429,
            )
        length = self._content_length(request)
        await self._read_body(request, MAX_CHUNK_BYTES, length)
        existed = await self._worker.run(
            lambda: self._store.conn.execute(
                "SELECT state FROM uploads WHERE upload_id=?", (request.match_info["upload_id"],)
            ).fetchone()
        )
        job = await self._worker.run(self._store.commit_upload, request.match_info["upload_id"], token)
        already_committed = existed is not None and existed["state"] == "COMMITTED"
        if self.file_runner is not None and not already_committed:
            # 只在首次受理时调度：重复 commit 返回同一 Job，不重投、不自动重跑
            try:
                self.file_runner.submit(job.job_id)
            except RunnerUnavailable as exc:
                # 已经受理但此刻不可调度：立即可靠 FAILED，绝不留下永远排队的 Job
                await self.file_runner.fail_job(job.job_id, "inference_unavailable", str(exc))
                raise HttpStoreError("inference_unavailable", str(exc), status=503)
        status = 200 if already_committed else 202
        return self._json(status, job.public(), headers={"X-Request-Id": request["request_id"]})

    async def _get_job(self, request):
        token = self._token(request)
        job = await self._worker.run(self._store.job_record, request.match_info["job_id"], token)
        return self._json(200, job.public(), headers={"X-Request-Id": request["request_id"]})

    async def _get_result(self, request):
        token = self._token(request)
        result = await self._worker.run(self._store.get_result, request.match_info["job_id"], token)
        return self._json(200, result, headers={"X-Request-Id": request["request_id"]})
