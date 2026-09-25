# coding: utf-8
"""对齐器、标点加载与文件时间戳的 fail-fast 契约测试。"""
import sys
import types
from types import SimpleNamespace

import numpy as np
import pytest

from core.server.engines.base import EngineCapabilities
from core.server.engines.factory import EngineFactory
from core.server.schema import Task
from core.server.state import WorkerState
from core.server.worker.model_loader import ModelLoader
from core.server.worker import model_loader as model_loader_module
from core.server.worker import pipeline as pipeline_module

def _fake_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module

def _set_aligner_paths(monkeypatch, tmp_path):
    from core.server.engines.factory import ModelPaths

    aligner_dir = tmp_path / "aligner"
    frontend = aligner_dir / "encoder_frontend.onnx"
    backend = aligner_dir / "encoder_backend.onnx"
    decode = aligner_dir / "llm_decode.gguf"
    monkeypatch.setattr(ModelPaths, "force_aligner_gguf_dir", aligner_dir)
    monkeypatch.setattr(ModelPaths, "force_aligner_gguf_encoder_frontend", frontend)
    monkeypatch.setattr(ModelPaths, "force_aligner_gguf_encoder_backend", backend)
    monkeypatch.setattr(ModelPaths, "force_aligner_gguf_llm_decode", decode)
    return aligner_dir, frontend

def test_create_align_engine_raises_and_keeps_misplaced_model_diagnosis(monkeypatch, tmp_path):
    aligner_dir, frontend = _set_aligner_paths(monkeypatch, tmp_path)
    (tmp_path / frontend.name).write_bytes(b" misplaced ")

    class AlignerConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class QwenForceAligner:
        def __init__(self, config):
            raise FileNotFoundError("对齐模型路径不存在")

    module_name = "core.server.engines.force_aligner_gguf.align_engine"
    monkeypatch.setitem(sys.modules, module_name, _fake_module(
        module_name, AlignerConfig=AlignerConfig, QwenForceAligner=QwenForceAligner
    ))

    with pytest.raises(RuntimeError, match="对齐模型路径不存在") as exc:
        EngineFactory.create_align_engine()
    assert "模型文件似乎错放到了上级目录" in str(exc.value)
    assert str(aligner_dir) in str(exc.value)

def test_create_punc_engine_propagates_constructor_failure(monkeypatch):
    class CTTransformerPuncEngine:
        def __init__(self, model_path):
            raise RuntimeError("标点模型损坏")

    module_name = "core.server.engines.ct_transformer.punc_engine"
    monkeypatch.setitem(sys.modules, module_name, _fake_module(
        module_name, CTTransformerPuncEngine=CTTransformerPuncEngine
    ))

    with pytest.raises(RuntimeError, match="标点模型损坏"):
        EngineFactory.create_punc_engine()
    loader = _model_loader(monkeypatch, [EngineCapabilities.ASR, EngineCapabilities.TIMESTAMPS])
    with pytest.raises(RuntimeError, match="标点模型损坏"):
        loader.load()

class _Recognizer:
    def __init__(self, capabilities):
        self.capabilities = capabilities

def _model_loader(monkeypatch, capabilities):
    monkeypatch.setitem(sys.modules, "sherpa_onnx", _fake_module("sherpa_onnx"))
    monkeypatch.setattr(model_loader_module.Config, "model_type", "test-engine")
    monkeypatch.setattr(model_loader_module.Config, "aligner_idle_timeout", 10)
    recognizer = _Recognizer(capabilities)
    monkeypatch.setattr(
        model_loader_module.EngineFactory,
        "create_asr_engine",
        lambda model_type: recognizer,
    )
    return ModelLoader()

def test_model_loader_preloads_required_aligner_once(monkeypatch):
    loader = _model_loader(monkeypatch, [EngineCapabilities.ASR, EngineCapabilities.PUNC])
    calls = []
    monkeypatch.setattr(
        EngineFactory,
        "create_align_engine",
        lambda: calls.append("load") or SimpleNamespace(
            align=lambda **kwargs: None, cleanup=lambda: None
        ),
    )
    loader.load()

    assert calls == ["load"]
    assert loader.aligner_status == "loaded"

def test_model_loader_propagates_aligner_startup_failure(monkeypatch):
    loader = _model_loader(monkeypatch, [EngineCapabilities.ASR, EngineCapabilities.PUNC])

    def fail():
        raise RuntimeError("对齐器构造失败")

    monkeypatch.setattr(EngineFactory, "create_align_engine", fail)
    with pytest.raises(RuntimeError, match="对齐器构造失败"):
        loader.load()

def test_model_loader_native_timestamps_skip_aligner(monkeypatch):
    loader = _model_loader(
        monkeypatch,
        [EngineCapabilities.ASR, EngineCapabilities.PUNC, EngineCapabilities.TIMESTAMPS],
    )
    assert loader.aligner_status == "not_required"

    def fail():
        raise AssertionError("原生时间戳引擎不应加载外挂对齐器")

    monkeypatch.setattr(EngineFactory, "create_align_engine", fail)
    loader.load()

    assert loader.aligner is None
    assert loader.aligner_status == "native"

class _FakeStream:
    def __init__(self):
        self.result = SimpleNamespace(text="测试", tokens=[], timestamps=[])

    def accept_waveform(self, sample_rate, samples):
        pass

class _FakeRecognizer:
    def __init__(self, capabilities=None):
        self.capabilities = capabilities or [EngineCapabilities.ASR]

    def create_stream(self):
        self.stream = _FakeStream()
        return self.stream

    def decode_stream(self, stream, **kwargs):
        pass

def _run_pipeline(monkeypatch, task_type, aligner, capabilities=None):
    monkeypatch.setattr(pipeline_module.Config, "gpu_boost_enabled", False)
    monkeypatch.setattr(pipeline_module.Config, "format_num", False)
    monkeypatch.setattr(pipeline_module.Config, "format_spell", False)

    def process_audio(task, result):
        result.duration = 2.0
        return np.ones(32000, dtype=np.float32)

    monkeypatch.setattr(pipeline_module, "process_audio_task", process_audio)
    task = Task(
        task_type, b"audio", 0.0, 0.0, f"{task_type}-task", "socket", True, 0.0, 0.0,
        language="chinese",
    )
    pipeline = pipeline_module.TaskPipeline(
        recognizer=_FakeRecognizer(capabilities), aligner=aligner, state=WorkerState()
    )
    return pipeline.process(task)

def test_file_pipeline_rejects_empty_alignment(monkeypatch):
    class Aligner:
        def align(self, **kwargs):
            return SimpleNamespace(items=[])

    with pytest.raises(RuntimeError, match="对齐器未返回时间戳"):
        _run_pipeline(monkeypatch, "file", Aligner())

def test_file_pipeline_never_generates_uniform_timestamps(monkeypatch):
    caps = [EngineCapabilities.ASR, EngineCapabilities.TIMESTAMPS]
    with pytest.raises(RuntimeError, match="文件任务没有真实时间戳"):
        _run_pipeline(monkeypatch, "file", None, capabilities=caps)

def test_microphone_pipeline_keeps_character_time_fallback(monkeypatch):
    result = _run_pipeline(monkeypatch, "mic", None)

    assert result.tokens == ["测", "试"]
    assert result.timestamps == [0.0, 1.0]

def test_file_pipeline_uses_aligner_timestamps(monkeypatch):
    class Aligner:
        def align(self, **kwargs):
            return SimpleNamespace(items=[
                SimpleNamespace(text="测", start_time=0.37),
                SimpleNamespace(text="试", start_time=1.23),
            ])

    result = _run_pipeline(monkeypatch, "file", Aligner())

    assert result.tokens == ["测", "试"]
    assert result.timestamps == [0.37, 1.23]
