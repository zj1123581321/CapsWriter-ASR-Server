"""最小 WebSocket 示例的真实 JSON 与异常终态契约。"""
from __future__ import annotations

import base64
import json
import wave

import pytest
import websockets

from examples.websocket_transcribe import transcribe_short_wav


def make_pcm16_wav(path):
    pcm = b"\x00\x01\xff\x7f"
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(pcm)
    return pcm


@pytest.mark.asyncio
async def test_example_sends_real_v2_payload_and_returns_final_result(tmp_path):
    path = tmp_path / "short.wav"
    pcm = make_pcm16_wav(path)
    received = {}

    async def server(socket):
        message = await socket.recv()
        received.update(json.loads(message))
        await socket.send(json.dumps({
            "type": "result", "task_id": received["task_id"],
            "is_final": True, "text": "测试",
        }))

    async with websockets.serve(server, "127.0.0.1", 0) as service:
        port = service.sockets[0].getsockname()[1]
        result = await transcribe_short_wav(path, f"ws://127.0.0.1:{port}")

    assert result["text"] == "测试"
    assert received["source"] == "file"
    assert received["encoding"] == "s16le"
    assert received["is_final"] is True
    assert received["samples_total"] == len(pcm) // 2
    assert base64.b64decode(received["data"], validate=True) == pcm
    assert received["task_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["error", "close"])
async def test_example_never_reports_error_or_early_close_as_success(tmp_path, terminal):
    path = tmp_path / "short.wav"
    make_pcm16_wav(path)

    async def server(socket):
        await socket.recv()
        if terminal == "error":
            await socket.send(json.dumps({"type": "error", "code": "bad_request", "message": "bad audio"}))
        else:
            await socket.close(code=1011, reason="failed")

    async with websockets.serve(server, "127.0.0.1", 0) as service:
        port = service.sockets[0].getsockname()[1]
        expected = RuntimeError if terminal == "error" else websockets.exceptions.ConnectionClosed
        with pytest.raises(expected):
            await transcribe_short_wav(path, f"ws://127.0.0.1:{port}")


@pytest.mark.asyncio
async def test_example_rejects_other_wav_format_with_transcode_command(tmp_path):
    path = tmp_path / "wide.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(48000)
        audio.writeframes(b"\x00\x00\x00\x00")
    with pytest.raises(ValueError, match="ffmpeg -nostdin -i input.m4a"):
        await transcribe_short_wav(path, "ws://127.0.0.1:1")
