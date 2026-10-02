# coding: utf-8
"""
HTTP 文件任务 runner（E3）

把 E1/E2 已受理（commit 后持久化）的 HTTP Job 接进**真实识别链**：

  * 源文件由服务端自己生成的 UUID 路径决定，用固定 exec argv 的 ffmpeg 有界流式
    解码为 16kHz/mono/f32le PCM：无 shell、无整文件 PCM 内存、无临时 PCM 文件、
    不读任意路径、不外联；
  * 复用 WS 同一份 PcmSegmenter 与「每任务 4 段在途」预算，把段投进现有多进程队列
    （owner_kind=http、socket_id 为空、task_id=job_id）；
  * worker 回父进程的每一条 Result 由 HttpResultSink 消费：中间段只做有界检查，
    最终段用真实 RecognitionMessage 字段整份持久化，并与 DONE 同一次事务提交；
  * 活跃 owner 在首段入队前登记，槽位/owner/源引用只在结果持久化或 FAILED 可靠提交
    之后释放。

失败语义：任一段解码/提交/结果失败即整 Job FAILED（先可靠提交，再停后续段）；
迟到结果不覆盖已终态 Job；未知后台异常先可靠提交 FAILED 再上抛到 listener 监督链
（fail-loud），绝不被吞掉。正常收尾只取消并回收解码进程，不把任务改写成 FAILED ——
重启时 QUEUED/RUNNING 由存储收敛为 FAILED[server_restarted]，不做自动重跑。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from typing import Optional

from config_server import ServerConfig as Config
from core.constants import AudioFormat
from core.server.http_store import MAX_RESULT_BYTES, HttpStoreError
from core.server.schema import Result, Task
from core.server.segmenter import PcmSegmenter
from core.server.state import (
    TaskKey,
    begin_task,
    ensure_server_runtime,
    make_task_key,
    register_http_job,
    register_segment_submission,
    release_terminal_task,
    transition_terminal,
)
from . import logger
from . import segmenter as shared_segmenter
from .connection.segmenter import get_cut_finder

# ffmpeg 解码读取块（有界；不按压缩体积估算内存与时长）
DECODE_READ_BYTES = 64 * 1024
# 真实样本数上限：与 WS 的 CW_MAX_TASK_SECONDS 同一口径（默认 4 小时）
MAX_TASK_SECONDS = float(os.environ.get("CW_MAX_TASK_SECONDS", "14400"))
# 解码收尾（kill + 子任务回收）的有界期限
DECODER_CLOSE_TIMEOUT = 10.0
# runner 收尾（取消所有在跑 Job）的有界期限
RUNNER_STOP_TIMEOUT = 20.0
# ffmpeg stderr 只保留末尾若干字节用于失败诊断
STDERR_TAIL_BYTES = 500


class JobFailure(Exception):
    """已定义的单个 Job 失败（解码/参数/提交语义），一律整 Job FAILED。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class RunnerUnavailable(Exception):
    """runner 此刻不可调度：commit 必须显式 503，绝不受理后永远排队。"""


def ffmpeg_path() -> Optional[str]:
    return shutil.which("ffmpeg")


class FileSourceDecoder:
    """对可 seek 的源文件执行固定 argv 的 ffmpeg 解码，输出有界 f32le PCM 块。

    argv 是常量列表：输入是服务端自己生成的源文件路径，输出只走管道，
    既不经过 shell，也不把整段 PCM 收进内存或落临时文件。
    """

    def __init__(self, path: Path, read_bytes: int = DECODE_READ_BYTES):
        self.path = Path(path)
        self.read_bytes = read_bytes
        self.samples_emitted = 0
        self.process: Optional[asyncio.subprocess.Process] = None
        self._stderr_tail = bytearray()
        self._stderr_task: Optional[asyncio.Task] = None
        self._finished = False

    def build_argv(self) -> list[str]:
        binary = ffmpeg_path()
        if binary is None:
            raise JobFailure("unsupported_encoding", "本机 PATH 中没有 ffmpeg")
        return [
            binary, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-i", str(self.path),
            "-ar", "16000", "-ac", "1", "-f", "f32le", "pipe:1",
        ]

    async def start(self) -> "FileSourceDecoder":
        argv = self.build_argv()
        try:
            self.process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise JobFailure("unsupported_encoding", "本机 PATH 中没有 ffmpeg") from exc
        except OSError as exc:
            raise JobFailure("decode_failed", f"无法启动 ffmpeg 解码源文件：{exc}") from exc
        self._stderr_task = asyncio.create_task(self._read_stderr())
        return self

    async def _read_stderr(self) -> None:
        while data := await self.process.stderr.read(4096):
            self._stderr_tail.extend(data)
            del self._stderr_tail[:-STDERR_TAIL_BYTES]

    def _stderr_detail(self) -> str:
        tail = bytes(self._stderr_tail).decode("utf-8", errors="replace").strip()
        return f"；ffmpeg stderr 末尾：{tail}" if tail else ""

    async def pcm_chunks(self):
        """按 64 KiB 有界读取并按 4 字节对齐输出；不累积整段 PCM。"""
        pending = bytearray()
        while True:
            data = await self.process.stdout.read(self.read_bytes)
            if not data:
                break
            pending.extend(data)
            aligned = len(pending) // AudioFormat.BYTES_PER_SAMPLE * AudioFormat.BYTES_PER_SAMPLE
            if aligned:
                block = bytes(pending[:aligned])
                del pending[:aligned]
                self.samples_emitted += len(block) // AudioFormat.BYTES_PER_SAMPLE
                yield block
        if pending:
            raise JobFailure(
                "decode_failed",
                "ffmpeg 输出的 f32le 数据未按 4 字节对齐" + self._stderr_detail(),
            )

    async def finish(self) -> None:
        """读尽输出后确认 ffmpeg 正常退出；非零退出即整 Job 失败。"""
        if self._finished:
            return
        self._finished = True
        returncode = await self.process.wait()
        await self._stderr_task
        if returncode != 0:
            raise JobFailure(
                "decode_failed",
                f"ffmpeg 解码源文件退出码 {returncode}" + self._stderr_detail(),
            )

    def kill(self) -> None:
        """同步杀掉解码进程：即使协程正在被取消，ffmpeg 也一定被回收。"""
        process = self.process
        if process is not None and process.returncode is None:
            process.kill()

    async def close(self) -> None:
        self.kill()
        tasks = [task for task in (self._stderr_task,) if task is not None]
        if self.process is not None and self.process.returncode is None:
            try:
                await asyncio.wait_for(self.process.wait(), timeout=DECODER_CLOSE_TIMEOUT)
            except asyncio.TimeoutError:  # pragma: no cover - kill 后仍不退出的兜底
                logger.error("ffmpeg 解码进程在 kill 后仍未退出")
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def __aenter__(self) -> "FileSourceDecoder":
        return await self.start()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()


class HttpResultSink:
    """父进程结果消费者：中间段只做有界检查，最终段整份持久化。"""

    def __init__(self, state, http, runner: "HttpFileRunner"):
        self.state = state
        self.http = http
        self.runner = runner

    async def __call__(self, result: Result) -> None:
        key = make_task_key("http", result.task_id)
        record = self.state.tasks.get(key)
        if record is None or record.status in {"DONE", "FAILED"}:
            # 迟到结果：已终态或已释放，绝不覆盖已提交的 DONE/FAILED
            logger.debug(f"丢弃终态或无主 HTTP 迟到结果 task={result.task_id}")
            return
        if result.error_code:
            await self._fail(result.task_id, result.error_code, result.error_message)
            return
        try:
            payload = http_result_payload(result)
            # 每条结果都查上限：不是先无限累计、末尾才查一次
            if len(dumps_result(payload)) > MAX_RESULT_BYTES:
                raise HttpStoreError(
                    "result_too_large", "识别结果超过持久化上限", status=507
                )
        except HttpStoreError as exc:
            await self._fail(result.task_id, exc.code, exc.message)
            return
        except BaseException as exc:
            await self._fail(
                result.task_id, "internal", f"{type(exc).__name__}: {exc}"
            )
            raise
        if not result.is_final:
            logger.debug(f"已核对 HTTP 中间段结果 task={result.task_id}")
            return
        try:
            await self.http.record_result(result.task_id, payload)
        except HttpStoreError as exc:
            await self._fail(result.task_id, exc.code, exc.message)
            return
        logger.info(
            f"HTTP 文件任务结果已持久化 task={result.task_id} "
            f"tokens={len(result.tokens)} 时长={result.duration:.2f}s"
        )

    async def _fail(self, job_id: str, code: str, message: str) -> None:
        await self.runner.fail_job(job_id, code, message)


def http_result_payload(result: Result) -> dict:
    """持久结果的唯一来源：pipeline 累计出的最终 Result 真实字段。"""
    return {
        "task_id": result.task_id,
        "type": result.type,
        "socket_id": result.socket_id,
        "owner_kind": result.owner_kind,
        "is_final": result.is_final,
        "duration": result.duration,
        "time_start": result.time_start,
        "time_submit": result.time_submit,
        "time_complete": result.time_complete,
        "text": result.text,
        "text_accu": result.text_accu,
        "tokens": list(result.tokens),
        "timestamps": list(result.timestamps),
    }


def dumps_result(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _check_samples_limit(job_id: str, samples: int) -> None:
    """实际解码样本数封顶（不靠压缩体积估算时长）。"""
    if samples > MAX_TASK_SECONDS * AudioFormat.SAMPLE_RATE:
        raise JobFailure(
            "audio_too_long",
            f"任务 {job_id} 解码样本数 {samples} 超过时长上限 {MAX_TASK_SECONDS:g}s",
        )


class HttpFileRunner:
    """已受理 HTTP Job 的调度者：解码 → 分段 → 入队 → 结果持久化。"""

    def __init__(self, state, http):
        self.state = state
        self.http = http
        self._jobs: dict[str, asyncio.Task] = {}
        self._stopped = False
        if ffmpeg_path() is None:
            raise RunnerUnavailable(
                "显式启用 HTTP 文件任务需要本机 PATH 中存在 ffmpeg"
            )

    @property
    def result_sink(self) -> HttpResultSink:
        return HttpResultSink(self.state, self.http, self)

    @property
    def available(self) -> bool:
        return not self._stopped and ffmpeg_path() is not None

    @property
    def active_jobs(self) -> list[str]:
        return sorted(self._jobs)

    # ---------------- 受理与调度 ----------------

    def submit(self, job_id: str) -> asyncio.Task:
        """把已受理的 Job 交给后台执行；同一 Job 重复 commit 不重投。"""
        existing = self._jobs.get(job_id)
        if existing is not None and not existing.done():
            return existing
        if not self.available:
            raise RunnerUnavailable("HTTP 文件 runner 当前不可调度")
        task = asyncio.ensure_future(self._run_job(job_id))
        self._jobs[job_id] = task
        task.add_done_callback(lambda done, jid=job_id: self._on_done(done, jid))
        return task

    def _on_done(self, task: asyncio.Task, job_id: str) -> None:
        self._jobs.pop(job_id, None)
        if task.cancelled():
            logger.info(f"HTTP 文件任务随收尾取消 job={job_id}")
            return
        error = task.exception()
        if error is None:
            logger.info(f"HTTP 文件任务调度结束 job={job_id}")
            return
        # 未知后台异常已在 _run_job 里可靠提交过 FAILED，这里继续上抛到监督链
        logger.error(f"HTTP 文件任务后台异常 job={job_id}：{error!r}")
        self.http.report_fatal(error)

    async def _run_job(self, job_id: str) -> None:
        try:
            await self._execute(job_id)
        except asyncio.CancelledError:
            raise
        except (JobFailure, HttpStoreError) as exc:
            await self.fail_job(job_id, exc.code, exc.message)
        except BaseException as exc:
            await self.fail_job(job_id, "internal", f"{type(exc).__name__}: {exc}")
            raise

    async def fail_job(self, job_id: str, code: str, message: str) -> bool:
        """可靠提交整 Job FAILED，然后释放运行态；已是终态时不覆盖。"""
        ensure_server_runtime(self.state)
        logger.error(f"HTTP 文件任务失败 job={job_id} code={code}：{message}")
        committed = await self.http.fail_job(job_id, code)
        key = make_task_key("http", job_id)
        if committed:
            transition_terminal(self.state, key, "FAILED", code=code)
        else:
            logger.info(f"HTTP 文件任务已终态，未覆盖失败状态 job={job_id}")
        release_terminal_task(self.state, key)
        return committed

    def cancel(self, job_id: str) -> None:
        task = self._jobs.get(job_id)
        if task is not None and not task.done():
            task.cancel()

    # ---------------- 单个 Job 的执行 ----------------

    def _validate_options(self, options: dict) -> tuple[float, float]:
        model = options.get("model")
        if model is not None and model != Config.model_type:
            raise JobFailure("bad_request", f"请求模型 {model!r} 与服务端模型 {Config.model_type!r} 不符")
        nominal = float(options.get("seg_duration") or 15.0)
        overlap = float(options.get("seg_overlap") or 0.0)
        shared_segmenter.validate_segment_params(nominal, overlap)
        return nominal, overlap

    def _new_segmenter(self) -> PcmSegmenter:
        segmenter = PcmSegmenter()
        segmenter.configure(
            cut_finder=get_cut_finder() if Config.seg_cut_snap else None,
            engine_segment_limit=shared_segmenter.engine_segment_limit(),
            cut_snap=Config.seg_cut_snap,
            search_before=Config.seg_search_before,
            search_after=Config.seg_search_after,
            max_cut=Config.seg_max_cut,
        )
        return segmenter

    async def _execute(self, job_id: str) -> None:
        state = self.state
        descriptor = await self.http.job_source(job_id)
        options = descriptor["options"]
        nominal, overlap = self._validate_options(options)
        time_start = float(descriptor["time_submit"])
        key = make_task_key("http", job_id)
        # 先可靠登记活跃 owner，再投第一段：worker 侧才不会把段当无主丢弃
        register_http_job(state, job_id)
        begin_task(state, key, Config.max_inflight_segments)
        record = state.tasks[key]
        context = options.get("context") or ""
        language = options.get("language") or "auto"
        segmenter = self._new_segmenter()
        decoder = await FileSourceDecoder(descriptor["path"]).start()
        try:
            async for pcm in decoder.pcm_chunks():
                record.samples_total = decoder.samples_emitted
                _check_samples_limit(job_id, decoder.samples_emitted)
                segmenter.append(pcm)
                segments = await segmenter.drain_ready(
                    source="file", nominal=nominal, overlap=overlap
                )
                for segment in segments:
                    if not await self._submit(
                        key, record, segment, job_id, time_start, context, language
                    ):
                        return
            await decoder.finish()
            segments = await segmenter.drain_ready(
                source="file", nominal=nominal, overlap=overlap, is_final=True
            )
            for segment in segments:
                if not await self._submit(
                    key, record, segment, job_id, time_start, context, language
                ):
                    return
            if not await self._submit(
                key, record, segmenter.final_segment(overlap), job_id,
                time_start, context, language,
            ):
                return
            # 末段入队后仍不释放：槽位/owner/源引用由终态（持久化或可靠 FAILED）释放
            await record.terminal_event.wait()
        finally:
            decoder.kill()
            await decoder.close()
            release_terminal_task(self.state, key)

    @staticmethod
    def _active(record) -> bool:
        return record.status not in {"DONE", "FAILED"}

    async def _submit(
        self,
        key: TaskKey,
        record,
        segment,
        job_id: str,
        time_start: float,
        context: str,
        language: str,
    ) -> bool:
        """按「每任务 4 段在途」预算把真实段投进现有多进程队列。

        返回 False 表示任务已终态：调用方必须停止投后续段。
        """
        if not await self._acquire_slot(record):
            logger.info(f"HTTP 文件任务已终态，停止提交后续段 job={job_id}")
            return False
        self.state.queue_in.put(Task(
            type="file",
            data=segment.data,
            offset=segment.offset,
            task_id=key[2],
            socket_id="",
            overlap=segment.overlap,
            is_final=segment.is_final,
            time_start=time_start,
            time_submit=time.time(),
            context=context,
            language=language,
            owner_kind="http",
        ))
        record.segments += 1
        register_segment_submission(self.state, key, time.monotonic())
        logger.debug(
            f"提交 HTTP 文件片段 job={job_id} 偏移={segment.offset:.2f}s "
            f"字节={len(segment.data)} final={segment.is_final}"
        )
        return True

    async def _acquire_slot(self, record) -> bool:
        """等待本任务的结果名额；终态会立刻取消等待。"""
        if not self._active(record) or record.segment_slots is None:
            return False
        semaphore = record.segment_slots
        acquire = asyncio.create_task(semaphore.acquire())
        terminal = asyncio.create_task(record.terminal_event.wait())
        try:
            done, _ = await asyncio.wait(
                (acquire, terminal), return_when=asyncio.FIRST_COMPLETED
            )
            if terminal in done or not self._active(record):
                if acquire.done() and not acquire.cancelled() and acquire.result():
                    semaphore.release()
                return False
            return True
        finally:
            for waiter in (acquire, terminal):
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(acquire, terminal, return_exceptions=True)

    # ---------------- 收尾 ----------------

    async def stop(self) -> None:
        """有界收尾：取消在跑 Job、解码进程被回收；不改写任务状态。"""
        self._stopped = True
        tasks = [task for task in self._jobs.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=RUNNER_STOP_TIMEOUT,
                )
            except asyncio.TimeoutError:  # pragma: no cover - 收尾超时的最后兜底
                logger.error("HTTP 文件 runner 收尾超时，仍有 Job 未结束")
        self._jobs.clear()
        logger.info("HTTP 文件 runner 已停止")


__all__ = [
    "FileSourceDecoder",
    "HttpFileRunner",
    "HttpResultSink",
    "JobFailure",
    "RunnerUnavailable",
    "ffmpeg_path",
    "http_result_payload",
]