"""SDK 实际压缩字节流与样本数声明的回归测试。"""
from __future__ import annotations

import base64
import json
import os
import shutil
import struct
import subprocess
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
import websockets
from websockets.datastructures import Headers

from sdk.capswriter_asr import Transcript, transcribe_file
from sdk.capswriter_asr import client as sdk_client


pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="测试环境没有真实 ffmpeg",
)


def _run_ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _iter_boxes(data: bytearray, start: int, end: int):
    offset = start
    while offset + 8 <= end:
        size = struct.unpack_from(">I", data, offset)[0]
        box_type = bytes(data[offset + 4:offset + 8])
        header_size = 8
        if size == 1:
            size = struct.unpack_from(">Q", data, offset + 8)[0]
            header_size = 16
        elif size == 0:
            size = end - offset
        box_end = offset + size
        if box_end > end or size < header_size:
            raise AssertionError(f"无效 MP4 box: {box_type!r} @{offset}")
        yield offset, header_size, box_type, box_end
        offset = box_end


def _shorten_mp4_header(path: Path) -> None:
    data = bytearray(path.read_bytes())
    containers = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts"}
    duration_offsets = {b"mvhd": (16, 20), b"mdhd": (16, 20), b"tkhd": (24, 28)}

    def visit(start: int, end: int) -> None:
        for offset, header_size, box_type, box_end in _iter_boxes(data, start, end):
            payload = offset + header_size
            if box_type in duration_offsets:
                version = data[payload]
                offset_v0, offset_v1 = duration_offsets[box_type]
                duration_offset = offset_v0 if version == 0 else offset_v1
                width = 4 if version == 0 else 8
                duration = int.from_bytes(
                    data[payload + duration_offset:payload + duration_offset + width],
                    "big",
                )
                data[payload + duration_offset:payload + duration_offset + width] = (
                    (duration // 2).to_bytes(width, "big")
                )
            if box_type in containers:
                visit(payload, box_end)

    visit(0, len(data))
    path.write_bytes(data)


@pytest.fixture(scope="module")
def shortened_mp4(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("samples-total") / "shortened.mp4"
    _run_ffmpeg(
        "-f", "lavfi",
        "-i", "sine=frequency=440:duration=60",
        "-ar", "48000",
        "-ac", "2",
        "-c:a", "aac",
        "-b:a", "128k",
        str(path),
    )
    _shorten_mp4_header(path)
    return path


@pytest.fixture(scope="module")
def mismatched_tracks_mp4(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("samples-total") / "mismatched-tracks.mp4"
    _run_ffmpeg(
        "-f", "lavfi",
        "-i", "testsrc=size=320x240:rate=10:duration=300",
        "-f", "lavfi",
        "-i", "sine=frequency=440:duration=60",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-map", "0:v",
        "-map", "1:a",
        str(path),
    )
    return path


def _decoded_sample_count(path: Path) -> int:
    result = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-i", str(path), "-ar", "16000", "-ac", "1", "-f", "s16le", "pipe:1",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert len(result.stdout) % 2 == 0
    return len(result.stdout) // 2


@asynccontextmanager
async def _capture_server():
    state = {"frames": [], "errors": []}

    async def process_request(connection, request):
        path = request.path if hasattr(request, "path") else connection
        if path != "/health":
            return None
        headers = Headers()
        headers["Content-Type"] = "application/json"
        body = json.dumps({
            "protocol_version": 2,
            "encodings": ["flac", "ogg_opus", "f32le", "s16le"],
        }).encode()
        if hasattr(request, "path"):
            from websockets.http11 import Response

            return Response(200, HTTPStatus.OK.phrase, headers, body)
        return HTTPStatus.OK, headers, body

    async def receive(ws):
        async for message in ws:
            frame = json.loads(message)
            state["frames"].append(frame)
            if frame["is_final"]:
                await ws.send(json.dumps({
                    "type": "result",
                    "is_final": True,
                    "text": "ok",
                    "duration": 0.0,
                }))
                return

    async with websockets.serve(
        receive,
        "127.0.0.1",
        0,
        process_request=process_request,
        ping_interval=None,
        max_size=None,
        max_queue=None,
    ) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}", state


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture_name", ["shortened", "mismatched"])
@pytest.mark.parametrize("encoding", ["flac", "ogg_opus"])
async def test_compressed_samples_total_matches_actual_sent_stream(
    fixture_name, encoding, shortened_mp4, mismatched_tracks_mp4,
):
    path = {
        "shortened": shortened_mp4,
        "mismatched": mismatched_tracks_mp4,
    }[fixture_name]
    expected_samples = _decoded_sample_count(path)

    async with _capture_server() as (url, state):
        transcript = await transcribe_file(path, url, encoding=encoding)

    assert isinstance(transcript, Transcript)
    assert transcript.text == "ok"
    assert not state["errors"]
    assert state["frames"][-1]["is_final"] is True
    assert state["frames"][-1]["samples_total"] == expected_samples
    assert all(frame["encoding"] == encoding for frame in state["frames"])


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture_name", ["shortened", "mismatched"])
@pytest.mark.parametrize("encoding,sample_width", [("f32le", 4), ("s16le", 2)])
async def test_raw_samples_total_matches_sent_bytes(
    fixture_name, encoding, sample_width, shortened_mp4, mismatched_tracks_mp4,
):
    path = {
        "shortened": shortened_mp4,
        "mismatched": mismatched_tracks_mp4,
    }[fixture_name]

    async with _capture_server() as (url, state):
        transcript = await transcribe_file(path, url, encoding=encoding)

    sent_payload_bytes = sum(
        len(base64.b64decode(frame["data"]))
        for frame in state["frames"]
    )
    assert isinstance(transcript, Transcript)
    assert sent_payload_bytes > 0
    assert state["frames"][-1]["samples_total"] == sent_payload_bytes // sample_width


@pytest.mark.asyncio
async def test_deadline_uses_decoded_sample_count(monkeypatch, tmp_path):
    source = tmp_path / "source.wav"
    sf.write(source, np.zeros(16000, dtype=np.float32), 16000, subtype="FLOAT")
    deadlines = []

    async def no_health_check(*_args):
        return None

    async def fake_transcode(*_args):
        return b"compressed"

    async def fake_count(*_args):
        return 600 * 16000

    async def finish(*_args, **_kwargs):
        return Transcript(text="ok", tokens=[], timestamps=[], duration=0, raw={})

    monkeypatch.setattr(sdk_client, "_check_server", no_health_check)
    monkeypatch.setattr(sdk_client, "_transcode", fake_transcode)
    monkeypatch.setattr(sdk_client, "_count_decoded_samples", fake_count)
    monkeypatch.setattr(sdk_client, "_transcribe_connected", finish)

    original_set_deadline = sdk_client._operation

    async def operation_with_observation(*args, **kwargs):
        kwargs["set_deadline"] = deadlines.append
        return await original_set_deadline(*args, **kwargs)

    monkeypatch.setattr(sdk_client, "_operation", operation_with_observation)
    transcript = await transcribe_file(source, "ws://127.0.0.1:1", encoding="flac")

    assert transcript.text == "ok"
    assert deadlines == [660.0]


@pytest.mark.asyncio
async def test_transcription_never_shells_out_to_ffprobe(
    monkeypatch, tmp_path,
):
    """声明值只能来自自己发出的字节流；任何 ffprobe 探测都是回归。

    假 ffprobe 一旦被调用就退出码 1 并输出 N/A（旧实现的失败形态），
    因此若有人重新引入「读容器时长」路径，本用例会变红而不是静默通过。
    """
    source = tmp_path / "source.wav"
    sf.write(source, np.zeros(16000, dtype=np.float32), 16000, subtype="FLOAT")
    tool_dir = tmp_path / "probe"
    tool_dir.mkdir()
    ffprobe = tool_dir / "ffprobe"
    ffprobe.write_text(
        "#!/bin/sh\nprintf 'ffprobe-was-called\\n' >> \"$CAPSWRITER_FFPROBE_CALL_LOG\"\n"
        "printf 'N/A\\n'\nexit 1\n",
        encoding="utf-8",
    )
    ffprobe.chmod(0o755)
    call_log = tmp_path / "ffprobe-calls.log"
    monkeypatch.setenv("PATH", f"{tool_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CAPSWRITER_FFPROBE_CALL_LOG", str(call_log))

    async with _capture_server() as (url, state):
        transcript = await transcribe_file(source, url, encoding="flac")

    assert transcript.text == "ok"
    assert state["frames"][-1]["is_final"] is True
    assert state["frames"][-1]["samples_total"] == 16000
    assert not call_log.exists(), "SDK 不得调用 ffprobe 探测音频时长"
