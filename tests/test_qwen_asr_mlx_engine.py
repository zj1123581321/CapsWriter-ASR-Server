# coding: utf-8
"""
qwen_asr_mlx 适配层逻辑测试（无需真机 MLX，使用注入的假 Session）。

覆盖 decode_stream 的关键分支：能力声明、空音频早退、文本写回、
超 chunk_size 报错、语言映射、cleanup。
"""
import numpy as np
import pytest

from core.server.engines.base import EngineCapabilities, RecognitionStream
from tests.conftest import make_audio


def test_capabilities_asr_punc(mlx_engine):
    """初版能力声明应为 [ASR, PUNC]，不含 TIMESTAMPS（交外挂 Aligner）。"""
    caps = mlx_engine.capabilities
    assert EngineCapabilities.ASR in caps
    assert EngineCapabilities.PUNC in caps
    assert EngineCapabilities.TIMESTAMPS not in caps
    assert EngineCapabilities.HOTWORDS not in caps


def test_create_stream_and_accept_waveform(mlx_engine):
    """create_stream 返回识别流；accept_waveform 存为 float32。"""
    stream = mlx_engine.create_stream()
    assert isinstance(stream, RecognitionStream)
    audio = np.ones(1600, dtype=np.float64)  # 故意给 float64
    stream.accept_waveform(16000, audio)
    assert stream.audio_data.dtype == np.float32


def test_decode_none_audio_is_noop(mlx_engine):
    """audio_data 为 None 时 decode_stream 不应推理、text 保持空。"""
    stream = mlx_engine.create_stream()
    mlx_engine.decode_stream(stream)
    assert stream.result.text == ""
    assert mlx_engine._session.transcribe_calls == []


def test_decode_writes_text(mlx_engine):
    """正常音频：decode_stream 把 Session.transcribe().text 写回 result.text。"""
    stream = mlx_engine.create_stream()
    stream.accept_waveform(16000, make_audio(2.0))
    mlx_engine.decode_stream(stream)
    assert stream.result.text == "你好世界。"
    assert len(mlx_engine._session.transcribe_calls) == 1


def test_decode_rejects_audio_over_chunk_size(mlx_engine):
    """音频超过 chunk_size*16000 应报错，不能截断后继续推理。"""
    over = make_audio(100.0)  # 100s > chunk_size 80s
    stream = mlx_engine.create_stream()
    stream.accept_waveform(16000, over)
    with pytest.raises(ValueError, match=r"100\.000s.*80\.000s"):
        mlx_engine.decode_stream(stream)
    assert mlx_engine._session.transcribe_calls == []


def test_decode_no_truncate_under_chunk_size(mlx_engine):
    """音频不超过 chunk_size 时应原样喂入。"""
    audio = make_audio(10.0)
    stream = mlx_engine.create_stream()
    stream.accept_waveform(16000, audio)
    mlx_engine.decode_stream(stream)
    assert mlx_engine._session.transcribe_calls[0]["audio_len"] == len(audio)


def test_decode_language_mapping_chinese(mlx_engine):
    """language='chinese' 应映射成 Qwen 英文明称 'Chinese' 传给 transcribe。"""
    stream = mlx_engine.create_stream()
    stream.accept_waveform(16000, make_audio(1.0))
    mlx_engine.decode_stream(stream, language="chinese")
    kwargs = mlx_engine._session.transcribe_calls[0]["kwargs"]
    assert kwargs.get("language") == "Chinese"


def test_decode_language_auto_omits_kwarg(mlx_engine):
    """language='auto' 映射为 None，不应传 language 给 transcribe。"""
    stream = mlx_engine.create_stream()
    stream.accept_waveform(16000, make_audio(1.0))
    mlx_engine.decode_stream(stream, language="auto")
    kwargs = mlx_engine._session.transcribe_calls[0]["kwargs"]
    assert "language" not in kwargs


def test_decode_no_language_omits_kwarg(mlx_engine):
    """language=None 时不应传 language。"""
    stream = mlx_engine.create_stream()
    stream.accept_waveform(16000, make_audio(1.0))
    mlx_engine.decode_stream(stream, language=None)
    kwargs = mlx_engine._session.transcribe_calls[0]["kwargs"]
    assert "language" not in kwargs


def test_decode_does_not_request_timestamps(mlx_engine):
    """初版 mic 路径只要文本，不应向 MLX 索取时间戳（交外挂 Aligner）。"""
    stream = mlx_engine.create_stream()
    stream.accept_waveform(16000, make_audio(1.0))
    mlx_engine.decode_stream(stream)
    kwargs = mlx_engine._session.transcribe_calls[0]["kwargs"]
    assert kwargs.get("return_timestamps") is False


def test_cleanup_releases_session(mlx_engine):
    """cleanup 后 _session 应置空，便于 GC 释放 Metal 显存。"""
    mlx_engine.cleanup()
    assert mlx_engine._session is None


def test_dtype_none_not_passed(mlx_engine):
    """默认 dtype=None 时不应把 dtype 传给 Session（用库默认 float16）。"""
    assert "dtype" not in mlx_engine._session.init_kwargs


def _inject_fake_mx(monkeypatch):
    """注入假 mlx.core（含 float16/bfloat16 哨兵），供 dtype 映射测试。"""
    import sys
    import types
    fake_mx = types.ModuleType("mlx.core")
    fake_mx.float16 = "MX_FLOAT16"
    fake_mx.bfloat16 = "MX_BFLOAT16"
    mlx_pkg = types.ModuleType("mlx")
    mlx_pkg.core = fake_mx
    monkeypatch.setitem(sys.modules, "mlx", mlx_pkg)
    monkeypatch.setitem(sys.modules, "mlx.core", fake_mx)


def test_dtype_string_mapped_to_mx(monkeypatch, fake_mlx):
    """dtype 字符串 'float16' 应映射成 mlx.core.float16 对象传给 Session。"""
    _inject_fake_mx(monkeypatch)
    from core.server.engines.qwen_asr_mlx.asr_engine import QwenASRMLXEngine, MLXEngineConfig
    eng = QwenASRMLXEngine(MLXEngineConfig(model="x", dtype="float16"))
    assert eng._session.init_kwargs["dtype"] == "MX_FLOAT16"


def test_invalid_dtype_raises(monkeypatch, fake_mlx):
    """非法 dtype 名应抛 RuntimeError，而不是把坏值塞给 Session。"""
    _inject_fake_mx(monkeypatch)
    from core.server.engines.qwen_asr_mlx.asr_engine import QwenASRMLXEngine, MLXEngineConfig
    with pytest.raises(RuntimeError):
        QwenASRMLXEngine(MLXEngineConfig(model="x", dtype="not_a_dtype"))
