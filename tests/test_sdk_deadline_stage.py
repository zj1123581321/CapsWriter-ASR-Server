"""默认时限的两段计时：本地准备阶段结束后远端预算重新锚定，超时分阶段报错。"""
from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from http import HTTPStatus

import pytest
from websockets.datastructures import Headers

from sdk.capswriter_asr import AsrError, Transcript, transcribe_file
from sdk.capswriter_asr import client as sdk_client


class FakeClock:
    """只替换 SDK 看到的 time.monotonic/time.time，不动真实 time 模块（asyncio 自用）。"""

    def __init__(self):
        self.now = 1000.0
        self.real = time

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.real.time()

    def __getattr__(self, name):
        return getattr(self.real, name)

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _install_fake_clock(monkeypatch) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr(sdk_client, "time", clock)
    return clock


@pytest.fixture
def tracked_processes(monkeypatch):
    """跟踪 SDK 起的子进程，断言函数返回前都被回收（returncode 非 None）。"""
    original = asyncio.create_subprocess_exec
    processes = []

    async def tracked(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", tracked)
    return processes


@pytest.fixture
def sleeping_ffmpeg(tmp_path, monkeypatch):
    """PATH 里放一个只睡觉的假 ffmpeg：本地转码阶段会真的挂着一个子进程。"""
    tool_dir = tmp_path / "media-tools"
    tool_dir.mkdir()
    ffmpeg = tool_dir / "ffmpeg"
    ffmpeg.write_text("#!/bin/sh\nexec sleep 30\n", encoding="utf-8")
    ffmpeg.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tool_dir}{os.pathsep}{os.environ['PATH']}")
    return ffmpeg


@asynccontextmanager
async def fake_v2_server(handler):
    body = json.dumps({"protocol_version": 2, "encodings": ["s16le"]}).encode()

    async def process_request(connection, request):
        path = request.path if hasattr(request, "path") else connection
        if path != "/health":
            return None
        headers = Headers()
        headers["Content-Type"] = "application/json"
        from websockets.http11 import Response

        return Response(200, HTTPStatus.OK.phrase, headers, body)

    async def receive(ws):
        await handler(ws)

    import websockets

    async with websockets.serve(
        receive,
        "127.0.0.1",
        0,
        process_request=process_request,
        ping_interval=None,
        max_size=None,
        max_queue=None,
    ) as server:
        yield f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"


FINAL_RESULT = {
    "type": "result",
    "is_final": True,
    "text": "好的。",
    "tokens": ["好", "的"],
    "timestamps": [0.0, 0.2],
    "duration": 1.0,
}


def _final_payload(**overrides) -> str:
    result = dict(FINAL_RESULT)
    result.update(overrides)
    return json.dumps(result)


def _write_stub_audio(tmp_path):
    audio = tmp_path / "source.wav"
    audio.write_bytes(b"RIFF")
    return audio


@pytest.mark.asyncio
async def test_default_deadline_reanchors_after_local_stage(tmp_path, monkeypatch):
    """默认时限：本地阶段烧掉 100 秒假时钟，1 秒音频的远端预算从本地结束时起算。"""
    audio = _write_stub_audio(tmp_path)
    clock = _install_fake_clock(monkeypatch)
    seen = {}

    async def slow_transcode(*_args):
        clock.advance(100.0)
        return b"\0" * 32000  # 1 秒 s16le

    monkeypatch.setattr(sdk_client, "_transcode", slow_transcode)

    async def handler(ws):
        seen["sent"] = json.loads(await ws.recv())
        # 远端阶段再烧 110 秒假时钟：累计 210 秒 > 入口的 120 秒上限，
        # 只有「远端预算从本地阶段结束时重新锚定」才会成功返回。
        clock.advance(110.0)
        await ws.send(_final_payload())

    async with fake_v2_server(handler) as url:
        transcript = await transcribe_file(audio, url, encoding="s16le", idle_timeout=5)

    assert isinstance(transcript, Transcript)
    assert transcript.text == "好的。"
    assert clock.now == 1000.0 + 210.0
    assert seen["sent"]["samples_total"] == 16000


@pytest.mark.asyncio
async def test_explicit_deadline_total_still_covers_local_stage(tmp_path, monkeypatch):
    """显式 deadline_total=50 是整个调用的墙钟上限：本地阶段推进 100 秒后必须超时。"""
    audio = _write_stub_audio(tmp_path)
    clock = _install_fake_clock(monkeypatch)

    async def slow_transcode(*_args):
        clock.advance(100.0)
        return b"\0" * 32000

    monkeypatch.setattr(sdk_client, "_transcode", slow_transcode)

    async def handler(ws):
        await ws.recv()
        await ws.send(_final_payload())

    async with fake_v2_server(handler) as url:
        with pytest.raises(AsrError) as caught:
            await transcribe_file(
                audio, url, encoding="s16le", deadline_total=50, idle_timeout=5
            )

    assert caught.value.code == "timeout"
    # 显式传参下 set_deadline 不重新锚定：本地阶段吃掉 100 秒后预算已耗尽。
    assert "转录超过deadline_total" in caught.value.message


@pytest.mark.asyncio
async def test_default_path_timeout_names_auto_budget(tmp_path, monkeypatch):
    """默认路径下用户没传过 deadline_total，消息不得把它写成被超过的预算。"""
    audio = _write_stub_audio(tmp_path)
    clock = _install_fake_clock(monkeypatch)

    async def fast_transcode(*_args):
        return b"\0" * 32000

    monkeypatch.setattr(sdk_client, "_transcode", fast_transcode)

    async def handler(ws):
        await ws.recv()
        clock.advance(200.0)  # 远超重锚定后的 max(120 秒, 1 + 60)
        await ws.send(_final_payload())

    async with fake_v2_server(handler) as url:
        with pytest.raises(AsrError) as caught:
            await transcribe_file(audio, url, encoding="s16le", idle_timeout=5)

    assert caught.value.code == "timeout"
    assert "自动预算" in caught.value.message
    assert "远端转录" in caught.value.message
    assert "deadline_total" not in caught.value.message


@pytest.mark.asyncio
async def test_explicit_deadline_kills_local_ffmpeg(
    tmp_path, sleeping_ffmpeg, tracked_processes
):
    """显式 deadline_total 卡在本地转码：抛 timeout，且本地 ffmpeg 子进程被回收。"""
    audio = _write_stub_audio(tmp_path)

    async def handler(ws):
        await ws.recv()  # pragma: no cover - 超时前不该走到上传

    async with fake_v2_server(handler) as url:
        with pytest.raises(AsrError) as caught:
            await transcribe_file(
                audio, url, encoding="s16le", deadline_total=1, idle_timeout=5
            )

    assert caught.value.code == "timeout"
    assert "本地准备" in caught.value.message
    assert tracked_processes, "本地转码应真的起过 ffmpeg 子进程"
    assert all(process.returncode is not None for process in tracked_processes), [
        process.pid for process in tracked_processes if process.returncode is None
    ]


@pytest.mark.asyncio
async def test_remote_stage_timeout_message(tmp_path, monkeypatch):
    """远端阶段超时：本地阶段已完成，消息点名「远端转录」。"""
    audio = _write_stub_audio(tmp_path)

    async def fast_transcode(*_args):
        return b"\0" * 32000

    async def never_reply(ws):
        await ws.recv()
        await ws.wait_closed()

    monkeypatch.setattr(sdk_client, "_transcode", fast_transcode)
    async with fake_v2_server(never_reply) as url:
        with pytest.raises(AsrError) as caught:
            await transcribe_file(
                audio, url, encoding="s16le", deadline_total=1, idle_timeout=5
            )
    assert caught.value.code == "timeout"
    assert "远端转录" in caught.value.message
    assert "本地准备" not in caught.value.message


@pytest.mark.asyncio
async def test_local_and_remote_timeout_messages_are_distinguishable(
    tmp_path, monkeypatch
):
    """两段超时的消息文案必须不同，且各自点名所处阶段。"""
    audio = _write_stub_audio(tmp_path)
    messages = {}

    async def never_reply(ws):
        await ws.recv()
        await ws.wait_closed()

    async def fast_transcode(*_args):
        return b"\0" * 32000

    monkeypatch.setattr(sdk_client, "_transcode", fast_transcode)
    async with fake_v2_server(never_reply) as url:
        with pytest.raises(AsrError) as remote:
            await transcribe_file(
                audio, url, encoding="s16le", deadline_total=1, idle_timeout=5
            )
    messages["remote"] = remote.value.message

    async def hanging_transcode(*_args):
        await asyncio.sleep(30)

    monkeypatch.setattr(sdk_client, "_transcode", hanging_transcode)
    async with fake_v2_server(never_reply) as url:
        with pytest.raises(AsrError) as local:
            await transcribe_file(
                audio, url, encoding="s16le", deadline_total=1, idle_timeout=5
            )
    messages["local"] = local.value.message

    assert "本地准备" in messages["local"]
    assert "远端转录" in messages["remote"]
    assert messages["local"] != messages["remote"]