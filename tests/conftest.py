# coding: utf-8
"""
qwen_asr_mlx 引擎单元测试共享夹具。

核心思路：MLX 推理仅 Apple Silicon 可用且依赖第三方包 mlx-qwen3-asr，
单元测试不依赖真机 —— 向 sys.modules 注入一个假的 mlx_qwen3_asr 模块，
其 Session.transcribe 记录调用参数、返回可控结果。适配层在方法内
`from mlx_qwen3_asr import Session` 延迟导入，因此注入即生效。
"""
import asyncio
import functools
import multiprocessing
import sys
import types
from types import SimpleNamespace
import numpy as np
import pytest
import pytest_asyncio
import websockets
from config_server import ServerConfig

from core.server.connection.ws_recv import ws_recv
from core.server.connection.ws_send import ws_send
from tests.harness.server import FakeServerHarness, ObservedTaskQueue
from tests.harness.worker import run_fake_worker


def pytest_configure(config):
    from _pytest.config.findpaths import ConfigValue
    config._inicfg.setdefault(
        "faulthandler_timeout",
        ConfigValue("120", origin="file", mode="ini"),
    )
    config._inicache.pop("faulthandler_timeout", None)


class FakeResult:
    """模拟 mlx_qwen3_asr Session.transcribe 的返回对象。"""
    def __init__(self, text="你好世界。", language="Chinese", segments=None):
        self.text = text
        self.language = language
        # 段级时间戳：list[{text,start}]，初版不使用，仅供将来 TODO 验证
        self.segments = segments if segments is not None else [
            {"text": "你好", "start": 0.0},
            {"text": "世界", "start": 0.5},
        ]


class FakeSession:
    """模拟 mlx_qwen3_asr 的 Session：记录构造与 transcribe 调用。"""
    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.transcribe_calls = []  # list[dict(audio_len, kwargs, audio)]
        self.model_info = {"name": kwargs.get("model"), "fake": True}

    def transcribe(self, audio, **kwargs):
        self.transcribe_calls.append(
            {"audio_len": len(audio), "kwargs": dict(kwargs), "audio": audio}
        )
        return FakeResult()


@pytest.fixture
def fake_mlx(monkeypatch):
    """把假的 mlx_qwen3_asr 模块注入 sys.modules，返回 FakeSession 类。"""
    mod = types.ModuleType("mlx_qwen3_asr")
    mod.Session = FakeSession
    monkeypatch.setitem(sys.modules, "mlx_qwen3_asr", mod)
    return FakeSession


@pytest.fixture
def mlx_engine(fake_mlx):
    """构造一个使用假 Session 的 QwenASRMLXEngine 实例。"""
    from core.server.engines.qwen_asr_mlx.asr_engine import (
        QwenASRMLXEngine,
        MLXEngineConfig,
    )
    config = MLXEngineConfig(model="Qwen/Qwen3-ASR-0.6B", dtype=None,
                             chunk_size=80.0, verbose=False)
    return QwenASRMLXEngine(config)


@pytest_asyncio.fixture
async def fake_asr_server(request, monkeypatch):
    """启动真实 v1 WebSocket 收发层与真 Queue/TaskHandler 子进程。"""
    options = dict(getattr(request, "param", {}))
    monkeypatch.setattr(ServerConfig, "seg_cut_snap", False)
    manager = multiprocessing.Manager()
    sockets_id = manager.list()
    calls = manager.list()
    observed = manager.list()
    queue_in = multiprocessing.Queue()
    queue_out = multiprocessing.Queue()
    state = SimpleNamespace(
        queue_in=ObservedTaskQueue(queue_in, observed),
        queue_out=queue_out,
        sockets={},
        sockets_id=sockets_id,
    )
    app = SimpleNamespace(state=state)

    process = multiprocessing.Process(
        target=run_fake_worker,
        args=(queue_in, queue_out, sockets_id, options, calls),
        daemon=True,
    )
    process.start()
    server = await websockets.serve(
        functools.partial(ws_recv, app=app),
        "127.0.0.1",
        0,
        max_size=None,
        ping_interval=None,
    )
    sender = asyncio.create_task(ws_send(app))
    harness = FakeServerHarness(server, process, calls, observed)

    yield harness

    server.close()
    try:
        await asyncio.wait_for(server.wait_closed(), timeout=5)
    except TimeoutError as exc:
        raise AssertionError("WebSocket 服务关闭超过 5 秒") from exc

    queue_in.put(None)
    process.join(timeout=5)
    assert not process.is_alive(), "识别子进程在 teardown 的 5 秒 join 后仍存活"

    queue_out.put(None)
    try:
        await asyncio.wait_for(sender, timeout=5)
    except TimeoutError as exc:
        raise AssertionError("WebSocket 发送协程在 5 秒内未退出") from exc

    queue_in.close()
    queue_in._thread.join(timeout=5)
    assert not queue_in._thread.is_alive(), "queue_in feeder 5 秒内未退出"
    queue_out.close()
    queue_out._thread.join(timeout=5)
    assert not queue_out._thread.is_alive(), "queue_out feeder 5 秒内未退出"
    manager.shutdown()


def make_audio(seconds: float, sr: int = 16000) -> np.ndarray:
    """生成指定时长的 float32 静音音频。"""
    return np.zeros(int(seconds * sr), dtype=np.float32)
