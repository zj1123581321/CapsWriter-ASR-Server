"""model 请求在代理、服务端和 SDK 边界上的行为。"""
from __future__ import annotations

import asyncio
import json
import time

import pytest
import websockets

from config_server import ServerConfig
from core.protocol import AudioMessage
from core.proxy.backend import BackendState
from core.proxy.proxy_server import ProxyServer
from sdk.capswriter_asr import client as sdk_client
from tests.test_proxy_version_routing import fake_backend


def audio_frame(task_id, *, model=None, encoding="f32le", final=True):
    frame = {
        "task_id": task_id,
        "source": "file",
        "data": "",
        "is_final": final,
        "time_start": time.time(),
        "encoding": encoding,
        "samples_total": 0,
    }
    if model is not None:
        frame["model"] = model
    return json.dumps(frame)


def result_frame(task_id):
    return json.dumps({
        "type": "result",
        "task_id": task_id,
        "is_final": True,
        "duration": 0,
        "time_start": 0,
        "time_submit": 0,
        "time_complete": 0,
        "text": "ok",
    })


@pytest.mark.asyncio
async def test_proxy_model_filter_routes_ten_tasks_and_keeps_unfiltered_candidates():
    hits = {"paraformer": [], "qwen_asr_mlx": []}

    def handler_for(model):
        async def handler(ws):
            async for raw in ws:
                task = json.loads(raw)
                hits[model].append(task["task_id"])
                await ws.send(result_frame(task["task_id"]))

        return handler

    async with fake_backend(
        handler_for("paraformer"),
        health_payload={"protocol_version": 2, "encodings": ["f32le"], "model": "paraformer"},
    ) as (paraformer_url, _):
        async with fake_backend(
            handler_for("qwen_asr_mlx"),
            health_payload={"protocol_version": 2, "encodings": ["f32le"], "model": "qwen_asr_mlx"},
        ) as (qwen_url, _):
            proxy = ProxyServer("127.0.0.1", 0, [
                BackendState(id="paraformer", url=paraformer_url),
                BackendState(id="qwen_asr_mlx", url=qwen_url),
            ])
            async with proxy.serve() as server:
                url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
                async with websockets.connect(url, max_size=None) as client:
                    for index in range(10):
                        await client.send(audio_frame(f"routed-{index}", model="qwen_asr_mlx"))
                        assert json.loads(await asyncio.wait_for(client.recv(), 3))["type"] == "result"
                    assert hits == {"paraformer": [], "qwen_asr_mlx": [f"routed-{i}" for i in range(10)]}

                    for index in range(10):
                        await client.send(audio_frame(f"unfiltered-{index}"))
                        assert json.loads(await asyncio.wait_for(client.recv(), 3))["type"] == "result"
                    assert hits["paraformer"] and hits["qwen_asr_mlx"]

                    await client.send(audio_frame("missing-model", model="sensevoice"))
                    error = json.loads(await asyncio.wait_for(client.recv(), 3))
                    assert error["code"] == "no_backend"
                    assert "sensevoice" in error["message"]
                    with pytest.raises(websockets.exceptions.ConnectionClosed):
                        await asyncio.wait_for(client.recv(), 3)


@pytest.mark.asyncio
async def test_server_rejects_model_different_from_its_config(fake_asr_server, monkeypatch):
    monkeypatch.setattr(ServerConfig, "model_type", "paraformer")
    async with websockets.connect(fake_asr_server.url, max_size=None, ping_interval=None) as client:
        await client.send(audio_frame("wrong-model", model="qwen_asr_mlx"))
        error = json.loads(await asyncio.wait_for(client.recv(), 3))
    assert error["type"] == "error"
    assert error["code"] == "bad_request"
    assert "qwen_asr_mlx" in error["message"]
    assert "paraformer" in error["message"]


@pytest.mark.asyncio
async def test_sdk_rejects_direct_server_model_before_websocket(monkeypatch, tmp_path):
    audio_path = tmp_path / "unused.wav"
    audio_path.touch()
    awaitable_calls = []
    websocket_calls = []

    def connect(*_args, **_kwargs):
        websocket_calls.append(True)
        raise AssertionError("模型校验失败后不得建立 WebSocket")

    monkeypatch.setattr(sdk_client, "_transcode", lambda *_args: awaitable_calls.append(True))
    monkeypatch.setattr(
        sdk_client,
        "_get_health",
        lambda _url: (200, json.dumps({
            "protocol_version": 2,
            "role": "server",
            "model": "paraformer",
            "encodings": ["flac"],
        }).encode()),
    )
    monkeypatch.setattr(sdk_client.websockets, "connect", connect)

    with pytest.raises(sdk_client.AsrError) as caught:
        await sdk_client.transcribe_file(audio_path, "ws://127.0.0.1:6016", model="qwen_asr_mlx")
    assert caught.value.code == "bad_request"
    assert not websocket_calls
    assert not awaitable_calls


def test_sdk_model_is_optional_on_each_encoded_frame():
    without_model = json.loads(sdk_client._audio_frame(
        b"", task_id="one", time_start=0, is_final=True, samples_total=0,
        encoding="flac", seg_duration=15, seg_overlap=2,
        language=None, context=None, model=None,
    ))
    with_model = json.loads(sdk_client._audio_frame(
        b"", task_id="two", time_start=0, is_final=False, samples_total=0,
        encoding="flac", seg_duration=15, seg_overlap=2,
        language=None, context=None, model="qwen_asr_mlx",
    ))
    assert "model" not in without_model
    assert with_model["model"] == "qwen_asr_mlx"


def test_audio_message_round_trips_optional_model():
    message = AudioMessage(
        task_id="task", source="file", data="", is_final=True, time_start=0,
        model="paraformer",
    )
    assert json.loads(message.to_json())["model"] == "paraformer"
    message.model = None
    assert "model" not in json.loads(message.to_json())
