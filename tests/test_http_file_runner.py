# coding: utf-8
"""HTTP 文件任务 runner：原容器解码 → 真实 worker → 持久 Result。

本文件只证明进程、队列、协议与存储契约，不冒充真实模型质量：
引擎一律是子进程里的 ProgrammableFakeEngine，音频一律由真实 ffmpeg 生成与解码。

覆盖：
  * 真实 HTTP 客户端上传完整容器 → commit → 上传客户端关闭 → 另一连接领取 DONE 与完整结果；
  * 四种真实容器（mp3/aac/m4a/opus）的真实 ffmpeg argv 与环境、段边界与样本数；
  * Task 真实跨 multiprocessing 序列化、worker Result 真实回父、result 与 DONE 同事务；
  * worker 中间/最终失败、ffmpeg 非零退出、结果超限、入队与后台未知异常；
  * SIGTERM 与崩溃窗口后的重启收敛，且不自动重跑。
"""
from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "sdk"))

from capswriter_asr import (  # noqa: E402
    AsrError,
    get_file_job_http,
    get_file_result_http,
    submit_file_http,
)

from config_server import ServerConfig  # noqa: E402
from core.server.http_server import HttpServer  # noqa: E402
from core.server.state import ServerState, make_task_key  # noqa: E402
from core.server.connection.ws_send import fail_active_tasks, ws_send  # noqa: E402
from core.server.worker.process_manager import ProcessManager  # noqa: E402
from tests.harness.worker import run_recording_worker  # noqa: E402

pytest.importorskip("aiohttp", reason="未安装 aiohttp==3.14.3；HTTP 入口默认关闭")

FFMPEG = shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(
    FFMPEG is None, reason="本机 PATH 中没有 ffmpeg，HTTP 文件解码链无法验证"
)

# 假引擎的固定输出：真实 pipeline 累计后即为持久 Result 的字段来源
FAKE_ENGINE = {
    "supports_timestamps": True,
    "result_text": "正文",
    "result_tokens": ["正", "文"],
    "result_timestamps": [0.1, 0.2],
}
CONTAINER_ARGVS = {
    "mp3": ["-c:a", "libmp3lame"],
    "aac": ["-c:a", "aac"],
    "m4a": ["-c:a", "aac", "-f", "ipod"],
    "opus": ["-c:a", "libopus"],
}


# ---------------------------------------------------------------- 真实容器样本


def tone_pcm(seconds: float, sample_rate: int = 16000) -> bytes:
    """非静音的确定性 float32 单声道 PCM（真实 producer，不是静音占位）。"""
    count = round(seconds * sample_rate)
    t = np.arange(count, dtype=np.float64) / sample_rate
    wave = 0.2 * np.sin(2 * np.pi * 220 * t) * np.sin(2 * np.pi * 0.7 * t + 0.3)
    return wave.astype("<f4").tobytes()


def make_container(tmp_path: Path, name: str, seconds: float = 20.0) -> Path:
    """用真实 ffmpeg 把 PCM 编码成可 seek 的容器文件（扩展名决定真实编码器）。"""
    raw = tmp_path / f"{name}.f32le"
    raw.write_bytes(tone_pcm(seconds))
    target = tmp_path / name
    container = target.suffix.lstrip(".")
    subprocess.run(
        [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "f32le", "-ar", "16000", "-ac", "1", "-i", str(raw),
         *CONTAINER_ARGVS[container], str(target)],
        check=True, capture_output=True,
    )
    assert target.stat().st_size > 0
    return target


def decoded_sample_count(path: Path) -> int:
    """独立跑一次真实 ffmpeg，得到该容器的真实样本数（不是按压缩体积估算）。"""
    process = subprocess.run(
        [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error",
         "-i", str(path), "-ar", "16000", "-ac", "1", "-f", "f32le", "pipe:1"],
        check=True, capture_output=True,
    )
    assert len(process.stdout) % 4 == 0
    return len(process.stdout) // 4


def install_recording_ffmpeg(tmp_path: Path, monkeypatch) -> Path:
    """把一个真 ffmpeg 包装脚本放到 PATH 首位，记录真实 argv/env 后跑真 ffmpeg。

    记录由真实子进程产生：start 记 argv/env，end 记真实退出码，两者都是追加事件，
    可用于算出真实并发解码数。包装脚本不在 PATH 首位时 runner 不会经过它，
    记录文件为空会让断言变红，因此该证据不会被绕过。
    """
    bindir = tmp_path / "ffmpeg-shim"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "ffmpeg-invocations.jsonl"
    script = bindir / "ffmpeg"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, subprocess, sys\n"
        f"RECORD = {str(log)!r}\n"
        f"REAL = {FFMPEG!r}\n"
        "entry = {\n"
        "    'event': 'start',\n"
        "    'argv': list(sys.argv),\n"
        "    'env_marker': os.environ.get('CW_TEST_ENV_MARKER'),\n"
        "    'path_head': (os.environ.get('PATH') or '').split(os.pathsep)[0],\n"
        "}\n"
        "with open(RECORD, 'a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps(entry, ensure_ascii=False) + '\\n')\n"
        "proc = subprocess.run([REAL] + sys.argv[1:])\n"
        "with open(RECORD, 'a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps({'event': 'end', 'rc': proc.returncode}) + '\\n')\n"
        "raise SystemExit(proc.returncode)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CW_TEST_ENV_MARKER", "runner-env-marker")
    return log


def read_ffmpeg_invocations(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]


def read_ffmpeg_starts(log: Path) -> list[dict]:
    return [entry for entry in read_ffmpeg_invocations(log) if entry.get("event") == "start"]


def ffmpeg_peak_concurrency(log: Path) -> int:
    """从真实子进程追加的 start/end 事件算同时活跃的解码进程数。"""
    current = peak = 0
    for entry in read_ffmpeg_invocations(log):
        if entry.get("event") == "start":
            current += 1
            peak = max(peak, current)
        elif entry.get("event") == "end":
            current -= 1
    return peak


# ---------------------------------------------------------------- 服务端骨架


class RunnerHarness:
    def __init__(self, *, http_server, state, base_url, port, worker, received, calls, manager):
        self.http_server = http_server
        self.state = state
        self.base_url = base_url
        self.port = port
        self.worker = worker
        self.received = received
        self.calls = calls
        self.manager = manager
        self.data_dir = http_server.data_dir

    def tasks(self, job_id: str) -> list[dict]:
        return [
            entry for entry in self.received
            if entry.get("event") == "task" and entry.get("task_id") == job_id
        ]

    def results(self, job_id: str) -> list[dict]:
        return [
            entry for entry in self.received
            if entry.get("event") == "result" and entry.get("task_id") == job_id
        ]

    def read_db(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        conn = sqlite3.connect(f"file:{self.data_dir / 'http.sqlite3'}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()


@asynccontextmanager
async def running_runner_server(tmp_path: Path, *, options=None):
    """port 0 上的真实 HTTP listener + 真实 runner + 真 ws_send + 真识别子进程。"""
    loop = asyncio.get_running_loop()
    previous_cut_snap = ServerConfig.seg_cut_snap
    ServerConfig.seg_cut_snap = False
    manager = multiprocessing.Manager()
    queue_in = multiprocessing.Queue()
    queue_out = multiprocessing.Queue()
    state = ServerState(queue_in=queue_in, queue_out=queue_out)
    state.sockets_id = manager.list()
    state.active_http_jobs = manager.list()
    received = manager.list()
    calls = manager.list()
    app = SimpleNamespace(state=state, loop=loop)
    worker = multiprocessing.Process(
        target=run_recording_worker,
        args=(
            queue_in, queue_out, state.sockets_id, state.active_http_jobs,
            dict(options or FAKE_ENGINE), calls, received,
        ),
        daemon=True,
    )
    worker.start()
    state.recognize_process = worker
    process_manager = ProcessManager(app)
    process_manager._process = worker
    process_manager.is_alive = True
    app.process_manager = process_manager
    await asyncio.to_thread(process_manager._wait_for_models)
    assert process_manager.models_ready, "识别子进程未在期限内完成模型就绪负载"

    http_server = HttpServer(app, "127.0.0.1", 0, tmp_path / "httpdata")
    http_server.prepare()
    from core.server.http_file_runner import HttpFileRunner

    runner = HttpFileRunner(state, http_server)
    http_server.attach_runner(runner)
    state.http_result_sink = runner.result_sink
    # 镜像生产 Application：终态收尾（finalize_http_job）从 state.app 取真实 runner
    app.http_file_runner = runner
    state.app = app
    await http_server._runner.setup()
    site = http_server._web.TCPSite(http_server._runner, "127.0.0.1", 0)
    await site.start()
    http_server._site = site
    port = site._server.sockets[0].getsockname()[1]
    sender = asyncio.create_task(ws_send(app))
    monitor = asyncio.create_task(process_manager.monitor())
    harness = RunnerHarness(
        http_server=http_server, state=state, base_url=f"http://127.0.0.1:{port}",
        port=port, worker=worker, received=received, calls=calls, manager=manager,
    )
    try:
        yield harness
    finally:
        for task in (sender, monitor):
            task.cancel()
        await asyncio.gather(sender, monitor, return_exceptions=True)
        await http_server.stop()
        queue_in.put(None)
        await asyncio.to_thread(worker.join, 5)
        assert not worker.is_alive(), "识别子进程在 teardown 的 5 秒 join 后仍存活"
        queue_out.put(None)
        for queue in (queue_in, queue_out):
            queue.close()
            queue._thread.join(timeout=5)
            assert not queue._thread.is_alive(), f"{queue} feeder 线程 5 秒内未退出"
        manager.shutdown()
        ServerConfig.seg_cut_snap = previous_cut_snap


async def wait_terminal(harness: RunnerHarness, recovery: Path, timeout: float = 60.0):
    """另一连接（新建 HTTP 客户端）轮询到终态。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    status = None
    while loop.time() < deadline:
        status = await get_file_job_http(harness.base_url, resume_path=recovery)
        if status.state in {"DONE", "FAILED"}:
            return status
        await asyncio.sleep(0.02)
    raise AssertionError(f"任务未在 {timeout}s 内到达终态，最后状态={status}")


async def wait_state(harness: RunnerHarness, recovery: Path, expected: str, timeout: float = 30.0):
    """另一连接轮询到指定状态（识别进行中观察 RUNNING 用）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    status = None
    while loop.time() < deadline:
        status = await get_file_job_http(harness.base_url, resume_path=recovery)
        if status.state == expected:
            return status
        if status.state in {"DONE", "FAILED"}:
            raise AssertionError(f"任务先到达终态 {status.state}，未观察到 {expected}")
        await asyncio.sleep(0.02)
    raise AssertionError(f"任务未在 {timeout}s 内到达 {expected}，最后状态={status}")


async def submit(harness: RunnerHarness, source: Path, recovery: Path, **options):
    """真实 SDK 客户端：上传完整容器并 commit，返回后上传客户端已关闭。"""
    return await submit_file_http(
        source, harness.base_url, resume_path=recovery,
        chunk_bytes=64 * 1024, **options,
    )


async def raw_get(harness: RunnerHarness, recovery: Path, suffix: str = "") -> httpx.Response:
    """另一条裸 HTTP 连接直接读取响应体（不经过 SDK 的字段裁剪）。"""
    payload = json.loads(recovery.read_text(encoding="utf-8"))
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        return await client.get(
            f"{harness.base_url}/v1/jobs/{payload['job_id']}{suffix}",
            headers={"Authorization": f"Bearer {payload['token']}"},
        )


async def raw_job(harness, recovery: Path) -> dict:
    """重启后端口会变（SDK 恢复文件绑定 base_url），因此直接用裸 HTTP 连接读任务状态。"""
    payload = json.loads(recovery.read_text(encoding="utf-8"))
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        response = await client.get(
            f"{harness.base_url}/v1/jobs/{payload['job_id']}",
            headers={"Authorization": f"Bearer {payload['token']}"},
        )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------- 跨边界主路径


@pytest.mark.asyncio
@pytest.mark.parametrize("container", list(CONTAINER_ARGVS))
async def test_real_container_upload_then_other_connection_takes_done_result(
    tmp_path, monkeypatch, container
):
    """四种真实容器：上传客户端关闭后，另一连接拿到 DONE 与完整持久结果。"""
    ffmpeg_log = install_recording_ffmpeg(tmp_path, monkeypatch)
    source = make_container(tmp_path, f"speech.{container}")
    expected_samples = decoded_sample_count(source)
    recovery = tmp_path / "resume.json"

    async with running_runner_server(tmp_path) as harness:
        handle = await submit(
            harness, source, recovery, seg_duration=5.0, seg_overlap=1.0
        )
        job_id = handle.job_id
        status = await wait_terminal(harness, recovery)
        assert status.state == "DONE", status
        assert status.result_available is True
        assert status.error_code is None
        transcript = await get_file_result_http(harness.base_url, resume_path=recovery)

        # 1. 持久结果 = worker 真实发出的最终 Result 字段（不是测试手造 dict）
        finals = [item for item in harness.results(job_id) if item["is_final"]]
        assert len(finals) == 1, harness.results(job_id)
        emitted = finals[0]
        assert emitted["owner_kind"] == "http" and emitted["socket_id"] == ""
        assert emitted["type"] == "file"
        assert transcript.raw["task_id"] == job_id
        assert transcript.raw["type"] == "file"
        assert transcript.raw["owner_kind"] == "http"
        assert transcript.raw["socket_id"] == ""
        assert transcript.raw["is_final"] is True
        for field in ("text", "text_accu", "duration"):
            assert transcript.raw[field] == emitted[field], field
        assert transcript.tokens == emitted["tokens"]
        assert transcript.timestamps == emitted["timestamps"]
        assert transcript.text_accu == "".join(transcript.tokens)
        assert len(transcript.tokens) == len(transcript.timestamps)
        assert transcript.tokens, "文件任务必须落真实 token，不能为空正文"
        for field in ("duration", "time_start", "time_submit", "time_complete"):
            assert isinstance(transcript.raw[field], float)

        # 2. result 与 DONE 同事务：终态行与结果行同时存在，且都指向同一任务
        job_row = harness.read_db(
            "SELECT state, error_code, time_complete, terminal_at FROM jobs WHERE job_id=?",
            (job_id,),
        )[0]
        assert job_row["state"] == "DONE" and job_row["error_code"] is None
        assert job_row["time_complete"] is not None and job_row["terminal_at"] is not None
        stored = harness.read_db("SELECT payload FROM results WHERE job_id=?", (job_id,))
        assert len(stored) == 1
        assert json.loads(stored[0]["payload"]) == transcript.raw

        # 3. Task 真实跨进程：owner 字段、段边界与最终段
        submitted = harness.tasks(job_id)
        assert submitted, "识别子进程没有收到任何 HTTP 段"
        assert all(item["owner_kind"] == "http" for item in submitted)
        assert all(item["socket_id"] == "" for item in submitted)
        assert all(item["type"] == "file" for item in submitted)
        assert [item["offset"] for item in submitted] == sorted(
            item["offset"] for item in submitted
        )
        assert submitted[-1]["is_final"] is True
        assert sum(1 for item in submitted if item["is_final"]) == 1
        assert all(item["samplerate"] == 16000 for item in submitted)
        # 末段样本数 + 各段（去掉重叠）必须正好覆盖解码出的真实样本数；
        # 后一段的 offset 已含上一段重叠，因此不再重复加重叠
        covered = submitted[-1]["samples"]
        for index in range(1, len(submitted)):
            previous = submitted[index - 1]
            item = submitted[index]
            # 段尾带 overlap，所以后一段的 offset = 前段 offset + (前段样本 - 前段重叠)
            expected_offset = (
                previous["offset"]
                + (previous["samples"] - round(previous["overlap"] * 16000)) / 16000
            )
            assert item["offset"] == pytest.approx(expected_offset, abs=1e-6)
            covered += previous["samples"] - round(previous["overlap"] * 16000)
        assert covered == expected_samples, (covered, expected_samples)
        assert submitted[-1]["samples"] <= 16000 * 7

        # 4. 真实 ffmpeg argv：固定 exec、无 shell、只读服务端自己的源文件
        invocations = read_ffmpeg_starts(ffmpeg_log)
        assert invocations, "ffmpeg 包装脚本没有被执行，argv 证据缺失"
        entry = invocations[-1]
        assert entry["path_head"] == str(tmp_path / "ffmpeg-shim")
        assert entry["env_marker"] == "runner-env-marker"
        argv = entry["argv"]
        assert argv[1:] == [
            "-nostdin", "-hide_banner", "-loglevel", "error", "-i",
            str(harness.data_dir / "sources" / f"{handle.upload_id}.bin"),
            "-ar", "16000", "-ac", "1", "-f", "f32le", "pipe:1",
        ]

        # 5. 没有临时 PCM 文件，源引用与活跃 owner 都已释放
        assert sorted(p.name for p in harness.data_dir.iterdir()) == [
            "http.lock", "http.sqlite3", "sources",
        ] or all(
            name.startswith("http.") or name == "sources"
            for name in (p.name for p in harness.data_dir.iterdir())
        )
        assert harness.state.tasks == {}
        assert list(harness.state.active_http_jobs) == []
        assert harness.http_server.fatal is None

    # 子进程退出后落盘内容不变（result 与 DONE 已经真正提交）
    conn = sqlite3.connect(tmp_path / "httpdata" / "http.sqlite3")
    try:
        assert conn.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE state='DONE'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_running_job_result_is_not_ready_and_exposes_nothing(tmp_path):
    """识别进行中：GET job 是持久化的 RUNNING，GET result 是 409 result_not_ready。"""
    stalled = dict(FAKE_ENGINE, delay_on_call=1, delay_seconds=3.0)
    source = make_container(tmp_path, "speech.mp3", seconds=20.0)
    recovery = tmp_path / "resume.json"
    async with running_runner_server(tmp_path, options=stalled) as harness:
        handle = await submit(harness, source, recovery, seg_duration=5.0, seg_overlap=1.0)
        # 识别进行中从另一连接观察：RUNNING 必须来自 SQLite（唯一真源），
        # 而不是 route 里的内存态或推断
        status = await wait_state(harness, recovery, "RUNNING")
        assert status.result_available is False
        row = harness.read_db(
            "SELECT state, started_at FROM jobs WHERE job_id=?", (handle.job_id,)
        )[0]
        assert row["state"] == "RUNNING"
        assert row["started_at"] is not None, "RUNNING 必须带 started_at"
        response = await raw_get(harness, recovery, "/result")
        assert response.status_code == 409
        body = response.json()
        assert body["code"] == "result_not_ready"
        assert "tokens" not in body and "text_accu" not in body
        with pytest.raises(AsrError) as info:
            await get_file_result_http(harness.base_url, resume_path=recovery)
        assert info.value.code == "result_not_ready"
        assert harness.read_db("SELECT COUNT(*) AS n FROM results")[0]["n"] == 0
        # 终态后 RUNNING 不回退：真实收尾后才允许离开运行态
        assert (await wait_terminal(harness, recovery)).state == "DONE"
        final = harness.read_db(
            "SELECT state, error_code FROM jobs WHERE job_id=?", (handle.job_id,)
        )[0]
        assert final["state"] == "DONE" and final["error_code"] is None


@pytest.mark.asyncio
async def test_worker_failure_fails_job_and_result_exposes_error_code(tmp_path):
    """worker 中途失败：整 Job FAILED，GET result 带出已存 error_code。"""
    source = make_container(tmp_path, "speech.mp3")
    recovery = tmp_path / "resume.json"
    failing = dict(FAKE_ENGINE, fail_on_call=2)
    async with running_runner_server(tmp_path, options=failing) as harness:
        await submit(harness, source, recovery, seg_duration=5.0, seg_overlap=1.0)
        status = await wait_terminal(harness, recovery)
        assert status.state == "FAILED"
        assert status.result_available is False
        assert status.error_code == "inference_failed"
        response = await raw_get(harness, recovery, "/result")
        assert response.status_code == 409
        body = response.json()
        assert body["code"] == "job_failed"
        assert body["error_code"] == "inference_failed"
        # 没有结果行，也没有终态 DONE：失败任务绝不留下半成品结果
        assert harness.read_db("SELECT COUNT(*) AS n FROM results")[0]["n"] == 0
        job_row = harness.read_db(
            "SELECT state, error_code FROM jobs WHERE error_code='inference_failed'"
        )
        assert len(job_row) == 1 and job_row[0]["state"] == "FAILED"
        # 失败后不再有新结果覆盖
        after = len(harness.results(status.job_id))
        await asyncio.sleep(0.2)
        assert len(harness.results(status.job_id)) == after
        assert list(harness.state.active_http_jobs) == []


@pytest.mark.asyncio
async def test_ffmpeg_nonzero_exit_fails_job_with_decode_failed(tmp_path):
    """真实 ffmpeg 非零退出：整 Job FAILED[decode_failed]，不静默受理。"""
    broken = tmp_path / "broken.mp3"
    broken.write_bytes(bytes((index * 31 + 7) % 251 for index in range(4096)))
    recovery = tmp_path / "resume.json"
    async with running_runner_server(tmp_path) as harness:
        await submit(harness, broken, recovery)
        status = await wait_terminal(harness, recovery)
        assert status.state == "FAILED"
        assert status.error_code == "decode_failed"
        response = await raw_get(harness, recovery, "/result")
        assert response.status_code == 409
        assert response.json()["error_code"] == "decode_failed"
        assert harness.read_db("SELECT COUNT(*) AS n FROM results")[0]["n"] == 0
        assert harness.tasks(status.job_id) == []


@pytest.mark.asyncio
async def test_result_over_limit_fails_job_while_normal_limit_succeeds(tmp_path, monkeypatch):
    """结果超限按真实上限判定：同样输入在真实 64 MiB 下成功、收紧后立刻 FAILED。"""
    from core.server import http_file_runner as runner_module

    source = make_container(tmp_path, "speech.mp3")
    recovery = tmp_path / "resume.json"
    async with running_runner_server(tmp_path) as harness:
        await submit(harness, source, recovery, seg_duration=5.0, seg_overlap=1.0)
        status = await wait_terminal(harness, recovery)
        assert status.state == "DONE", status
        transcript = await get_file_result_http(harness.base_url, resume_path=recovery)
        normal_size = len(json.dumps(transcript.raw, ensure_ascii=False).encode("utf-8"))

    tiny = tmp_path / "tiny"
    tiny.mkdir()
    bounded = make_container(tiny, "speech.mp3")
    monkeypatch.setattr(runner_module, "MAX_RESULT_BYTES", 16)
    async with running_runner_server(tiny) as harness:
        recovery = tiny / "resume.json"
        await submit(harness, bounded, recovery, seg_duration=5.0, seg_overlap=1.0)
        status = await wait_terminal(harness, recovery)
        assert status.state == "FAILED"
        assert status.error_code == "result_too_large"
        assert normal_size > 16, "对照用例必须本来就能装下，否则超限断言没有约束力"
        assert harness.read_db("SELECT COUNT(*) AS n FROM results")[0]["n"] == 0
        assert harness.http_server.fatal is None


@pytest.mark.asyncio
async def test_record_result_rejects_tokens_text_accu_mismatch(tmp_path):
    """tokens 拼接与 text_accu 不一致必须被拒（invalid_result），且不留半成品。

    正对照先证明同样形状的 payload 在一致时真的能被接受，避免这条断言因为别的
    字段不合规而“顺带变红”。
    """
    from core.server.http_file_runner import http_result_payload
    from core.server.http_store import HttpStoreError
    from core.server.schema import Result

    stalled = dict(FAKE_ENGINE, delay_on_call=1, delay_seconds=3.0)
    source = make_container(tmp_path, "speech.mp3")
    async with running_runner_server(tmp_path, options=stalled) as harness:
        ok_recovery = tmp_path / "resume_ok.json"
        bad_recovery = tmp_path / "resume_bad.json"
        ok_handle = await submit(
            harness, source, ok_recovery, seg_duration=5.0, seg_overlap=1.0
        )
        bad_handle = await submit(
            harness, source, bad_recovery, seg_duration=5.0, seg_overlap=1.0
        )

        def payload_for(job_id: str, text_accu: str) -> dict:
            # 用真实 producer 函数构造 payload，只改 text_accu 这一个字段
            return http_result_payload(Result(
                task_id=job_id, socket_id="", type="file", owner_kind="http",
                duration=20.0, time_start=1.0, time_submit=2.0, time_complete=3.0,
                text="正文", text_accu=text_accu,
                tokens=["正", "文"], timestamps=[0.1, 0.2], is_final=True,
            ))

        await harness.http_server.record_result(
            ok_handle.job_id, payload_for(ok_handle.job_id, "正文")
        )
        accepted = harness.read_db(
            "SELECT state FROM jobs WHERE job_id=?", (ok_handle.job_id,)
        )[0]
        assert accepted["state"] == "DONE", "拼接一致的 payload 必须能被接受，否则反例无效"

        with pytest.raises(HttpStoreError) as info:
            await harness.http_server.record_result(
                bad_handle.job_id, payload_for(bad_handle.job_id, "正文不一致")
            )
        assert info.value.code == "invalid_result"
        assert info.value.status == 422
        rejected = harness.read_db(
            "SELECT state, error_code FROM jobs WHERE job_id=?", (bad_handle.job_id,)
        )[0]
        assert rejected["state"] in {"QUEUED", "RUNNING"}
        assert rejected["error_code"] is None
        assert harness.read_db(
            "SELECT COUNT(*) AS n FROM results WHERE job_id=?", (bad_handle.job_id,)
        )[0]["n"] == 0


@pytest.mark.asyncio
async def test_repeated_commit_returns_same_job_without_resubmitting(tmp_path):
    """重复 commit 返回同一 Job 且不重投：段数不增加，也不新建 Job。"""
    source = make_container(tmp_path, "speech.mp3")
    recovery = tmp_path / "resume.json"
    async with running_runner_server(tmp_path) as harness:
        handle = await submit(harness, source, recovery, seg_duration=5.0, seg_overlap=1.0)
        status = await wait_terminal(harness, recovery)
        assert status.state == "DONE"
        before = len(harness.tasks(handle.job_id))
        payload = json.loads(recovery.read_text(encoding="utf-8"))
        headers = {"Authorization": f"Bearer {payload['token']}", "Content-Length": "0"}
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            first = await client.post(
                f"{harness.base_url}/v1/uploads/{payload['upload_id']}/commit", headers=headers
            )
            second = await client.post(
                f"{harness.base_url}/v1/uploads/{payload['upload_id']}/commit", headers=headers
            )
        assert first.status_code == 200 and second.status_code == 200
        assert first.json()["job_id"] == second.json()["job_id"] == handle.job_id
        await asyncio.sleep(0.2)
        assert len(harness.tasks(handle.job_id)) == before
        assert harness.read_db("SELECT COUNT(*) AS n FROM jobs")[0]["n"] == 1
        assert harness.read_db("SELECT COUNT(*) AS n FROM results")[0]["n"] == 1


@pytest.mark.asyncio
async def test_global_gate_runs_one_http_job_at_a_time(tmp_path, monkeypatch):
    """任一时刻最多 1 个 HTTP Job 在解码，其余保持 QUEUED 直到前一个落终态。

    闸门被持有的证据来自真实子进程：把第一个 Job 的终态写入（真实受监督 I/O 入口）
    卡在闸门释放之前，此时其余 Job 必须是 QUEUED 且只有一个 ffmpeg 在跑。
    """
    ffmpeg_log = install_recording_ffmpeg(tmp_path, monkeypatch)
    sources = [make_container(tmp_path, f"gate{index}.mp3") for index in range(3)]
    async with running_runner_server(tmp_path) as harness:
        entered = asyncio.Event()
        release_terminal = asyncio.Event()
        real_record_result = harness.http_server.record_result

        async def blocked_record_result(job_id, payload):
            # 真实终态写入之前卡住：此时 Job 未落库，闸门必须仍被持有
            entered.set()
            await release_terminal.wait()
            return await real_record_result(job_id, payload)

        monkeypatch.setattr(
            harness.http_server, "record_result", blocked_record_result
        )
        recoveries = [tmp_path / f"gate_resume{index}.json" for index in range(3)]
        handles = [
            await submit(harness, source, recovery, seg_duration=5.0, seg_overlap=1.0)
            for source, recovery in zip(sources, recoveries)
        ]
        assert await asyncio.wait_for(entered.wait(), 60), "没有任何 Job 走到终态写入"

        states = {
            handle.job_id: (await get_file_job_http(harness.base_url, resume_path=recovery)).state
            for handle, recovery in zip(handles, recoveries)
        }
        running = [job_id for job_id, state in states.items() if state == "RUNNING"]
        assert len(running) == 1, states
        assert all(
            state == "QUEUED" for job_id, state in states.items() if job_id != running[0]
        ), states
        # 真实解码进程数：闸门持有期间只应有 1 个 ffmpeg 在跑
        assert ffmpeg_peak_concurrency(ffmpeg_log) == 1, read_ffmpeg_invocations(ffmpeg_log)
        assert not harness.http_server.fatal

        # 重复 commit 不会让同一 Job 多占一个闸门位（也不会多解码一次）
        payload = json.loads(recoveries[0].read_text(encoding="utf-8"))
        headers = {"Authorization": f"Bearer {payload['token']}", "Content-Length": "0"}
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            repeat = await client.post(
                f"{harness.base_url}/v1/uploads/{payload['upload_id']}/commit", headers=headers
            )
        assert repeat.status_code == 200
        assert repeat.json()["job_id"] == running[0]

        release_terminal.set()
        for handle, recovery in zip(handles, recoveries):
            status = await wait_terminal(harness, recovery, timeout=120)
            assert status.state == "DONE", (handle.job_id, status)

        starts = read_ffmpeg_starts(ffmpeg_log)
        assert len(starts) == 3, f"每个 Job 只应解码一次：{len(starts)}"
        assert ffmpeg_peak_concurrency(ffmpeg_log) == 1, read_ffmpeg_invocations(ffmpeg_log)
        # 闸门在终态可靠落库之后才释放：三个 Job 各自都留下完整结果
        assert harness.read_db("SELECT COUNT(*) AS n FROM jobs WHERE state='DONE'")[0]["n"] == 3
        assert harness.read_db("SELECT COUNT(*) AS n FROM results")[0]["n"] == 3
        assert list(harness.state.active_http_jobs) == []
        assert harness.state.tasks == {}


@pytest.mark.asyncio
async def test_concurrent_jobs_keep_per_job_fifo_and_finish(tmp_path):
    """多个 Job 并发：各自段序 FIFO、互不串扰，全部 DONE 且结果各自自洽。"""
    sources = [make_container(tmp_path, f"speech{index}.mp3") for index in range(3)]
    async with running_runner_server(tmp_path) as harness:
        handles = []
        for index, source in enumerate(sources):
            recovery = tmp_path / f"resume{index}.json"
            handles.append((await submit(
                harness, source, recovery, seg_duration=5.0, seg_overlap=1.0
            ), recovery))
        for handle, recovery in handles:
            status = await wait_terminal(harness, recovery)
            assert status.state == "DONE", (handle.job_id, status)
            transcript = await get_file_result_http(harness.base_url, resume_path=recovery)
            assert transcript.text_accu == "".join(transcript.tokens)
            offsets = [item["offset"] for item in harness.tasks(handle.job_id)]
            assert offsets == sorted(offsets)
            finals = [item for item in harness.results(handle.job_id) if item["is_final"]]
            assert len(finals) == 1 and finals[0]["task_id"] == handle.job_id
        assert harness.read_db("SELECT COUNT(*) AS n FROM jobs WHERE state='DONE'")[0]["n"] == 3
        assert harness.read_db("SELECT COUNT(*) AS n FROM results")[0]["n"] == 3
        assert harness.http_server.fatal is None


# ---------------------------------------------------------------- 信号与进程边界


@pytest.mark.asyncio
async def test_sigterm_exits_zero_and_restart_marks_server_restarted(tmp_path):
    """SIGTERM：0 退出并有界清理；重启后旧 Job 是 FAILED[server_restarted]，不重跑。"""
    from tests.harness.server import ManagedHttpServerHarness

    data_dir = tmp_path / "httpdata"
    stalled = dict(FAKE_ENGINE, delay_on_call=1, delay_seconds=60.0)
    harness = await ManagedHttpServerHarness.start(
        data_dir=data_dir, options=stalled
    )
    try:
        source = make_container(tmp_path, "speech.mp3")
        recovery = tmp_path / "resume.json"
        handle = await submit(harness, source, recovery, seg_duration=5.0, seg_overlap=1.0)
        job_id = handle.job_id
        # 识别确实已经开始（RUNNING）才发信号：重启收敛要覆盖真实在跑的 Job
        assert (await wait_state(harness, recovery, "RUNNING")).state == "RUNNING"
        await harness.terminate(signal.SIGTERM, timeout=20)
    finally:
        await harness.cleanup()

    assert harness.exitcode == 0, harness.stderr_tail()

    # 重启：同一数据目录，旧 Job 被收敛为 server_restarted，不自动重跑
    restarted = await ManagedHttpServerHarness.start(data_dir=data_dir, options=stalled)
    try:
        status = await raw_job(restarted, recovery)
        assert status["state"] == "FAILED"
        assert status["error_code"] == "server_restarted"
        assert status["result_available"] is False
        await asyncio.sleep(0.3)
        assert list(restarted.received) == [], f"重启后不得自动重跑推理: {list(restarted.received)!r}"
    finally:
        await restarted.stop()
        await restarted.cleanup()


@pytest.mark.asyncio
async def test_crash_window_restart_keeps_failed_server_restarted(tmp_path):
    """崩溃窗口（SIGKILL）：旧 Job 仍是 FAILED[server_restarted]，且不重跑。"""
    from tests.harness.server import ManagedHttpServerHarness

    data_dir = tmp_path / "httpdata"
    stalled = dict(FAKE_ENGINE, delay_on_call=1, delay_seconds=60.0)
    harness = await ManagedHttpServerHarness.start(data_dir=data_dir, options=stalled)
    try:
        source = make_container(tmp_path, "speech.mp3")
        recovery = tmp_path / "resume.json"
        handle = await submit(harness, source, recovery, seg_duration=5.0, seg_overlap=1.0)
        assert (await wait_state(harness, recovery, "RUNNING")).state == "RUNNING"
        await harness.terminate(signal.SIGKILL, timeout=20)
    finally:
        await harness.cleanup()
    assert harness.exitcode != 0

    restarted = await ManagedHttpServerHarness.start(data_dir=data_dir, options=stalled)
    try:
        status = await raw_job(restarted, recovery)
        assert status["state"] == "FAILED"
        assert status["error_code"] == "server_restarted"
        assert handle.job_id
        await asyncio.sleep(0.3)
        assert list(restarted.received) == [], f"重启后不得自动重跑推理: {list(restarted.received)!r}"
        row = restarted.read_db("SELECT COUNT(*) AS n FROM results")[0]["n"]
        assert row == 0
    finally:
        await restarted.stop()
        await restarted.cleanup()


@pytest.mark.asyncio
async def test_segment_timeout_persists_failed_before_exit(tmp_path):
    """推理段超时：进程退出前 Job 已可靠落库 FAILED[inference_timeout]，错因不丢。"""
    from tests.harness.server import ManagedHttpServerHarness

    stalled = dict(FAKE_ENGINE, delay_on_call=1, delay_seconds=60.0)
    harness = await ManagedHttpServerHarness.start(
        data_dir=tmp_path / "httpdata", options=stalled,
        env={"CW_SEGMENT_TIMEOUT": "5"},
    )
    try:
        source = make_container(tmp_path, "speech.mp3")
        recovery = tmp_path / "resume.json"
        handle = await submit(harness, source, recovery, seg_duration=5.0, seg_overlap=1.0)
        job_id = handle.job_id
        assert (await wait_state(harness, recovery, "RUNNING")).state == "RUNNING"
        # 边轮询真实库边等进程退出：FAILED 必须发生在进程还活着的时候，
        # 不能只靠重启后的 server_restarted 反推。先采样、后判活：提交严格
        # 先于死亡，最后一份样本必然落在提交之后（先判活会被死亡检测的毫秒级
        # 延迟漏掉「提交 → 死亡」之间的窄窗口）
        observations = []
        while True:
            rows = harness.read_db(
                "SELECT state, error_code FROM jobs WHERE job_id=?", (job_id,)
            )
            if rows:
                observations.append((rows[0]["state"], rows[0]["error_code"]))
            if not harness.process.is_alive():
                break
            await asyncio.sleep(0.01)
        await harness.wait_for_exit(30)
    finally:
        await harness.cleanup()

    assert harness.exitcode != 0, "推理段超时仍必须非零退出"
    failed_before_exit = [item for item in observations if item[0] == "FAILED" and item[1]]
    assert failed_before_exit, (
        f"进程退出前未观察到已落库的 FAILED（error_code 为空）：{observations[-6:]}"
    )
    assert {item[1] for item in failed_before_exit} == {"inference_timeout"}
    row = harness.read_db(
        "SELECT state, error_code FROM jobs WHERE job_id=?", (job_id,)
    )[0]
    assert row["state"] == "FAILED" and row["error_code"] == "inference_timeout"
    assert harness.read_db("SELECT COUNT(*) AS n FROM results")[0]["n"] == 0


# ---------------------------------------------------------------- R4 释放不变式探针


async def _wait_engine_calls(harness: RunnerHarness, count: int, timeout: float = 30.0):
    """等识别子进程真实进到第 N 次引擎调用（ stalled 引擎已开工的证据）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while len(harness.calls) < count:
        if loop.time() >= deadline:
            raise AssertionError(
                f"假引擎调用未达到 {count} 次：{list(harness.calls)!r}"
            )
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["fail_active_tasks", "segment_timeout"])
async def test_http_owner_released_before_persist(tmp_path, monkeypatch, trigger):
    """R4：FAILED 持久写被卡住时，owner/闸门必须仍被持有（先落库，后释放）。

    两条历史上违规的路径参数化：
      * ``fail_active_tasks`` —— worker 崩溃/收尾时给 HTTP 任务排错（原实现
        只做内存态 transition_terminal，整条缺失落库）；进程内直调，触发后
        进程仍存活，因此还能验证「落库完成后闸门交给第二个 Job」的完整排水；
      * ``segment_timeout`` —— 监控协程的推理段超时 HTTP 分支（原实现先释放
        owner、后落库，顺序颠倒）；该路径随后按语义 SystemExit(1) 非零退出，
        asyncio 会把 task 里的 SystemExit 重抛进事件循环，因此必须走真实
        独立服务子进程（ManagedHttpServerHarness），卡点用标记文件跨进程传递。

    把真实受监督 I/O 入口（HttpServer.fail_job）卡住，在卡住窗口内反复断言：
    第一个 Job 在库中仍是 RUNNING（失败事实尚未落库）、第二个已并发提交的
    Job 仍是 QUEUED 且没有第二个 ffmpeg 解码进程；进程内参数还断言 owner
    仍在 active_http_jobs、内存记录未终态。任一提前释放的形态都会让探针
    立刻带着证据变红。
    """
    if trigger == "fail_active_tasks":
        await _probe_release_order_fail_active_tasks(tmp_path, monkeypatch)
    else:
        await _probe_release_order_segment_timeout(tmp_path, monkeypatch)


async def _probe_release_order_fail_active_tasks(tmp_path, monkeypatch):
    """fail_active_tasks 路径：进程内直调，卡住 FAILED 持久写验证 owner/闸门。"""
    ffmpeg_log = install_recording_ffmpeg(tmp_path, monkeypatch)
    stalled = dict(FAKE_ENGINE, delay_on_call=1, delay_seconds=5.0)
    source_a = make_container(tmp_path, "probe_a.mp3")
    source_b = make_container(tmp_path, "probe_b.mp3")

    async with running_runner_server(tmp_path, options=stalled) as harness:
        entered = asyncio.Event()
        release_persist = asyncio.Event()
        real_fail_job = harness.http_server.fail_job

        async def blocked_fail_job(job_id, error_code):
            # 真实 FAILED 持久写之前卡住：失败事实尚未落库
            entered.set()
            await release_persist.wait()
            return await real_fail_job(job_id, error_code)

        monkeypatch.setattr(harness.http_server, "fail_job", blocked_fail_job)
        recovery_a = tmp_path / "probe_a.json"
        recovery_b = tmp_path / "probe_b.json"
        handle_a = await submit(
            harness, source_a, recovery_a, seg_duration=5.0, seg_overlap=1.0
        )
        handle_b = await submit(
            harness, source_b, recovery_b, seg_duration=5.0, seg_overlap=1.0
        )
        job_a, job_b = handle_a.job_id, handle_b.job_id
        assert (await wait_state(harness, recovery_a, "RUNNING")).state == "RUNNING"
        await _wait_engine_calls(harness, 1)

        trigger_task = asyncio.create_task(
            fail_active_tasks(harness.state, "internal", "R4 探针：释放前必须先落库")
        )

        async def sample() -> dict:
            row = harness.read_db(
                "SELECT state, error_code FROM jobs WHERE job_id=?", (job_a,)
            )[0]
            record = harness.state.tasks.get(make_task_key("http", job_a))
            b_status = await get_file_job_http(
                harness.base_url, resume_path=recovery_b
            )
            return {
                "a_db": (row["state"], row["error_code"]),
                "a_owner": job_a in list(harness.state.active_http_jobs),
                "a_record_terminal": (
                    record is None or record.status in {"DONE", "FAILED"}
                ),
                "b_state": b_status.state,
                "decode_starts": len(read_ffmpeg_starts(ffmpeg_log)),
            }

        def invariant(s: dict) -> bool:
            return (
                s["a_db"] == ("RUNNING", None)
                and s["a_owner"]
                and not s["a_record_terminal"]
                and s["b_state"] == "QUEUED"
                and s["decode_starts"] == 1
            )

        # 卡住窗口：不变式必须全程成立；任何提前释放的形态立即收集证据变红
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 15
        good_since = None
        violation = None
        try:
            while loop.time() < deadline:
                current = await sample()
                if not invariant(current):
                    violation = current
                    break
                if entered.is_set():
                    if good_since is None:
                        good_since = loop.time()
                    elif loop.time() - good_since >= 1.0:
                        break
                await asyncio.sleep(0.05)
            assert violation is None, (
                f"R4 不变式被违反（owner/闸门先于持久化释放）：{violation}"
            )
            assert entered.is_set(), (
                "fail_active_tasks 没有触发 FAILED 持久写，探针没打到点"
            )

            # 松开持久写：先落库成功，owner/闸门才释放，第二个 Job 才获准解码
            release_persist.set()
            await asyncio.wait_for(trigger_task, 30)
            row = None
            deadline = loop.time() + 30
            while loop.time() < deadline:
                row = harness.read_db(
                    "SELECT state, error_code FROM jobs WHERE job_id=?", (job_a,)
                )[0]
                if row["state"] == "FAILED":
                    break
                await asyncio.sleep(0.05)
            assert (row["state"], row["error_code"]) == ("FAILED", "internal"), (
                dict(row)
            )
            assert (await wait_terminal(harness, recovery_b, timeout=60)).state == "DONE"
            # 每个 Job 恰好解码一次，且任一时刻只有一个解码进程
            assert len(read_ffmpeg_starts(ffmpeg_log)) == 2, (
                read_ffmpeg_invocations(ffmpeg_log)
            )
            assert ffmpeg_peak_concurrency(ffmpeg_log) == 1, (
                read_ffmpeg_invocations(ffmpeg_log)
            )
            assert harness.read_db("SELECT COUNT(*) AS n FROM results")[0]["n"] == 1
            assert list(harness.state.active_http_jobs) == []
            assert harness.state.tasks == {}
            assert harness.http_server.fatal is None
        finally:
            release_persist.set()


async def _wait_path(path: Path, timeout: float = 20.0):
    """跨进程探针辅助：等子进程落下的标记文件出现。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not path.exists():
        if loop.time() >= deadline:
            raise AssertionError(f"等待标记文件超时：{path}")
        await asyncio.sleep(0.05)


async def _probe_release_order_segment_timeout(tmp_path, monkeypatch):
    """segment_timeout 路径：真实独立服务子进程，SystemExit 是其正常收尾。"""
    from tests.harness.server import ManagedHttpServerHarness

    install_recording_ffmpeg(tmp_path, monkeypatch)  # 建 shim 目录；子进程经 PATH 使用
    stalled = dict(FAKE_ENGINE, delay_on_call=1, delay_seconds=15.0)
    source_a = make_container(tmp_path, "probe_a.mp3")
    source_b = make_container(tmp_path, "probe_b.mp3")
    marker = tmp_path / "persist-block.release"
    entered = tmp_path / "persist-block.entered"
    harness = await ManagedHttpServerHarness.start(
        data_dir=tmp_path / "httpdata", options=stalled,
        ffmpeg_shim=tmp_path / "ffmpeg-shim",
        stderr_path=tmp_path / "server-stderr.log",
        env={
            "CW_SEGMENT_TIMEOUT": "1",
            "CW_TEST_PERSIST_BLOCK_MARKER": str(marker),
            "CW_TEST_PERSIST_BLOCK_ENTERED": str(entered),
            "CW_TEST_PERSIST_BLOCK_WAIT": "45",
        },
    )
    ffmpeg_log = tmp_path / "ffmpeg-invocations.jsonl"
    try:
        recovery_a = tmp_path / "probe_a.json"
        recovery_b = tmp_path / "probe_b.json"
        handle_a = await submit(
            harness, source_a, recovery_a, seg_duration=5.0, seg_overlap=1.0
        )
        handle_b = await submit(
            harness, source_b, recovery_b, seg_duration=5.0, seg_overlap=1.0
        )
        job_a = handle_a.job_id
        assert (await wait_state(harness, recovery_a, "RUNNING")).state == "RUNNING"
        # 监控协程命中段超时 → 违规路径触发 → 卡在被阻塞的 FAILED 持久写
        await _wait_path(entered, 20)

        async def sample() -> dict:
            row = harness.read_db(
                "SELECT state, error_code FROM jobs WHERE job_id=?", (job_a,)
            )[0]
            return {
                "a_db": (row["state"], row["error_code"]),
                "b_state": (await raw_job(harness, recovery_b))["state"],
                "decode_starts": len(read_ffmpeg_starts(ffmpeg_log)),
            }

        def invariant(s: dict) -> bool:
            return (
                s["a_db"] == ("RUNNING", None)
                and s["b_state"] == "QUEUED"
                and s["decode_starts"] == 1
            )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10
        good_since = None
        violation = None
        while loop.time() < deadline:
            current = await sample()
            if not invariant(current):
                violation = current
                break
            if good_since is None:
                good_since = loop.time()
            elif loop.time() - good_since >= 1.0:
                break
            await asyncio.sleep(0.05)
        assert violation is None, (
            f"R4 不变式被违反（owner/闸门先于持久化释放）：{violation}"
        )

        # 放行：落库成功、owner 释放之后，监控按原语义 SystemExit(1) 非零退出
        marker.write_text("release")
        await harness.wait_for_exit(30)
    finally:
        marker.write_text("release")  # 幂等：探针中途变红也要让子进程能走完
        await harness.cleanup()

    assert harness.exitcode != 0, "推理段超时仍必须非零退出"
    row = harness.read_db(
        "SELECT state, error_code FROM jobs WHERE job_id=?", (job_a,)
    )[0]
    assert (row["state"], row["error_code"]) == ("FAILED", "inference_timeout"), (
        dict(row)
    )
    assert harness.read_db("SELECT COUNT(*) AS n FROM results")[0]["n"] == 0
    # 卡住窗口内第二个 Job 始终没有获准解码；放行后进程立即退出，也不会再解码
    assert len(read_ffmpeg_starts(ffmpeg_log)) == 1, (
        read_ffmpeg_invocations(ffmpeg_log)
    )
    assert ffmpeg_peak_concurrency(ffmpeg_log) == 1, (
        read_ffmpeg_invocations(ffmpeg_log)
    )


@pytest.mark.asyncio
async def test_worker_crash_persists_failed(tmp_path):
    """R4：worker 真实 os._exit(非零) 崩溃，主进程非零退出之前 FAILED 已落库。

    崩溃由 monitor 的存活检查发现并转 fail_active_tasks 收尾：修复前 HTTP 任务
    只做内存态 transition_terminal（owner 立即释放、错因永远丢失，库里停在
    RUNNING）；修复后先可靠落库 FAILED[internal]，主进程才非零退出。父进程在
    子进程存活期间持续轮询真实 SQLite——FAILED 必须发生在退出之前，而不是靠
    重启后的 server_restarted 反推。
    """
    from tests.harness.server import ManagedHttpServerHarness

    crashing = dict(FAKE_ENGINE, exit_on_call=1, exit_code=3)
    harness = await ManagedHttpServerHarness.start(
        data_dir=tmp_path / "httpdata", options=crashing,
        stderr_path=tmp_path / "server-stderr.log",
    )
    try:
        source = make_container(tmp_path, "speech.mp3")
        recovery = tmp_path / "resume.json"
        handle = await submit(harness, source, recovery, seg_duration=5.0, seg_overlap=1.0)
        job_id = handle.job_id
        assert (await wait_state(harness, recovery, "RUNNING")).state == "RUNNING"
        observations = []
        while True:
            # 先采样、后判活：FAILED 的提交严格先于进程死亡，最后一份样本
            # 必然落在提交之后（若先判活，死亡检测的毫秒级延迟会漏掉
            # 「提交 → 死亡」之间的窄窗口，把真绿误判成红）
            rows = harness.read_db(
                "SELECT state, error_code FROM jobs WHERE job_id=?", (job_id,)
            )
            if rows:
                observations.append((rows[0]["state"], rows[0]["error_code"]))
            if not harness.process.is_alive():
                break
            await asyncio.sleep(0.01)
        await harness.wait_for_exit(30)
        # Manager 代理在 cleanup 之后不可访问，崩溃证据必须在 cleanup 前取出
        crash_call_count = len(harness.calls)
    finally:
        await harness.cleanup()

    assert harness.exitcode != 0, "worker 崩溃必须让主进程非零退出"
    # 崩溃路径身份：worker 真实进到第 1 次引擎调用后 os._exit(3)；
    # CW_SEGMENT_TIMEOUT 默认 600s ≫ 本用例时长，段超时分支不可能触发；
    # SystemExit 经 serve() 显式打印 HTTP_SERVER_FAILURE 到子进程 stderr 文件
    assert crash_call_count == 1, "worker 必须真实进入第 1 次解码后崩溃"
    # SystemExit 被 asyncio 从 task 直接重抛进事件循环，绕开 serve() 的错误
    # 打印路径，因此子进程 stderr 文件为空是正常形态；崩溃路径身份由
    # crash_call_count == 1（进入过引擎）+ FAILED[internal] + 默认
    # CW_SEGMENT_TIMEOUT=600s 排除段超时分支共同锁定。
    failed_before_exit = [
        item for item in observations if item[0] == "FAILED" and item[1]
    ]
    assert failed_before_exit, (
        f"进程退出前未观察到已落库的 FAILED（error_code 为空）：{observations[-6:]}"
    )
    assert {item[1] for item in failed_before_exit} == {"internal"}
    row = harness.read_db(
        "SELECT state, error_code FROM jobs WHERE job_id=?", (job_id,)
    )[0]
    assert row["state"] == "FAILED" and row["error_code"] == "internal"
    assert harness.read_db("SELECT COUNT(*) AS n FROM results")[0]["n"] == 0


@pytest.mark.asyncio
async def test_finalize_giveup_keeps_owner_when_persist_times_out(tmp_path, monkeypatch):
    """R4：持久写超过有界期限时收尾必须放弃等待——不释放 owner、显式日志。

    把 HTTP_FINALIZE_TIMEOUT 收紧到 0.3s，让 FAILED 持久写卡 60s：finalize 必须
    在期限后超时返回（「错因没写进去」绝不能升级成「进程永久挂住」），此时 Job
    在库中仍是 RUNNING、owner 仍被持有、fail_active_tasks 已完成，并留下
    「持久失败事实未落库」的显式错误日志，交调用方按原语义非零退出兜底。
    """
    import logging

    from core.server import http_file_runner as runner_module
    from core.server import logger as server_logger

    stalled = dict(FAKE_ENGINE, delay_on_call=1, delay_seconds=4.0)
    source = make_container(tmp_path, "speech.mp3")
    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    capture = _Capture(level=logging.ERROR)
    server_logger.addHandler(capture)
    monkeypatch.setattr(runner_module, "HTTP_FINALIZE_TIMEOUT", 0.3)
    try:
        async with running_runner_server(tmp_path, options=stalled) as harness:
            recovery = tmp_path / "resume.json"
            handle = await submit(
                harness, source, recovery, seg_duration=5.0, seg_overlap=1.0
            )
            job_id = handle.job_id
            assert (await wait_state(harness, recovery, "RUNNING")).state == "RUNNING"
            await _wait_engine_calls(harness, 1)
            entered = asyncio.Event()

            async def stuck_fail_job(job_id_, error_code):
                entered.set()
                await asyncio.sleep(60)

            monkeypatch.setattr(harness.http_server, "fail_job", stuck_fail_job)
            loop = asyncio.get_running_loop()
            started = loop.time()
            await fail_active_tasks(harness.state, "internal", "R4 超时兜底探针")
            assert entered.is_set(), "fail_active_tasks 没有触发持久写，探针没打到点"
            assert loop.time() - started < 10, (
                "finalize 没有在超时限内放弃，进程会被永久挂住"
            )
            row = harness.read_db(
                "SELECT state, error_code FROM jobs WHERE job_id=?", (job_id,)
            )[0]
            assert (row["state"], row["error_code"]) == ("RUNNING", None), dict(row)
            assert job_id in list(harness.state.active_http_jobs)
            record = harness.state.tasks[make_task_key("http", job_id)]
            assert record.status not in {"DONE", "FAILED"}
            messages = [r.getMessage() for r in records if r.levelno >= logging.ERROR]
            assert any("持久失败事实未落库" in m for m in messages), messages
    finally:
        server_logger.removeHandler(capture)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["pcm_chunks", "enqueue"])
async def test_unknown_background_exception_fails_job_and_exits_nonzero(tmp_path, fault):
    """runner 后台未知异常：先可靠 FAILED，再上抛到监督链让进程非零退出。"""
    from tests.harness.server import ManagedHttpServerHarness

    harness = await ManagedHttpServerHarness.start(
        data_dir=tmp_path / "httpdata", options=FAKE_ENGINE, fault=fault
    )
    try:
        source = make_container(tmp_path, "speech.mp3")
        recovery = tmp_path / "resume.json"
        handle = await submit(harness, source, recovery, seg_duration=5.0, seg_overlap=1.0)
        job_id = handle.job_id
        await harness.wait_for_exit(20)
    finally:
        await harness.cleanup()

    assert harness.exitcode != 0, "未知后台异常必须让进程非零退出"
    assert f"injected background failure ({fault})" in harness.stderr_tail()
    conn = sqlite3.connect(tmp_path / "httpdata" / "http.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT state, error_code FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        assert row["state"] == "FAILED"
        assert row["error_code"] == "internal"
        assert conn.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_http_data_dir_has_no_temporary_pcm_after_completion(tmp_path):
    """整个链路不留临时 PCM：解码走管道，落盘只有源文件与数据库。"""
    source = make_container(tmp_path, "speech.mp3")
    recovery = tmp_path / "resume.json"
    async with running_runner_server(tmp_path) as harness:
        await submit(harness, source, recovery, seg_duration=5.0, seg_overlap=1.0)
        assert (await wait_terminal(harness, recovery)).state == "DONE"
        payload = json.loads(recovery.read_text(encoding="utf-8"))
        files = sorted(str(path.relative_to(harness.data_dir)) for path in
                        harness.data_dir.rglob("*") if path.is_file())
        assert all(
            name == f"sources/{payload['upload_id']}.bin" or name.startswith("http.")
            for name in files
        ), files
        assert not any(name.endswith((".pcm", ".raw", ".tmp")) for name in files)