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
from core.server.state import ServerState  # noqa: E402
from core.server.connection.ws_send import ws_send  # noqa: E402
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
    """把一个真 ffmpeg 包装脚本放到 PATH 首位，记录真实 argv/env 后 exec 真 ffmpeg。

    记录由真实子进程产生；包装脚本不在 PATH 首位时 runner 不会经过它，
    记录文件为空会让断言变红，因此该证据不会被绕过。
    """
    bindir = tmp_path / "ffmpeg-shim"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "ffmpeg-invocations.jsonl"
    script = bindir / "ffmpeg"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"RECORD = {str(log)!r}\n"
        f"REAL = {FFMPEG!r}\n"
        "entry = {\n"
        "    'argv': list(sys.argv),\n"
        "    'env_marker': os.environ.get('CW_TEST_ENV_MARKER'),\n"
        "    'path_head': (os.environ.get('PATH') or '').split(os.pathsep)[0],\n"
        "}\n"
        "with open(RECORD, 'a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps(entry, ensure_ascii=False) + '\\n')\n"
        "os.execv(REAL, sys.argv)\n",
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
        invocations = read_ffmpeg_invocations(ffmpeg_log)
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
async def test_queued_job_result_is_not_ready_and_exposes_nothing(tmp_path):
    """识别尚未完成时 GET result 是 409 result_not_ready，且不泄露半成品。"""
    stalled = dict(FAKE_ENGINE, delay_on_call=1, delay_seconds=60.0)
    source = make_container(tmp_path, "speech.mp3", seconds=20.0)
    recovery = tmp_path / "resume.json"
    async with running_runner_server(tmp_path, options=stalled) as harness:
        handle = await submit(harness, source, recovery, seg_duration=5.0, seg_overlap=1.0)
        status = await get_file_job_http(harness.base_url, resume_path=recovery)
        assert status.state == "QUEUED"
        response = await raw_get(harness, recovery, "/result")
        assert response.status_code == 409
        body = response.json()
        assert body["code"] == "result_not_ready"
        assert "tokens" not in body and "text_accu" not in body
        with pytest.raises(AsrError) as info:
            await get_file_result_http(harness.base_url, resume_path=recovery)
        assert info.value.code == "result_not_ready"
        assert harness.read_db("SELECT COUNT(*) AS n FROM results")[0]["n"] == 0
        assert handle.job_id


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
        status = await get_file_job_http(harness.base_url, resume_path=recovery)
        assert status.state == "QUEUED"
        await harness.terminate(signal.SIGTERM, timeout=20)
    finally:
        await harness.cleanup()

    assert harness.exitcode == 0, harness.stderr_tail()

    # 重启：同一数据目录，旧 Job 被收敛为 server_restarted，不自动重跑
    restarted = await ManagedHttpServerHarness.start(data_dir=data_dir, options=stalled)
    try:
        status = await get_file_job_http(restarted.base_url, resume_path=recovery)
        assert status.state == "FAILED"
        assert status.error_code == "server_restarted"
        assert status.result_available is False
        await asyncio.sleep(0.3)
        assert restarted.received == [], "重启后不得自动重跑推理"
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
        await harness.terminate(signal.SIGKILL, timeout=20)
    finally:
        await harness.cleanup()
    assert harness.exitcode != 0

    restarted = await ManagedHttpServerHarness.start(data_dir=data_dir, options=stalled)
    try:
        status = await get_file_job_http(restarted.base_url, resume_path=recovery)
        assert status.state == "FAILED"
        assert status.error_code == "server_restarted"
        assert handle.job_id
        await asyncio.sleep(0.3)
        assert restarted.received == []
        row = restarted.read_db("SELECT COUNT(*) AS n FROM results")[0]["n"]
        assert row == 0
    finally:
        await restarted.stop()
        await restarted.cleanup()


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