# coding: utf-8
"""HTTP 十二组边界 QA 的剩余缺口。

本文件只补 C2（base 5134720）之后仍然没有真实 producer 证明的五条不变式，
逐条对应 `docs/sessions/261003-http-completion/m6-qa-evidence.md` 覆盖表里标出的缺口：

  * 组 1：同一次真实 SDK 上传的请求字节与服务端落盘长度/SHA 端到端对照；
  * 组 3：commit 响应真的发出又被丢掉之后，显式恢复且识别恰好 1 次；
  * 组 7：真 WS 识别与真 HTTP 识别同时经过同一个真 worker Queue/Result Queue；
  * 组 10：44.1 kHz 立体声 / 8 kHz 单声道源经真 ffmpeg 后的有界 16 k mono f32 段；
  * 组 11：`is_final` 那次解码失败时整任务失败，不发布缺段成功。

复用 `tests/test_http_file_runner.py` 已入库的真实服务骨架（真 HTTP listener、真 runner、
真 ffmpeg、真识别子进程），不另造框架；所有网络端口都是 port 0，数据目录都是 tmp_path。
"""
from __future__ import annotations

import asyncio
import base64
import functools
import json
import shutil
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
import websockets

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "sdk"))

from capswriter_asr import (  # noqa: E402
    get_file_job_http,
    get_file_result_http,
    resume_file_http,
    submit_file_http,
)
from capswriter_asr import http_client as sdk_http  # noqa: E402

from core.protocol import AudioMessage  # noqa: E402
from core.server.connection.ws_recv import ws_recv  # noqa: E402
from tests.test_http_file_runner import (  # noqa: E402
    FAKE_ENGINE,
    decoded_sample_count,
    install_recording_ffmpeg,
    make_container,
    raw_get,
    read_ffmpeg_starts,
    running_runner_server,
    submit,
    wait_terminal,
)

FFMPEG = shutil.which("ffmpeg")

pytest.importorskip("aiohttp", reason="未安装 aiohttp==3.14.3；HTTP 入口默认关闭")

WS_SAMPLE_RATE = 16000


def ws_tone(seconds: float) -> np.ndarray:
    """非静音的确定性 float32 单声道采样（真实 WS producer 输入）。"""
    count = round(seconds * WS_SAMPLE_RATE)
    t = np.arange(count, dtype=np.float64) / WS_SAMPLE_RATE
    return (0.2 * np.sin(2 * np.pi * 220 * t) * np.sin(2 * np.pi * 0.7 * t)).astype("<f4")


def ws_frame(samples: np.ndarray, task_id: str, *, is_final: bool, seg_duration=5.0):
    return AudioMessage(
        data=base64.b64encode(samples.astype("<f4", copy=False).tobytes()).decode("ascii"),
        is_final=is_final,
        task_id=task_id,
        source="mic",
        time_start=0.0,
        seg_duration=seg_duration,
        seg_overlap=0.0,
        context="",
        language="auto",
    ).to_json()


async def start_ws_server(state):
    """在同一份 state 上再挂一个真实 ws_recv（port 0），与 HTTP listener 共存。"""
    app = SimpleNamespace(state=state)
    server = await websockets.serve(
        functools.partial(ws_recv, app=app), "127.0.0.1", 0,
        max_size=None, ping_interval=None,
    )
    return server, f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"


async def wait_until(predicate, what: str, timeout: float = 30.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"等待「{what}」超时 ({timeout}s)")
        await asyncio.sleep(0.005)


def ws_task_records(harness, task_id: str) -> list[dict]:
    return [
        entry for entry in harness.received
        if entry.get("event") == "task" and entry.get("task_id") == task_id
    ]


@pytest.mark.asyncio
async def test_real_ws_and_http_share_one_worker_without_key_pollution(tmp_path):
    """组 7：真 WS 与真 HTTP 同时经过同一个真 worker，key 不互相污染。

    覆盖三件事，缺一不可：
      1. 同一次运行里识别子进程同时收到 `owner_kind="ws"`（带真实 socket_id）和
         `owner_kind="http"`（socket_id 为空）的段，两类 key 互不串；
      2. 只连不发帧的空 socket 存在期间，HTTP 文件任务照常跑到 DONE；
      3. 断开的 WS 任务从 state.tasks / connection_tasks 消失、worker 不再收到它的段，
         同期的 HTTP 任务不受影响。
    """
    source = make_container(tmp_path, "speech.mp3", seconds=20.0)
    samples = ws_tone(20.0)

    async with running_runner_server(tmp_path) as harness:
        ws_server, ws_url = await start_ws_server(harness.state)
        try:
            # ---- 1. 空 socket 存在期间 HTTP 照常 ------------------------------
            idle = await websockets.connect(ws_url, max_size=None, ping_interval=None)
            assert idle.state.name == "OPEN"
            recovery_a = tmp_path / "resume_a.json"
            handle_a = await submit(harness, source, recovery_a, seg_duration=5.0, seg_overlap=1.0)
            assert (await wait_terminal(harness, recovery_a)).state == "DONE"
            assert idle.state.name == "OPEN", "空 WS socket 不应因 HTTP 任务而断开"
            # 空 socket 没有发过首帧，因此没有任何 ws 任务 key
            assert all(key[0] != "ws" for key in harness.state.tasks), harness.state.tasks

            # ---- 2. WS 慢速推流期间提交 HTTP，两者同时在跑 ---------------------
            live = await websockets.connect(ws_url, max_size=None, ping_interval=None)
            stop_stream = asyncio.Event()
            messages: list[dict] = []

            async def reader():
                try:
                    async for raw in live:
                        messages.append(json.loads(raw))
                except websockets.ConnectionClosed:
                    pass

            reader_task = asyncio.create_task(reader())
            chunk = round(0.5 * WS_SAMPLE_RATE)
            finished = asyncio.Event()

            async def streamer():
                for start in range(0, len(samples), chunk):
                    await live.send(ws_frame(samples[start:start + chunk], "ws-live",
                                             is_final=False))
                    await asyncio.sleep(0.05)
                await live.send(ws_frame(samples[len(samples):], "ws-live", is_final=True))
                finished.set()

            stream_task = asyncio.create_task(streamer())
            await wait_until(
                lambda: len(ws_task_records(harness, "ws-live")) >= 1,
                "识别子进程收到第一个 WS 段",
            )

            recovery_b = tmp_path / "resume_b.json"
            handle_b = await submit(harness, source, recovery_b, seg_duration=5.0, seg_overlap=1.0)
            job_b = handle_b.job_id

            # 抓「两类 key 同时存在于 state.tasks」的窗口：HTTP Job 存活期间轮询
            both_seen = False
            for _ in range(4000):
                keys = set(harness.state.tasks)
                if ("http", job_b, job_b) in keys:
                    if any(key[0] == "ws" for key in keys):
                        both_seen = True
                        break
                elif both_seen:
                    break
                await asyncio.sleep(0.005)
            assert both_seen, (
                f"没有观察到 WS 与 HTTP 任务同时在场，state.tasks={harness.state.tasks!r}"
            )

            assert (await wait_terminal(harness, recovery_b)).state == "DONE"
            await asyncio.wait_for(finished.wait(), timeout=30)
            await asyncio.wait_for(stream_task, timeout=30)
            await asyncio.sleep(0.3)

            # ---- 3. key 不污染：worker 侧收到的两类 Task 形状互斥 -------------
            ws_records = ws_task_records(harness, "ws-live")
            http_records = [
                entry for entry in harness.received
                if entry.get("event") == "task" and entry.get("owner_kind") == "http"
                and entry.get("task_id") == job_b
            ]
            assert ws_records, "识别子进程没有收到任何 WS 段"
            assert http_records, "识别子进程没有收到 HTTP 任务的段"
            assert all(item["owner_kind"] == "ws" for item in ws_records)
            assert all(item["socket_id"] for item in ws_records), "WS 段必须带真实 socket_id"
            assert len({item["socket_id"] for item in ws_records}) == 1
            assert all(item["owner_kind"] == "http" for item in http_records)
            assert all(item["socket_id"] == "" for item in http_records)
            assert job_b not in {item["task_id"] for item in ws_records}
            assert "ws-live" not in {item["task_id"] for item in http_records}

            # ---- 4. 消费侧不串：WS 只收到自己的结果，HTTP 结果只落库 ----------
            assert messages, "WS 客户端没有收到任何结果"
            assert {item["task_id"] for item in messages} == {"ws-live"}
            finals = [item for item in messages if item.get("is_final")]
            assert len(finals) == 1, messages
            assert any(item.get("type") == "error" for item in messages) is False, messages
            result_rows = harness.read_db("SELECT job_id, payload FROM results")
            assert {row["job_id"] for row in result_rows} == {handle_a.job_id, job_b}
            for row in result_rows:
                payload = json.loads(row["payload"])
                assert payload["task_id"] == row["job_id"]
                assert payload["owner_kind"] == "http"
                assert payload["socket_id"] == ""

            # ---- 5. 断开的 WS 不再被处理，HTTP 不受影响 ------------------------
            dropped = await websockets.connect(ws_url, max_size=None, ping_interval=None)

            async def drop_streamer():
                for start in range(0, len(samples), chunk):
                    await dropped.send(ws_frame(samples[start:start + chunk], "ws-drop",
                                                is_final=False))
                    await asyncio.sleep(0.05)

            drop_task = asyncio.create_task(drop_streamer())
            await wait_until(
                lambda: len(ws_task_records(harness, "ws-drop")) >= 1,
                "识别子进程收到被丢弃 WS 任务的段",
            )
            await dropped.close()
            drop_task.cancel()
            await asyncio.gather(drop_task, return_exceptions=True)
            await wait_until(
                lambda: all(key[1] != "ws-drop" for key in harness.state.tasks),
                "断开的 WS 任务从 state.tasks 移除",
            )
            assert all(
                key[2] != "ws-drop" for key in harness.state.connection_tasks.values()
            ), harness.state.connection_tasks
            assert all(
                key[2] != "ws-drop" for key in harness.state.pending_segments
            ), harness.state.pending_segments
            settled = len(ws_task_records(harness, "ws-drop"))
            await asyncio.sleep(1.5)
            assert len(ws_task_records(harness, "ws-drop")) == settled, (
                "断开的 WS 任务仍在被投递给识别子进程"
            )
            # 断 WS 之后 HTTP 入口照常
            recovery_c = tmp_path / "resume_c.json"
            handle_c = await submit(harness, source, recovery_c, seg_duration=5.0, seg_overlap=1.0)
            assert (await wait_terminal(harness, recovery_c)).state == "DONE"
            transcript_c = await get_file_result_http(harness.base_url, resume_path=recovery_c)
            assert transcript_c.raw["task_id"] == handle_c.job_id

            await live.close()
            await reader_task
            await idle.close()
            assert harness.http_server.fatal is None
        finally:
            ws_server.close()
            await asyncio.wait_for(ws_server.wait_closed(), timeout=5)




# ---------------------------------------------------------------- 组 1：真实线缆字节


class RecordingProxy:
    """端口转发代理：真实客户端字节转发给真实服务端，同时逐字节记录两个方向。

    记录的是 TCP 上真实流过的字节，不是 SDK 内部函数的入参，因此「请求体是源文件
    原字节」这条断言覆盖的是真正的发布边界。
    """

    def __init__(self, target_port: int):
        self.target_port = target_port
        self.client_chunks: list[bytes] = []
        self.server_chunks: list[bytes] = []
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> "RecordingProxy":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.sockets[0].getsockname()[1]}"

    async def _handle(self, reader, writer):
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                "127.0.0.1", self.target_port
            )
        except OSError:
            writer.close()
            return

        async def pump(source, sink, record):
            try:
                while chunk := await source.read(65536):
                    record.append(chunk)
                    sink.write(chunk)
                    await sink.drain()
            except (OSError, asyncio.IncompleteReadError):
                pass
            finally:
                try:
                    sink.write_eof()
                except (OSError, RuntimeError):
                    pass

        try:
            await asyncio.gather(
                pump(reader, upstream_writer, self.client_chunks),
                pump(upstream_reader, writer, self.server_chunks),
            )
        finally:
            for handle in (writer, upstream_writer):
                try:
                    handle.close()
                except OSError:
                    pass

    async def stop(self) -> None:
        self._server.close()
        await asyncio.wait_for(self._server.wait_closed(), timeout=5)

    def client_bytes(self) -> bytes:
        return b"".join(self.client_chunks)

    def server_bytes(self) -> bytes:
        return b"".join(self.server_chunks)

    def requests(self) -> list[dict]:
        """按 HTTP/1.1 报文解析客户端方向；收不全的尾部不算一次请求。"""
        stream = self.client_bytes()
        parsed: list[dict] = []
        offset = 0
        while True:
            head_end = stream.find(b"\r\n\r\n", offset)
            if head_end < 0:
                return parsed
            lines = stream[offset:head_end].decode("latin-1").split("\r\n")
            method, target, _version = lines[0].split(" ", 2)
            headers = {}
            for line in lines[1:]:
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
            length = int(headers.get("content-length", "0"))
            body_start = head_end + 4
            if len(stream) < body_start + length:
                return parsed
            parsed.append({
                "method": method,
                "target": target,
                "headers": headers,
                "body": stream[body_start:body_start + length],
            })
            offset = body_start + length


@pytest.mark.asyncio
async def test_real_sdk_upload_bytes_match_server_disk_sha(tmp_path):
    """组 1：真实 SDK 的线缆字节与服务端写盘的长度/SHA 一致。

    上传请求体必须是源文件原字节的顺序切片：不是 JSON、不是 Base64、不是 multipart，
    也没有 Content-Encoding；创建请求只是小 JSON，且远小于源文件本身。
    """
    source = make_container(tmp_path, "speech.mp3", seconds=30.0)
    source_bytes = source.read_bytes()
    assert len(source_bytes) > 4096, "对照样本必须明显大于创建 JSON，否则「整文件未入 JSON」无约束力"
    recovery = tmp_path / "resume.json"

    async with running_runner_server(tmp_path) as harness:
        proxy = await RecordingProxy(harness.port).start()
        handle = await submit_file_http(
            source, proxy.url, resume_path=recovery, chunk_bytes=64 * 1024,
            seg_duration=5.0, seg_overlap=1.0,
        )
        # 轮询也走同一代理：恢复文件绑定的就是这条客户端看到的地址
        deadline = asyncio.get_running_loop().time() + 60
        while True:
            status = await get_file_job_http(proxy.url, resume_path=recovery)
            if status.state in {"DONE", "FAILED"}:
                break
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"任务未在期限内到达终态，最后状态={status}")
            await asyncio.sleep(0.02)
        await proxy.stop()
        assert status.state == "DONE", status

        requests = proxy.requests()
        creates = [item for item in requests if item["method"] == "POST" and item["target"] == "/v1/uploads"]
        patches = [item for item in requests if item["method"] == "PATCH"]
        commits = [item for item in requests if item["target"].endswith("/commit")]
        assert len(creates) == 1, [item["target"] for item in requests]
        assert len(commits) == 1, [item["target"] for item in requests]
        assert patches, "没有任何 PATCH 请求，线缆字节证据缺失"

        # PATCH 体是源文件原字节的顺序切片，且 Content-Length 与实际体长一致
        assert b"".join(item["body"] for item in patches) == source_bytes
        for item in patches:
            assert item["headers"]["content-type"] == "application/octet-stream"
            assert int(item["headers"]["content-length"]) == len(item["body"])
            assert "content-encoding" not in item["headers"]
        running = 0
        for item in patches:
            assert running == int(item["headers"]["upload-offset"])
            assert item["body"] == source_bytes[running:running + len(item["body"])]
            running += len(item["body"])
        assert running == len(source_bytes)

        # 整份文件没有以 JSON / Base64 / multipart 的形式出现
        assert b"multipart/form-data" not in proxy.client_bytes()
        assert b"content-encoding" not in proxy.client_bytes().lower()
        assert base64.b64encode(source_bytes) not in proxy.client_bytes()
        create_body = json.loads(creates[0]["body"])
        assert create_body["size_bytes"] == len(source_bytes)
        assert create_body["sha256"] == sha256(source_bytes).hexdigest()
        assert len(creates[0]["body"]) < 1024, "创建请求必须是小 JSON，不能夹带文件内容"

        # 服务端写盘：长度、SHA、逐字节都等于源文件
        stored = json.loads(recovery.read_text(encoding="utf-8"))
        written = harness.data_dir / "sources" / f"{stored['upload_id']}.bin"
        assert written.stat().st_size == len(source_bytes)
        assert written.read_bytes() == source_bytes
        assert sha256(written.read_bytes()).hexdigest() == create_body["sha256"]
        row = harness.read_db(
            "SELECT size_bytes, sha256, confirmed_offset FROM uploads WHERE upload_id=?",
            (stored["upload_id"],),
        )[0]
        assert row["size_bytes"] == len(source_bytes)
        assert row["sha256"] == create_body["sha256"]
        assert row["confirmed_offset"] == len(source_bytes)
        assert handle.job_id == stored["job_id"]


# ---------------------------------------------------------------- 组 3：丢响应后的显式恢复


class RequestRecorder:
    """包住 httpx 的唯一发送出口，记录每次真实请求，并可丢弃指定响应。

    「丢弃」不是伪造响应：请求照样经真实 httpx 栈发到真实服务端，服务端照样真实建
    Job 并返回 202，只是这一次响应在回到客户端之前被扔掉，SDK 自己的
    ``httpx.TransportError → AsrError(connection_lost)`` 映射仍然原样执行。
    """

    def __init__(self, drop_commit: bool = False):
        self.calls: list[tuple[str, str]] = []
        self.dropped: list[dict] = []
        self._drop_commit = drop_commit

    def install(self, monkeypatch) -> "RequestRecorder":
        recorder = self
        original_send = httpx.AsyncClient.send

        async def send(client, request, **kwargs):
            recorder.calls.append((request.method, str(request.url)))
            response = await original_send(client, request, **kwargs)
            if (
                recorder._drop_commit
                and request.method == "POST"
                and request.url.path.endswith("/commit")
            ):
                recorder.dropped.append({
                    "status": response.status_code,
                    "body": response.text,
                })
                raise httpx.RemoteProtocolError(
                    "commit 响应在返回客户端前丢失", request=request,
                )
            return response

        monkeypatch.setattr(httpx.AsyncClient, "send", send)
        return self


@pytest.mark.asyncio
async def test_lost_commit_response_recovers_with_exactly_one_recognition(
    tmp_path, monkeypatch
):
    """组 3：commit 确认真的丢失后，显式恢复拿到同一 Job，识别恰好发生 1 次。

    证据分三层：客户端实际发出的请求序列（只有一次 GET 恢复，不再自动重发 commit）、
    SQLite 里的 Job/结果行数、以及真实 ffmpeg 解码次数与识别子进程收到的段数——
    最后一项保证「没有把同一个 Job 重新识别一遍」。
    """
    ffmpeg_log = install_recording_ffmpeg(tmp_path, monkeypatch)
    source = make_container(tmp_path, "speech.mp3", seconds=20.0)
    recovery = tmp_path / "resume.json"

    async with running_runner_server(tmp_path) as harness:
        recorder = RequestRecorder(drop_commit=True).install(monkeypatch)
        with pytest.raises(sdk_http.AsrError) as caught:
            await submit_file_http(
                source, harness.base_url, resume_path=recovery, chunk_bytes=64 * 1024,
                seg_duration=5.0, seg_overlap=1.0,
            )
        assert caught.value.code == "connection_lost"
        assert len(recorder.dropped) == 1
        assert recorder.dropped[0]["status"] == 202, recorder.dropped
        committed_job = json.loads(recorder.dropped[0]["body"])["job_id"]
        # 提交阶段：一次创建、若干 PATCH、一次 commit——commit 只发过一次
        assert [method for method, _ in recorder.calls].count("POST") == 2
        assert sum(1 for method, url in recorder.calls if url.endswith("/commit")) == 1

        partial = json.loads(recovery.read_text(encoding="utf-8"))
        assert partial["job_id"] is None, "客户端没拿到确认就不许假装自己知道 job_id"
        assert partial["confirmed_offset"] == source.stat().st_size
        upload_id = partial["upload_id"]

        # 服务端确实已经建了 Job（响应是在网络里丢的，不是在服务端丢的）
        rows = harness.read_db(
            "SELECT job_id, state FROM jobs WHERE job_id=?", (committed_job,)
        )
        assert len(rows) == 1, rows

        # ---- 显式恢复：客户端只发一次 GET，从 COMMITTED 找回同一 Job ---------
        before_resume = len(recorder.calls)
        handle = await resume_file_http(source, harness.base_url, resume_path=recovery)
        resumed_calls = recorder.calls[before_resume:]
        assert [method for method, _ in resumed_calls] == ["GET"], resumed_calls
        assert resumed_calls[0][1].endswith(f"/v1/uploads/{upload_id}"), resumed_calls
        assert handle.job_id == committed_job
        assert handle.upload_id == upload_id
        assert json.loads(recovery.read_text(encoding="utf-8"))["job_id"] == committed_job

        status = await wait_terminal(harness, recovery)
        assert status.state == "DONE", status
        transcript = await get_file_result_http(harness.base_url, resume_path=recovery)
        assert transcript.raw["task_id"] == committed_job

        # ---- 识别次数：一次解码、一次 Job、每个段只被识别一次 ----------------
        assert len(read_ffmpeg_starts(ffmpeg_log)) == 1, read_ffmpeg_starts(ffmpeg_log)
        assert harness.read_db("SELECT COUNT(*) AS n FROM jobs")[0]["n"] == 1
        assert harness.read_db("SELECT COUNT(*) AS n FROM results")[0]["n"] == 1
        tasks = harness.tasks(committed_job)
        assert len(tasks) >= 2, tasks
        assert len(harness.calls) == len(tasks), (harness.calls, tasks)
        assert sum(1 for item in tasks if item["is_final"]) == 1
        finals = [item for item in harness.results(committed_job) if item["is_final"]]
        assert len(finals) == 1, harness.results(committed_job)
        assert transcript.raw["text"] == finals[0]["text"]
        # 恢复之后客户端一次都没有再发 PATCH 或 commit（后面的请求全是状态轮询）
        after_resume = [method for method, _ in recorder.calls[before_resume + 1:]]
        assert after_resume, "恢复之后至少应有状态轮询请求"
        assert set(after_resume) == {"GET"}, after_resume


# ---------------------------------------------------------------- 组 10：重采样后的有界 PCM


def make_pcm_container(tmp_path: Path, name: str, *, seconds: float, rate: int, channels: int):
    """用真实 ffmpeg 从指定采样率/声道数的原始 PCM 造容器（不是 16 kHz 的同义改写）。"""
    count = round(seconds * rate)
    t = np.arange(count, dtype=np.float64) / rate
    wave = 0.2 * np.sin(2 * np.pi * 220 * t) * np.sin(2 * np.pi * 0.7 * t)
    interleaved = np.repeat(wave, channels).astype("<f4").tobytes()
    raw = tmp_path / f"{name}.raw"
    raw.write_bytes(interleaved)
    target = tmp_path / name
    subprocess.run(
        [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "f32le", "-ar", str(rate), "-ac", str(channels), "-i", str(raw),
         "-c:a", "pcm_s16le", str(target)],
        check=True, capture_output=True,
    )
    assert target.stat().st_size > 0
    return target


RESAMPLE_MATRIX = [
    {"name": "stereo44.wav", "rate": 44100, "channels": 2},
    {"name": "mono8k.wav", "rate": 8000, "channels": 1},
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", RESAMPLE_MATRIX)
async def test_resampled_sources_produce_bounded_16k_mono_f32_segments(
    tmp_path, monkeypatch, case
):
    """组 10：44.1 kHz 立体声 / 8 kHz 单声道经真 ffmpeg 后仍是有界 16 k mono f32 段。

    关键不变式：段样本数之和等于独立跑一次真 ffmpeg 得到的样本数（真的重采样+降混，
    不是把原始字节搬过去）；每段 `samplerate=16000`、字节数是 4 的倍数、单段有界；
    送进 worker 的 PCM 字节明显少于源文件字节（没有把整文件交给 worker）。
    """
    ffmpeg_log = install_recording_ffmpeg(tmp_path, monkeypatch)
    source = make_pcm_container(
        tmp_path, case["name"], seconds=20.0, rate=case["rate"], channels=case["channels"],
    )
    source_bytes = source.stat().st_size
    expected_samples = decoded_sample_count(source)
    recovery = tmp_path / "resume.json"

    async with running_runner_server(tmp_path) as harness:
        handle = await submit(harness, source, recovery, seg_duration=5.0, seg_overlap=1.0)
        status = await wait_terminal(harness, recovery)
        assert status.state == "DONE", status
        transcript = await get_file_result_http(harness.base_url, resume_path=recovery)
        assert transcript.raw["task_id"] == handle.job_id

        segments = harness.tasks(handle.job_id)
        assert len(segments) >= 2, segments
        assert all(item["owner_kind"] == "http" for item in segments)
        assert all(item["samplerate"] == 16000 for item in segments), segments
        assert all(item["data_bytes"] % 4 == 0 for item in segments), segments
        assert all(item["samples"] == item["data_bytes"] // 4 for item in segments)
        assert sum(1 for item in segments if item["is_final"]) == 1
        assert segments[-1]["is_final"] is True

        # 去掉重叠后各段正好覆盖真 ffmpeg 解出的样本数
        covered = segments[-1]["samples"]
        for previous, item in zip(segments, segments[1:]):
            stride = previous["samples"] - round(previous["overlap"] * 16000)
            assert item["offset"] == pytest.approx(
                previous["offset"] + stride / 16000, abs=1e-6,
            )
            covered += stride
        assert covered == expected_samples, (case, covered, expected_samples)

        # 有界：单段样本数不超过段预算（seg_duration + seg_overlap = 6s，留 1s 余量）
        assert max(item["samples"] for item in segments) <= 16000 * 7
        # worker 拿到的是有界 16 k mono f32：字节数正好等于「样本数 + 各段重叠」，
        # 没有任何一段就是整份源文件（段摘要里找不到源文件摘要）
        delivered_samples = sum(item["samples"] for item in segments)
        overlap_samples = sum(
            round(item["overlap"] * 16000) for item in segments[:-1]
        )
        assert delivered_samples == expected_samples + overlap_samples, (
            case, delivered_samples, expected_samples, overlap_samples,
        )
        source_digest = sha256(source.read_bytes()).hexdigest()[:16]
        assert source_digest not in {item["data_sha256_prefix"] for item in segments}

        # 解码 argv 仍固定为 16 kHz mono f32 管道
        starts = read_ffmpeg_starts(ffmpeg_log)
        assert len(starts) == 1, starts
        assert starts[0]["argv"][-7:] == [
            "-ar", "16000", "-ac", "1", "-f", "f32le", "pipe:1",
        ]
        assert str(harness.data_dir / "sources" / f"{handle.upload_id}.bin") in starts[0]["argv"]


# ---------------------------------------------------------------- 组 11：末段失败


@pytest.mark.asyncio
async def test_final_segment_failure_fails_job_without_publishing_partial(tmp_path):
    """组 11：`is_final` 那一次解码失败时，整任务失败且不发布缺段成功。

    失败注入落在**最后一次**引擎调用（真实 `is_final` 段），此前各段已经真实产出
    非 final Result——正是「前几段成功、最后一段炸掉」这种最容易被误判为成功的形态。
    对照组用同样的源与分段参数跑一次成功解码，证明末段确实存在（失败点确实是末段）。
    """
    source = make_container(tmp_path, "speech.mp3", seconds=20.0)
    options = dict(seg_duration=5.0, seg_overlap=1.0)

    async with running_runner_server(tmp_path) as harness:
        ok_recovery = tmp_path / "ok.json"
        ok_handle = await submit(harness, source, ok_recovery, **options)
        assert (await wait_terminal(harness, ok_recovery)).state == "DONE"
        ok_segments = harness.tasks(ok_handle.job_id)
        final_calls = sum(1 for item in ok_segments if item["is_final"])
        assert final_calls == 1, ok_segments
        segment_count = len(ok_segments)
        assert segment_count >= 2, ok_segments

    # 同样的源、同样的分段参数：最后一次引擎调用抛错
    failing = dict(FAKE_ENGINE, fail_on_call=segment_count)
    crash_dir = tmp_path / "crash"
    crash_dir.mkdir()
    source = make_container(crash_dir, "speech.mp3", seconds=20.0)
    recovery = crash_dir / "resume.json"
    async with running_runner_server(crash_dir, options=failing) as harness:
        handle = await submit(harness, source, recovery, **options)
        status = await wait_terminal(harness, recovery)
        assert status.state == "FAILED", status
        assert status.error_code == "inference_failed"
        assert status.result_available is False

        # 失败点确实落在 is_final 段：前 N-1 段已产出非 final 结果，末段没有
        segments = harness.tasks(handle.job_id)
        assert len(segments) == segment_count, segments
        assert sum(1 for item in segments if item["is_final"]) == 1
        assert segments[-1]["is_final"] is True
        emitted = harness.results(handle.job_id)
        assert len(emitted) == segment_count, emitted
        successes = [item for item in emitted if not item["error_code"]]
        failures = [item for item in emitted if item["error_code"]]
        assert len(successes) == segment_count - 1, emitted
        assert all(item["is_final"] is False and item["tokens"] for item in successes)
        # 末段产出的是带 error_code 的空结果，绝不是带正文的 final 结果
        assert len(failures) == 1, emitted
        assert failures[0]["error_code"] == "inference_failed"
        assert failures[0]["is_final"] is False
        assert failures[0]["text"] == "" and failures[0]["tokens"] == []
        assert not any(item["is_final"] for item in emitted), emitted
        assert len(harness.calls) == segment_count, harness.calls

        # 缺段成功绝不被发布：库里没有结果行，终态是 FAILED
        assert harness.read_db("SELECT COUNT(*) AS n FROM results")[0]["n"] == 0
        row = harness.read_db(
            "SELECT state, error_code FROM jobs WHERE job_id=?", (handle.job_id,)
        )[0]
        assert (row["state"], row["error_code"]) == ("FAILED", "inference_failed")
        response = await raw_get(harness, recovery, "/result")
        assert response.status_code == 409
        body = response.json()
        assert body["code"] == "job_failed"
        assert body["error_code"] == "inference_failed"
        assert "text" not in body and "tokens" not in body
        # 终态收尾：owner 与闸门都已释放，事件循环仍健康
        assert list(harness.state.active_http_jobs) == []
        assert harness.state.tasks == {}
        assert harness.http_server.fatal is None
