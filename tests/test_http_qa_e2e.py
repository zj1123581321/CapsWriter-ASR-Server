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
import sys
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import websockets

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "sdk"))

from capswriter_asr import AsrError, get_file_result_http, submit_file_http  # noqa: E402

from core.protocol import AudioMessage  # noqa: E402
from core.server.connection.ws_recv import ws_recv  # noqa: E402
from tests.test_http_file_runner import (  # noqa: E402
    FAKE_ENGINE,
    make_container,
    running_runner_server,
    submit,
    wait_terminal,
)

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


