# coding: utf-8
"""真实 SDK 到服务端与 v2 proxy 的发布边界验收。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from config_server import ServerConfig
from core.proxy.backend import BackendState
from core.proxy.proxy_server import ProxyServer
from tests.harness.fake_engine import ProgrammableFakeEngine
from tests.harness.server import ManagedFakeServerHarness
from tests.test_server_e2e_baseline import assert_gapless, decode_spans

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "sdk"))
from capswriter_asr import Transcript, transcribe_file


@pytest.mark.asyncio
async def test_real_sdk_transcribes_90_seconds_with_all_encodings_and_proxy(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(ServerConfig, "model_type", "fake-test-engine")
    decode_stream = ProgrammableFakeEngine.decode_stream

    def decode_with_timestamps(engine, stream, *args, **kwargs):
        result = decode_stream(engine, stream, *args, **kwargs)
        result.tokens = ["x"]
        result.timestamps = [0.0]
        return result

    monkeypatch.setattr(ProgrammableFakeEngine, "decode_stream", decode_with_timestamps)
    audio_path = tmp_path / "ninety-seconds.wav"
    sf.write(audio_path, np.zeros(90 * 16000, dtype=np.float32), 16000, subtype="FLOAT")
    backend = await ManagedFakeServerHarness.start(options={"supports_timestamps": True})
    try:
        for encoding in ("flac", "ogg_opus", "s16le", "f32le"):
            transcript = await transcribe_file(
                audio_path,
                backend.url,
                encoding=encoding,
                seg_duration=100,
                seg_overlap=0,
                deadline_total=240,
            )
            assert isinstance(transcript, Transcript)
            assert_gapless(decode_spans(transcript.text), 90_000, backend.calls)

        proxy = ProxyServer(
            "127.0.0.1", 0, [BackendState(id="e2e-server", url=backend.url)]
        )
        async with proxy.serve() as listener:
            proxy_url = f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
            transcript = await transcribe_file(
                audio_path,
                proxy_url,
                encoding="flac",
                seg_duration=100,
                seg_overlap=0,
                deadline_total=240,
            )
        assert isinstance(transcript, Transcript)
        assert_gapless(decode_spans(transcript.text), 90_000, backend.calls)
    finally:
        await backend.stop()
