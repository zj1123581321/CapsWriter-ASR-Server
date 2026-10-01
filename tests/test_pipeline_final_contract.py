# coding: utf-8
"""TaskPipeline 最终收尾、短尾和 token/time 自洽契约。"""

from types import SimpleNamespace

import numpy as np
import pytest

from config_server import ServerConfig
from core.server.engines.base import EngineCapabilities
from core.server.schema import Task
from core.server.state import WorkerState
from core.server.worker import pipeline as pipeline_module
from core.server.worker.pipeline import TaskPipeline


def _audio(samples):
    return np.zeros(samples, dtype=np.float32).tobytes()


def _task(
    task_id,
    *,
    task_type="file",
    samples=1600,
    is_final=True,
    owner_kind="ws",
):
    return Task(
        task_type,
        _audio(samples),
        0.0,
        0.0,
        task_id,
        "" if owner_kind == "http" else "socket",
        is_final,
        0.0,
        0.0,
        language="chinese",
        owner_kind=owner_kind,
    )


class _Recognizer:
    def __init__(self, text, tokens, timestamps, *, native=True):
        self.text = text
        self.tokens = list(tokens)
        self.timestamps = list(timestamps)
        self.calls = 0
        self.capabilities = [EngineCapabilities.ASR]
        if native:
            self.capabilities.append(EngineCapabilities.TIMESTAMPS)

    def create_stream(self):
        return _Stream(self)

    def decode_stream(self, stream, **kwargs):
        self.calls += 1


class _Stream:
    def __init__(self, recognizer):
        self._recognizer = recognizer
        self.result = SimpleNamespace(
            text=recognizer.text,
            tokens=list(recognizer.tokens),
            timestamps=list(recognizer.timestamps),
        )

    def accept_waveform(self, sample_rate, samples):
        self.sample_rate = sample_rate
        self.samples = samples


class _IdentityFormatter:
    def format(self, text):
        return text


@pytest.fixture(autouse=True)
def disable_optional_text_rewrites(monkeypatch):
    monkeypatch.setattr(ServerConfig, "format_num", False)
    monkeypatch.setattr(ServerConfig, "format_spell", False)


def _pipeline(recognizer, *, state=None, aligner=None, formatter=None):
    pipeline = TaskPipeline(
        recognizer,
        aligner=aligner,
        state=state or WorkerState(),
    )
    if formatter is not None:
        pipeline.formatter = formatter
    else:
        pipeline.formatter = _IdentityFormatter()
    return pipeline


def test_short_final_file_uses_existing_session_finalizer_without_inference():
    recognizer = _Recognizer("一二", ["一", "二"], [0.1, 0.2])

    class AddPunctuation:
        def format(self, text):
            return f"{text}。" if text else text

    state = WorkerState()
    pipeline = _pipeline(
        recognizer,
        state=state,
        formatter=AddPunctuation(),
    )

    pipeline.process(_task("short", samples=1600, is_final=False))
    result = pipeline.process(_task("short", samples=1599, is_final=True))

    assert recognizer.calls == 1
    assert result.is_final is True
    assert result.text == "一二。"
    assert result.text_accu == "一二。"
    assert "".join(result.tokens) == result.text_accu
    assert len(result.tokens) == len(result.timestamps)


def test_empty_eof_finalizes_existing_session_and_keeps_empty_file_legal():
    recognizer = _Recognizer("一", ["一"], [0.1])
    state = WorkerState()
    pipeline = _pipeline(recognizer, state=state)

    pipeline.process(_task("eof", samples=1600, is_final=False))
    result = pipeline.process(_task("eof", samples=0, is_final=True))

    assert recognizer.calls == 1
    assert result.is_final is True
    assert result.text_accu == "一"
    assert "".join(result.tokens) == result.text_accu


def test_exact_1600_sample_final_is_recognized():
    recognizer = _Recognizer("足够", ["足", "够"], [0.1, 0.2])
    result = _pipeline(recognizer).process(_task("threshold", samples=1600))

    assert recognizer.calls == 1
    assert result.is_final is True
    assert "".join(result.tokens) == result.text_accu == "足够"


def test_initial_empty_file_result_is_a_valid_final_result():
    recognizer = _Recognizer("", [], [])
    result = _pipeline(recognizer).process(_task("empty", samples=0))

    assert recognizer.calls == 0
    assert result.is_final is True
    assert result.tokens == []
    assert result.timestamps == []
    assert result.text_accu == ""


def test_file_native_timestamps_keep_text_independent_from_text_accu():
    recognizer = _Recognizer("普通回显", ["字幕"], [0.37])
    result = _pipeline(recognizer).process(_task("native"))

    assert result.text == "普通回显"
    assert result.text_accu == "字幕"
    assert "".join(result.tokens) == result.text_accu
    assert result.timestamps == [0.37]


def test_file_aligner_timestamps_join_into_final_text():
    recognizer = _Recognizer("对齐结果", [], [], native=False)

    class Aligner:
        def align(self, **kwargs):
            return SimpleNamespace(
                items=[
                    SimpleNamespace(text="对", start_time=0.37),
                    SimpleNamespace(text="齐", start_time=1.23),
                ]
            )

    result = _pipeline(recognizer, aligner=Aligner()).process(_task("aligner"))

    assert result.text == "对齐结果"
    assert result.text_accu == "对齐"
    assert result.tokens == ["对", "齐"]
    assert result.timestamps == [0.37, 1.23]


def test_mic_without_real_tokens_keeps_uniform_character_fallback():
    recognizer = _Recognizer("测试", [], [])
    result = _pipeline(recognizer).process(
        _task("mic", task_type="mic", samples=1600)
    )

    assert result.tokens == ["测", "试"]
    assert result.timestamps == [0.0, pytest.approx(0.05)]
    assert result.text_accu == "测试"


def test_mic_no_token_fallback_excludes_ordinary_spaces():
    recognizer = _Recognizer("hello world", [], [])
    result = _pipeline(recognizer).process(
        _task("mic-space", task_type="mic", samples=1600)
    )

    assert result.is_final is True
    assert result.text_accu == "hello world"
    assert result.tokens == list("helloworld")
    assert " " not in result.tokens
    assert result.timestamps == [pytest.approx(i * 0.01) for i in range(10)]


def test_mic_short_tail_and_nonfinal_keep_existing_boundaries():
    recognizer = _Recognizer("hello world", [], [])
    state = WorkerState()
    pipeline = _pipeline(recognizer, state=state)

    nonfinal = pipeline.process(
        _task("mic-bound", task_type="mic", samples=1600, is_final=False)
    )
    assert nonfinal.is_final is False
    assert recognizer.calls == 1

    short_final = pipeline.process(
        _task("mic-bound", task_type="mic", samples=1599, is_final=True)
    )
    assert recognizer.calls == 1
    assert short_final.is_final is True
    assert short_final.text_accu == "hello world"
    assert short_final.tokens == list("helloworld")
    assert " " not in short_final.tokens


def test_mic_native_tokens_are_not_rejected_by_file_join_check(monkeypatch):
    recognizer = _Recognizer("hello world", ["hello", "world"], [0.0, 0.05])
    monkeypatch.setattr(
        pipeline_module,
        "sync_tokens_from_text",
        lambda tokens, timestamps, text: (["hello"], [0.0]),
    )

    result = _pipeline(recognizer).process(
        _task("mic-native-join", task_type="mic")
    )

    assert result.is_final is True
    assert result.tokens == ["hello"]
    assert result.timestamps == [0.0]
    assert result.text_accu == "helloworld"


def test_pipeline_rejects_native_mismatched_raw_arrays_before_merge():
    recognizer = _Recognizer("错误", ["错", "误"], [0.1])

    with pytest.raises(RuntimeError, match="tokens.*timestamps"):
        _pipeline(recognizer).process(_task("raw-mismatch"))


def test_pipeline_rejects_final_sync_that_does_not_match_text_accu(monkeypatch):
    recognizer = _Recognizer("正文", ["正", "文"], [0.1, 0.2])
    monkeypatch.setattr(
        pipeline_module,
        "sync_tokens_from_text",
        lambda tokens, timestamps, text: (["错"], [0.1]),
    )

    with pytest.raises(RuntimeError, match="text_accu"):
        _pipeline(recognizer).process(_task("final-mismatch"))


def test_http_owner_uses_the_same_final_contract():
    recognizer = _Recognizer("正文", ["正", "文"], [0.1, 0.2])
    result = _pipeline(recognizer).process(
        _task("http-owner", owner_kind="http")
    )

    assert result.owner_kind == "http"
    assert "".join(result.tokens) == result.text_accu
