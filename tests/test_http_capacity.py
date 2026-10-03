# coding: utf-8
"""M4-C1 容量不变式：真实源预留、结果/WAL 预留、物理余量的行为契约。

与 test_http_store.py 的持久性契约分开：本文件只钉三件事——
  1. 容量怎么算（16 GiB 源预留按 source-presence；DB guard 含待处理结果 + WAL 预留；
     物理余量扣掉所有已承诺但尚未落盘的占用）；
  2. 容量不足时拒收的副作用边界（不留痕、不写盘、不推进 offset、不改旧数据）；
  3. 容量满时不该误拒的东西（幂等重放、旧 job/result 查询）仍可用。

参考模型一律是真实字节与真实 SQLite 行：DB/-wal/-shm 的长度用 os.stat 实测，源字节用
真实文件长度实测，HTTP 状态用真实 aiohttp TCP 往返。预算函数自己的输出**不**当参考
模型，只当被测对象与计数单位；单 Job 预留的覆盖性由真实 SQLite 字节单独证明。
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import math
import os
import shutil
import sqlite3
import threading
import time
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from core.server import http_store as store_module
from core.server.http_store import HttpStore, HttpStoreError

MB = 1024 * 1024

needs_aiohttp = pytest.mark.skipif(
    importlib.util.find_spec("aiohttp") is None,
    reason="未安装 aiohttp==3.14.3，HTTP listener 默认关闭",
)


# ------------------------------------------------------------------ 通用助手


def _options() -> dict:
    return {"model": None, "language": None, "context": None,
            "seg_duration": 15.0, "seg_overlap": 2.0}


def _final_result(job_id: str, text: str = "已完成") -> dict:
    return {
        "task_id": job_id, "socket_id": "", "type": "file", "owner_kind": "http",
        "duration": 1.0, "time_start": 1.0, "time_submit": 2.0, "time_complete": 3.0,
        "text": text, "text_accu": text, "tokens": [text], "timestamps": [0.1],
        "is_final": True,
    }


def _sized_result(job_id: str, encoded_cap: int) -> tuple[dict, int]:
    """造一个合法完整结果，其 JSON 编码后逼近 encoded_cap 字节。

    tokens 拼接必须等于 text_accu，所以 text / text_accu / tokens[0] 各存一份正文，
    每个正文字符按 3 份折算；留 16 字节余量保证不撞 record_result 的 64 MiB 上限。
    """
    probe = _final_result(job_id, "a")
    fixed = len(json.dumps(probe, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    chars = max(1, (encoded_cap - fixed - 16) // 3)
    body = "a" * chars
    result = _final_result(job_id, body)
    return result, len(json.dumps(result, ensure_ascii=False,
                                  separators=(",", ":")).encode("utf-8"))


def _db_file_bytes(data_dir: Path) -> int:
    """实测 DB/-wal/-shm 的真实长度（参考模型，不问被测函数）。"""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.stat(Path(str(data_dir / "http.sqlite3") + suffix)).st_size
        except FileNotFoundError:
            continue
    return total


def _source_bytes(data_dir: Path) -> int:
    return sum(item.stat().st_size for item in (data_dir / "sources").iterdir())


def _source_path(data_dir: Path, upload_id: str) -> Path:
    return data_dir / "sources" / f"{upload_id}.bin"


def _read_rows(data_dir: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    """独立只读连接读真实落盘的 SQLite 文件，不用被测连接。"""
    conn = sqlite3.connect(f"file:{data_dir / 'http.sqlite3'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _row_count(data_dir: Path, table: str) -> int:
    return _read_rows(data_dir, f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]


def _offset_of(data_dir: Path, upload_id: str) -> int:
    return _read_rows(data_dir, "SELECT confirmed_offset AS n FROM uploads WHERE upload_id=?",
                      (upload_id,))[0]["n"]


_DiskUsage = shutil._ntuple_diskusage


def _fixed_free(free: int):
    """只改 free 读数的假 disk_usage：used/total 取真实值，不伪造。"""
    real = shutil.disk_usage(Path(__file__).resolve().parent)

    def usage(_path):
        return _DiskUsage(total=real.total, used=real.used, free=free)

    return usage


def _tracking_source_free(data_dir: Path, start_free: int):
    """free 随 sources 里**真实落盘**的源字节线性递减（数据库增长不计入本次账）。

    于是「free 减少量」与「已物化源字节」严格同步：若实现把已写字节在 free 与未物化
    承诺两侧重复收费，余量账会随写入进度一路下滑。
    """
    base_free = start_free + _source_bytes(data_dir)

    def usage(_path):
        free = base_free - _source_bytes(data_dir)
        return _DiskUsage(total=base_free, used=base_free - free, free=free)

    return usage


def _expected_result_reservation(page_size: int, result_cap: int) -> int:
    """单 Job 预留上界的独立抄写：核对计数单位，不当覆盖性证据（覆盖性看真实 SQLite 字节）。"""
    overflow = max(0, result_cap - (page_size - 35))
    record_pages = 1 + math.ceil(overflow / (page_size - 4))
    peak = (record_pages + store_module.RESULT_COMMIT_EXTRA_PAGES) * (
        2 * page_size + store_module.WAL_FRAME_OVERHEAD_BYTES)
    return math.ceil(peak * store_module.RESULT_RESERVATION_SAFETY)


def _create(store: HttpStore, payload: bytes, token: str = "tok", create_key: str = "key"):
    return store.create_upload(size_bytes=len(payload), sha256=sha256(payload).hexdigest(),
                               options=_options(), token=token, create_key=create_key)


def _write(store: HttpStore, record, payload: bytes, token: str = "tok", chunk: int = 4096) -> int:
    offset = 0
    while offset < len(payload):
        offset = store.append_bytes(record.upload_id, token, offset,
                                    payload[offset:offset + chunk])
    return offset


def _uploaded(store: HttpStore, payload: bytes, token: str = "tok", create_key: str = "key"):
    record = _create(store, payload, token, create_key)
    _write(store, record, payload, token)
    return record


def _pending_job(store: HttpStore, payload: bytes, tag: str):
    record = _uploaded(store, payload, f"t-{tag}", f"k-{tag}")
    return store.commit_upload(record.upload_id, f"t-{tag}")


# ------------------------------------------------------------------ 真实 HTTP


class _StubApp:
    def __init__(self, loop):
        self.loop = loop
        self.state = SimpleNamespace(tasks={})


@contextlib.asynccontextmanager
async def _running_server(data_dir: Path, inference: bool = True):
    from core.server.http_server import HttpServer

    loop = asyncio.get_running_loop()
    server = HttpServer(_StubApp(loop), "127.0.0.1", 0, data_dir)
    server.inference_available = inference
    server.prepare()
    await server._runner.setup()
    site = server._web.TCPSite(server._runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield server, f"http://127.0.0.1:{port}"
    finally:
        await server.stop()


async def _http_create(client, base, payload: bytes, token="tok", key="key"):
    return await client.post(
        f"{base}/v1/uploads",
        content=json.dumps({"size_bytes": len(payload),
                            "sha256": sha256(payload).hexdigest(),
                            "options": {}}).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key,
                 "Content-Type": "application/json"},
    )


async def _http_patch(client, base, upload_id, token, offset, data: bytes):
    return await client.patch(
        f"{base}/v1/uploads/{upload_id}", content=data,
        headers={"Authorization": f"Bearer {token}", "Upload-Offset": str(offset),
                 "Content-Type": "application/octet-stream"},
    )


async def _http_commit(client, base, upload_id, token):
    return await client.post(
        f"{base}/v1/uploads/{upload_id}/commit", content=b"",
        headers={"Authorization": f"Bearer {token}", "Content-Length": "0"},
    )


# ------------------------------------------------------------------ 源容量口径


@needs_aiohttp
@pytest.mark.asyncio
async def test_source_reserve_charges_uploaded_expired_and_committed_sources(tmp_path, monkeypatch):
    """活跃上传、EXPIRED 但保留物理文件、已提交保留源三类都必须计费。

    真实 HTTP 状态码 + 真实 SQLite 行 + 真实源文件字节；等值放行、超一字节才拒。
    """
    data_dir = tmp_path / "httpdata"
    active = b"s" * 4096
    stale = b"e" * 4096
    # 上限 = 三份已登记源之和；等值再放一份正好用满，超一字节即拒
    monkeypatch.setattr(store_module, "SOURCE_RESERVE_BYTES", 3 * len(active))
    async with _running_server(data_dir) as (server, base):
        async with httpx.AsyncClient(timeout=30) as client:
            created = await _http_create(client, base, active, "t1", "k1")
            assert created.status_code == 201, created.text
            committed_id = created.json()["upload_id"]
            patched = await _http_patch(client, base, committed_id, "t1", 0, active)
            assert patched.status_code == 204
            accepted = await _http_commit(client, base, committed_id, "t1")
            assert accepted.status_code == 202
            done_job = accepted.json()["job_id"]
            server._worker.run_sync(server._store.record_result, done_job, _final_result(done_job))

            # 过期但仍保留物理文件的 partial：state=EXPIRED，文件仍在
            stale_id = (await _http_create(client, base, stale, "t2", "k2")).json()["upload_id"]
            assert (await _http_patch(client, base, stale_id, "t2", 0, stale[:8])).status_code == 204
            server._worker.run_sync(
                lambda: server._store.conn.execute(
                    "UPDATE uploads SET state='EXPIRED' WHERE upload_id=?", (stale_id,))
            )
            assert _source_path(data_dir, stale_id).stat().st_size == 8

            # 等值：已占三份，再加一份正好用满 4 份上限
            equal = await _http_create(client, base, active, "t3", "k3")
            assert equal.status_code == 201, equal.text
            equal_id = equal.json()["upload_id"]

            # 超一字节即拒
            overflow = await _http_create(client, base, b"x", "t4", "k4")
            assert overflow.status_code == 507
            assert overflow.json()["code"] == "source_reserve_full"

            rows = _read_rows(data_dir, "SELECT upload_id, state, size_bytes FROM uploads ORDER BY upload_id")
            # 拒收不得插入 uploads 行
            assert [(row["upload_id"], row["state"]) for row in rows] == sorted(
                [(committed_id, "COMMITTED"), (stale_id, "EXPIRED"), (equal_id, "UPLOADING")])
            assert [row["size_bytes"] for row in rows] == [len(active), len(stale), len(active)]
            # 真实源字节：已提交源写满、过期 partial 保留 8 字节、新建源仍是空文件
            assert _source_path(data_dir, committed_id).stat().st_size == len(active)
            assert _source_path(data_dir, stale_id).stat().st_size == 8
            assert _source_path(data_dir, equal_id).stat().st_size == 0
            # 已完成结果仍可读（容量计算没有顺手删任何东西）
            read = await client.get(f"{base}/v1/jobs/{done_job}/result",
                                    headers={"Authorization": "Bearer t1"})
            assert read.status_code == 200


def test_source_reserve_counts_declared_size_not_confirmed_offset(tmp_path, monkeypatch):
    """尾没写完也按声明长度计费：不能拿 confirmed_offset 当总预留。"""
    data_dir = tmp_path / "data"
    declared = 8 * MB
    tiny = 4096
    with HttpStore(data_dir) as store:
        record = store.create_upload(size_bytes=declared, sha256=sha256(b"x").hexdigest(),
                                     options=_options(), token="t", create_key="k")
        store.append_bytes(record.upload_id, "t", 0, b"x" * tiny)
        assert store.get_upload(record.upload_id, "t").confirmed_offset == tiny
        # 恰好放行一份同尺寸新源（等值合法）
        monkeypatch.setattr(store_module, "SOURCE_RESERVE_BYTES", declared + tiny)
        assert _create(store, b"y" * tiny, "t2", "k2").upload_id != record.upload_id
        # 再加一字节就超
        monkeypatch.setattr(store_module, "SOURCE_RESERVE_BYTES", declared + tiny - 1)
        with pytest.raises(HttpStoreError) as info:
            _create(store, b"z" * tiny, "t3", "k3")
        assert info.value.code == "source_reserve_full"
        assert info.value.status == 507
        assert _row_count(data_dir, "uploads") == 2


def test_source_reserve_releases_deleted_terminal_source(tmp_path):
    """源被安全删除后退出 16 GiB 预留；同一 Job 的结果、元数据与身份不变。"""
    data_dir = tmp_path / "data"
    payload = b"p" * 4096
    with HttpStore(data_dir, inference_ready=lambda: True) as store:
        first = _uploaded(store, payload, "t1", "k1")
        job = store.commit_upload(first.upload_id, "t1")
        store.record_result(job.job_id, _final_result(job.job_id))
        second = _uploaded(store, payload, "t2", "k2")
        assert store._reserved_source_bytes() == 2 * len(payload)
        # C2 的「先 unlink、后记释放」里 unlink 已完成的那一步
        os.unlink(_source_path(data_dir, first.upload_id))
        assert not _source_path(data_dir, first.upload_id).exists()
        assert store._reserved_source_bytes() == len(payload)
        # 额度真的放开：原本满额的两份现在放得下第三份
        assert _create(store, b"q" * 4096, "t3", "k3").upload_id
        # 删除不动 jobs / results / 元数据
        assert store.get_result(job.job_id, "t1")["text"] == "已完成"
        assert store.job_record(job.job_id, "t1").state == "DONE"
        assert _row_count(data_dir, "uploads") == 3
        assert _row_count(data_dir, "results") == 1


def test_source_reserve_stat_failure_is_not_treated_as_deleted_source(tmp_path, monkeypatch):
    """stat 的非「不存在」错误必须显式上抛，不能当成源已删，也不能当成 0 占用放行。"""
    data_dir = tmp_path / "data"
    payload = b"p" * 4096
    real_stat = os.stat

    def denying_stat(path, *args, **kwargs):
        if str(path).endswith(f"{record.upload_id}.bin"):
            raise PermissionError(13, "Permission denied", str(path))
        return real_stat(path, *args, **kwargs)

    with HttpStore(data_dir) as store:
        record = _uploaded(store, payload)
        assert store._reserved_source_bytes() == len(payload)
        with monkeypatch.context() as patch:
            patch.setattr(os, "stat", denying_stat)
            with pytest.raises(PermissionError):
                store._reserved_source_bytes()
            with pytest.raises(PermissionError):
                _create(store, b"z", "t2", "k2")
        assert _row_count(data_dir, "uploads") == 1
    # 注入撤除后口径恢复正常：源仍按声明长度计费
    assert _reserved_bytes_after(data_dir) == len(payload)


def _reserved_bytes_after(data_dir: Path) -> int:
    with HttpStore(data_dir) as store:
        return store._reserved_source_bytes()


# ------------------------------------------------------------------ DB + WAL + 结果预留


def test_pending_jobs_reserve_result_and_wal_peak_and_commit_refuses_beyond(tmp_path, monkeypatch):
    """受理 Job 时提前预留完整结果 + WAL 峰值；create 与 commit 用同一口径。"""
    data_dir = tmp_path / "data"
    cap = 512 * 1024
    monkeypatch.setattr(store_module, "MAX_RESULT_BYTES", cap)
    payload = b"p" * 4096
    db_slack = MB  # 一次事务自身写入 SQLite 的 WAL 增量，给阈值留的实测余量
    with HttpStore(data_dir, inference_ready=lambda: True) as store:
        per_job = store._result_reservation_bytes()
        assert per_job == _expected_result_reservation(store._sqlite_page_size(), cap)
        assert store._pending_result_reservation_bytes() == 0

        first = _pending_job(store, payload, "one")
        assert store._pending_result_reservation_bytes() == per_job
        db_now = _db_file_bytes(data_dir)
        assert db_now > 0
        # 上限 = 真实占用 + 再放两个待处理 Job 的预留（等值放行）
        monkeypatch.setattr(store_module, "DB_GUARD_BYTES", db_now + 2 * per_job + db_slack)
        assert _create(store, b"q", "t-two-src", "k-two-src").upload_id
        second_upload = _uploaded(store, payload, "t-two", "k-two")
        second = store.commit_upload(second_upload.upload_id, "t-two")
        assert store._pending_result_reservation_bytes() == 2 * per_job

        # 第三个 Job 的预留放不下：commit 必须 507 且不留痕
        third_upload = _uploaded(store, payload, "t-three", "k-three")
        with pytest.raises(HttpStoreError) as info:
            store.commit_upload(third_upload.upload_id, "t-three")
        assert info.value.code == "storage_guard_full"
        assert info.value.status == 507
        assert store.get_upload(third_upload.upload_id, "t-three").state == "UPLOADING"
        assert store.get_upload(third_upload.upload_id, "t-three").confirmed_offset == len(payload)
        assert _source_path(data_dir, third_upload.upload_id).stat().st_size == len(payload)
        assert _row_count(data_dir, "jobs") == 2
        # 同一个 guard 也挡住 create：待处理预留必须进 create 的账
        monkeypatch.setattr(store_module, "DB_GUARD_BYTES", _db_file_bytes(data_dir))
        with pytest.raises(HttpStoreError) as create_info:
            _create(store, b"r", "t-four", "k-four")
        assert create_info.value.code == "storage_guard_full"
        assert first.job_id != second.job_id


def test_terminal_job_releases_pending_reservation_into_measured_bytes(tmp_path, monkeypatch):
    """Job 进终态后 pending 预留自动退出，同一份结果字节转为实测文件占用。"""
    data_dir = tmp_path / "data"
    cap = 256 * 1024
    monkeypatch.setattr(store_module, "MAX_RESULT_BYTES", cap)
    payload = b"p" * 4096
    with HttpStore(data_dir, inference_ready=lambda: True) as store:
        per_job = store._result_reservation_bytes()
        job = _pending_job(store, payload, "one")
        db_before = _db_file_bytes(data_dir)
        assert store._pending_result_reservation_bytes() == per_job
        result, encoded = _sized_result(job.job_id, cap)
        store.record_result(job.job_id, result)
        db_after = _db_file_bytes(data_dir)
        assert store._pending_result_reservation_bytes() == 0
        # 结果字节是真的落在 DB/-wal/-shm 里（WAL 帧副本也计入实测长度）
        assert db_after - db_before >= encoded
        # guard 只看实测占用，同一额度又放得下新上传
        monkeypatch.setattr(store_module, "DB_GUARD_BYTES", db_after)
        assert _create(store, b"q", "t2", "k2").upload_id


def test_failed_job_releases_pending_reservation_and_keeps_409(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(store_module, "MAX_RESULT_BYTES", 256 * 1024)
    payload = b"p" * 4096
    with HttpStore(data_dir, inference_ready=lambda: True) as store:
        per_job = store._result_reservation_bytes()
        job = _pending_job(store, payload, "one")
        assert store._pending_result_reservation_bytes() == per_job
        assert store.mark_running(job.job_id) is True
        assert store._pending_result_reservation_bytes() == per_job, "RUNNING 也必须预留"
        store.fail_job(job.job_id, "probe_failure")
        assert store._pending_result_reservation_bytes() == 0
        with pytest.raises(HttpStoreError) as info:
            store.get_result(job.job_id, "t-one")
        assert info.value.status == 409
        assert info.value.code == "job_failed"
        assert info.value.error_code == "probe_failure"


@needs_aiohttp
@pytest.mark.asyncio
async def test_result_reservation_covers_real_sqlite_peak(tmp_path, monkeypatch):
    """真实 SQLite 对照：单 Job 预留必须覆盖真实 DB/-wal/-shm 峰值。

    同时钉死「payload × 2 并不覆盖」这一事实，并打印 page_size、checkpoint 前后与
    多 Job 逐个提交的真实字节，供回归对照。
    """
    data_dir = tmp_path / "data"
    cap = 16 * MB
    monkeypatch.setattr(store_module, "MAX_RESULT_BYTES", cap)
    payload = b"p" * 4096
    jobs = 3
    with HttpStore(data_dir, inference_ready=lambda: True) as store:
        page_size = store._sqlite_page_size()
        reserve = store._result_reservation_bytes()
        job_ids = [_pending_job(store, payload, f"j{index}").job_id for index in range(jobs)]
        store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        base = _db_file_bytes(data_dir)
        print(f"[real-sqlite] page_size={page_size} cap={cap} reserve={reserve} "
              f"reserve/cap={reserve / cap:.4f} naive_2x={2 * cap} base={base}")
        first_growth = None
        for index, job_id in enumerate(job_ids):
            result, encoded = _sized_result(job_id, cap)
            before = _db_file_bytes(data_dir)
            store.record_result(job_id, result)
            after = _db_file_bytes(data_dir)
            growth = after - before
            pending = store._pending_result_reservation_bytes()
            print(f"[real-sqlite] job#{index} payload={encoded} growth={growth} "
                  f"db+wal+shm={after} pending={pending}")
            # 每次真实提交的增量都被单 Job 预留覆盖
            assert growth <= reserve, (
                f"真实单次提交峰值 {growth} 超过单 Job 预留 {reserve}（page_size={page_size}）")
            if index == 0:
                first_growth = growth
        # 首次提交含完整 WAL 帧副本：预留不是空头，且 2 倍确实不覆盖（最小更正的依据）
        assert first_growth >= 1.9 * cap, f"峰值 {first_growth} 不含 WAL 副本，测试无证明力"
        assert first_growth > 2 * cap, "实测峰值未超出 payload×2，无法证明 2 倍不足"
        assert reserve <= 2.2 * cap, f"预留 {reserve} 相对结果上限过于宽松"
        # 全部 pending 跑完后：真实总占用不超过「基线 + 受理时预留的合计」
        assert _db_file_bytes(data_dir) - base <= jobs * reserve
        store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        after_checkpoint = _db_file_bytes(data_dir)
        print(f"[real-sqlite] after ckpt TRUNCATE db+wal+shm={after_checkpoint}")
        assert after_checkpoint >= 3 * cap
        assert store._pending_result_reservation_bytes() == 0


def test_result_reservation_holds_while_wal_accumulates(tmp_path, monkeypatch):
    """checkpoint 不发生时（-wal 只增不减），逐个 Job 提交的真实增量仍被单 Job 预留覆盖。"""
    data_dir = tmp_path / "data"
    cap = 256 * 1024
    monkeypatch.setattr(store_module, "MAX_RESULT_BYTES", cap)
    payload = b"p" * 4096
    with HttpStore(data_dir, inference_ready=lambda: True) as store:
        reserve = store._result_reservation_bytes()
        store.conn.execute("PRAGMA wal_autocheckpoint=0")
        for index in range(3):
            job = _pending_job(store, payload, f"j{index}")
            result, encoded = _sized_result(job.job_id, cap)
            before = _db_file_bytes(data_dir)
            store.record_result(job.job_id, result)
            growth = _db_file_bytes(data_dir) - before
            print(f"[wal-accumulate] job#{index} payload={encoded} growth={growth} reserve={reserve}")
            assert growth <= reserve
        wal = os.stat(str(data_dir / "http.sqlite3") + "-wal").st_size
        assert wal >= 2 * cap, "关闭自动 checkpoint 后 WAL 应累积多个结果"
        store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # 全部 checkpoint 之后，结果页真的落进主库文件
        assert _db_file_bytes(data_dir) >= 3 * cap


# ------------------------------------------------------------------ 物理余量


@needs_aiohttp
@pytest.mark.asyncio
async def test_physical_margin_thresholds_on_create_patch_and_commit(tmp_path, monkeypatch):
    """余量类阈值：余量-1 拒 507、余量等值放行；create / PATCH / commit 都覆盖。"""
    data_dir = tmp_path / "httpdata"
    margin = 8 * MB
    monkeypatch.setattr(store_module, "FREE_SPACE_MARGIN_BYTES", margin)
    payload = b"p" * 4096
    async with _running_server(data_dir) as (server, base):
        async with httpx.AsyncClient(timeout=30) as client:
            # create：free 恰好等于 margin + 声明长度时等值放行
            monkeypatch.setattr(store_module.shutil, "disk_usage",
                                _fixed_free(margin + len(payload)))
            created = await _http_create(client, base, payload, "t1", "k1")
            assert created.status_code == 201, created.text
            upload_id = created.json()["upload_id"]
            # 余量-1：同样的声明再也放不下
            monkeypatch.setattr(store_module.shutil, "disk_usage",
                                _fixed_free(margin + len(payload) - 1))
            refused = await _http_create(client, base, payload, "t2", "k2")
            assert refused.status_code == 507
            assert refused.json()["code"] == "disk_guard_full"
            assert _row_count(data_dir, "uploads") == 1

            # PATCH：未物化尾 = 声明长度 - 真实文件长度
            written = 2048
            monkeypatch.setattr(store_module.shutil, "disk_usage",
                                _fixed_free(margin + len(payload)))
            assert (await _http_patch(client, base, upload_id, "t1", 0, payload[:written])).status_code == 204
            tail = len(payload) - written
            monkeypatch.setattr(store_module.shutil, "disk_usage", _fixed_free(margin + tail - 1))
            blocked = await _http_patch(client, base, upload_id, "t1", written, payload[written:])
            assert blocked.status_code == 507
            assert blocked.json()["code"] == "disk_guard_full"
            assert _offset_of(data_dir, upload_id) == written
            assert _source_path(data_dir, upload_id).stat().st_size == written
            # 余量等值：放行，且 ACK offset 与真实文件字节一致
            monkeypatch.setattr(store_module.shutil, "disk_usage", _fixed_free(margin + tail))
            allowed = await _http_patch(client, base, upload_id, "t1", written, payload[written:])
            assert allowed.status_code == 204
            assert allowed.headers["Upload-Offset"] == str(len(payload))
            assert _source_path(data_dir, upload_id).stat().st_size == len(payload)
            assert _source_path(data_dir, upload_id).read_bytes() == payload
            assert _offset_of(data_dir, upload_id) == len(payload)

            # commit：本次新增 Job 的结果预留必须进余量账
            per_job = await server._worker.run(server._store._result_reservation_bytes)
            monkeypatch.setattr(store_module.shutil, "disk_usage",
                                _fixed_free(margin + per_job - 1))
            blocked_commit = await _http_commit(client, base, upload_id, "t1")
            assert blocked_commit.status_code == 507
            assert blocked_commit.json()["code"] == "disk_guard_full"
            assert _row_count(data_dir, "jobs") == 0
            assert _offset_of(data_dir, upload_id) == len(payload)
            monkeypatch.setattr(store_module.shutil, "disk_usage",
                                _fixed_free(margin + per_job))
            accepted = await _http_commit(client, base, upload_id, "t1")
            assert accepted.status_code == 202, accepted.text
            assert _row_count(data_dir, "jobs") == 1


@needs_aiohttp
@pytest.mark.asyncio
async def test_physical_margin_accounts_every_committed_tail_not_only_own(tmp_path, monkeypatch):
    """余量账必须覆盖其他所有已承诺上传的尾；只看本请求那一块会静默超卖。"""
    data_dir = tmp_path / "httpdata"
    margin = 8 * MB
    monkeypatch.setattr(store_module, "FREE_SPACE_MARGIN_BYTES", margin)
    size = 512 * 1024
    first = b"a" * size
    second = b"b" * size
    async with _running_server(data_dir) as (server, base):
        async with httpx.AsyncClient(timeout=60) as client:
            first_id = (await _http_create(client, base, first, "t1", "k1")).json()["upload_id"]
            second_id = (await _http_create(client, base, second, "t2", "k2")).json()["upload_id"]
            # free 刚好够两条尾：两条都写得下
            monkeypatch.setattr(store_module.shutil, "disk_usage", _fixed_free(margin + 2 * size))
            assert (await _http_patch(client, base, first_id, "t1", 0, first)).status_code == 204
            # 余量只剩一条尾少一字节时，第二条超卖：若只看本请求自己的尾就会被放行
            monkeypatch.setattr(store_module.shutil, "disk_usage", _fixed_free(margin + size - 1))
            blocked = await _http_patch(client, base, second_id, "t2", 0, second)
            assert blocked.status_code == 507
            assert blocked.json()["code"] == "disk_guard_full"
            assert _source_path(data_dir, second_id).stat().st_size == 0
            assert _offset_of(data_dir, second_id) == 0
            # 余量放开后第二条照常写：闸是真的，不是一律失败
            monkeypatch.setattr(store_module.shutil, "disk_usage", _fixed_free(margin + size))
            assert (await _http_patch(client, base, second_id, "t2", 0, second)).status_code == 204
            assert _source_path(data_dir, second_id).stat().st_size == size


def test_physical_margin_does_not_double_charge_written_source_bytes(tmp_path, monkeypatch):
    """已写盘字节只在 disk_usage 里扣一次；否则余量账会随写入进度一路下滑到拒收。"""
    data_dir = tmp_path / "data"
    margin = 1 * MB
    size = 64 * 1024
    monkeypatch.setattr(store_module, "FREE_SPACE_MARGIN_BYTES", margin)
    monkeypatch.setattr(store_module, "DB_GUARD_BYTES", 64 * MB)
    payload = b"p" * size
    with HttpStore(data_dir) as store:
        # 余量-1：等值边界之下连创建都放不下
        monkeypatch.setattr(store_module.shutil, "disk_usage",
                            _tracking_source_free(data_dir, margin + size - 1))
        with pytest.raises(HttpStoreError) as info:
            _create(store, payload)
        assert info.value.code == "disk_guard_full"
        assert _row_count(data_dir, "uploads") == 0
        assert list((data_dir / "sources").iterdir()) == []

        # 等值放行；随后每写一块，free 同步减少、未物化承诺同步减少，余量账保持不变。
        # 若已写字节被重复收费，第二块就会跌破余量而 507。
        monkeypatch.setattr(store_module.shutil, "disk_usage",
                            _tracking_source_free(data_dir, margin + size))
        record = _create(store, payload)
        offset = 0
        while offset < size:
            offset = store.append_bytes(record.upload_id, "tok", offset,
                                        payload[offset:offset + 4096])
        assert offset == size
        assert _source_path(data_dir, record.upload_id).stat().st_size == size
        assert _source_path(data_dir, record.upload_id).read_bytes() == payload
        assert store.get_upload(record.upload_id, "tok").confirmed_offset == size


def test_physical_margin_counts_unconfirmed_written_tail_as_materialized(tmp_path, monkeypatch):
    """崩溃残留的「已写入但未确认尾」占着物理空间，不能再当未物化承诺收一遍费。"""
    data_dir = tmp_path / "data"
    margin = 1 * MB
    size = 32 * 1024
    monkeypatch.setattr(store_module, "FREE_SPACE_MARGIN_BYTES", margin)
    monkeypatch.setattr(store_module, "DB_GUARD_BYTES", 64 * MB)
    payload = b"p" * size
    with HttpStore(data_dir) as store:
        record = _create(store, payload)
        store.append_bytes(record.upload_id, "tok", 0, payload[:8])
        residue = b"garbage-tail" * 64
        with open(_source_path(data_dir, record.upload_id), "ab") as handle:
            handle.write(residue)
        source = _source_path(data_dir, record.upload_id)
        assert source.stat().st_size == 8 + len(residue)
        assert store._unmaterialized_source_bytes() == size - source.stat().st_size
        # free 固定：未物化按「声明 - 真实文件长度」算，含残留尾，所以恰好等值放行
        monkeypatch.setattr(store_module.shutil, "disk_usage",
                            _fixed_free(margin + size - source.stat().st_size))
        assert store.append_bytes(record.upload_id, "tok", 8, payload[8:]) == size
        assert source.read_bytes() == payload
        assert store._unmaterialized_source_bytes() == 0


def test_disk_usage_failure_is_not_treated_as_available_capacity(tmp_path, monkeypatch):
    """disk_usage 读不出来必须显式上抛：不许当「有容量」放行，也不许当成 0 而拒收。"""
    data_dir = tmp_path / "data"
    payload = b"p" * 4096

    def boom(_path):
        raise OSError(5, "I/O error", str(data_dir))

    with HttpStore(data_dir, inference_ready=lambda: True) as store:
        written_upload = _uploaded(store, payload)
        probe = _create(store, b"z" * 4096, "tok3", "k3")
        monkeypatch.setattr(store_module.shutil, "disk_usage", boom)
        with pytest.raises(OSError):
            _create(store, b"q", "t2", "k2")
        with pytest.raises(OSError):
            store.append_bytes(probe.upload_id, "tok3", 0, b"z" * 8)
        # 失败请求没有留下任何痕迹：既没有新行，也没有多写一个字节
        assert _row_count(data_dir, "uploads") == 2
        assert _offset_of(data_dir, probe.upload_id) == 0
        assert _source_path(data_dir, probe.upload_id).stat().st_size == 0
        assert _source_path(data_dir, written_upload.upload_id).read_bytes() == payload


def test_missing_database_file_is_not_treated_as_zero_bytes(tmp_path):
    """DB 主文件缺失是显式失败，不能把未知占用当成 0 字节从而放行。"""
    data_dir = tmp_path / "data"
    payload = b"p" * 4096
    with HttpStore(data_dir, inference_ready=lambda: True) as store:
        _uploaded(store, payload)
        os.unlink(data_dir / "http.sqlite3")
        with pytest.raises(HttpStoreError) as info:
            _create(store, b"q", "t2", "k2")
        assert info.value.code == "store_unavailable"
        assert info.value.status == 503


# ------------------------------------------------------------------ 身份与事务


@needs_aiohttp
@pytest.mark.asyncio
async def test_capacity_full_still_replays_commit_and_serves_old_reads(tmp_path, monkeypatch):
    """容量满时：重放拿回同一 Job、旧 job/result 查询照常；拒收只发生在新增承诺上。"""
    data_dir = tmp_path / "httpdata"
    payload = b"p" * 4096
    async with _running_server(data_dir) as (server, base):
        async with httpx.AsyncClient(timeout=30) as client:
            upload_id = (await _http_create(client, base, payload, "t1", "k1")).json()["upload_id"]
            await _http_patch(client, base, upload_id, "t1", 0, payload)
            done = await _http_commit(client, base, upload_id, "t1")
            assert done.status_code == 202
            done_job = done.json()["job_id"]
            await server._worker.run(server._store.record_result, done_job,
                                     _final_result(done_job, "第一条已完成"))
            failed_upload = (await _http_create(client, base, payload, "t2", "k2")).json()["upload_id"]
            await _http_patch(client, base, failed_upload, "t2", 0, payload)
            failed_job = (await _http_commit(client, base, failed_upload, "t2")).json()["job_id"]
            await server._worker.run(server._store.fail_job, failed_job, "probe_failure")

            # 压到「一个待处理 Job 的预留都放不下」：任何新增承诺都 507
            per_job = await server._worker.run(server._store._result_reservation_bytes)
            monkeypatch.setattr(store_module, "DB_GUARD_BYTES", _db_file_bytes(data_dir) - 1)
            fresh = await _http_create(client, base, payload, "t3", "k3")
            assert fresh.status_code == 507
            assert fresh.json()["code"] == "storage_guard_full"
            # 等值放行（额度类等值合法）：没有待处理 Job 时实测占用正好等于上限
            monkeypatch.setattr(store_module, "DB_GUARD_BYTES", _db_file_bytes(data_dir))
            pending_id = (await _http_create(client, base, payload, "t3", "k3")).json()["upload_id"]
            await _http_patch(client, base, pending_id, "t3", 0, payload)
            # 本次新增 Job 的预留放不下：commit 507
            monkeypatch.setattr(store_module, "DB_GUARD_BYTES", _db_file_bytes(data_dir))
            blocked = await _http_commit(client, base, pending_id, "t3")
            assert blocked.status_code == 507
            assert blocked.json()["code"] == "storage_guard_full"
            # 余出一格预留后受理
            monkeypatch.setattr(store_module, "DB_GUARD_BYTES", _db_file_bytes(data_dir) + per_job)
            accepted = await _http_commit(client, base, pending_id, "t3")
            assert accepted.status_code == 202
            # 再压满：一个待处理 Job 的预留也放不下
            monkeypatch.setattr(store_module, "DB_GUARD_BYTES", _db_file_bytes(data_dir))

            # 幂等重放：同一 upload 再 commit 必须 200 拿回同一个 Job，且不增加 pending 预算
            replay = await _http_commit(client, base, upload_id, "t1")
            assert replay.status_code == 200
            assert replay.json()["job_id"] == done_job
            replay_failed = await _http_commit(client, base, failed_upload, "t2")
            assert replay_failed.status_code == 200
            assert replay_failed.json()["job_id"] == failed_job
            assert replay_failed.json()["state"] == "FAILED"

            # 旧查询不受容量影响：DONE 结果 200 完整，FAILED 结果 409 + error_code
            done_read = await client.get(f"{base}/v1/jobs/{done_job}/result",
                                         headers={"Authorization": "Bearer t1"})
            assert done_read.status_code == 200
            assert done_read.json()["text"] == "第一条已完成"
            job_read = await client.get(f"{base}/v1/jobs/{done_job}",
                                        headers={"Authorization": "Bearer t1"})
            assert job_read.status_code == 200
            assert job_read.json()["state"] == "DONE"
            assert job_read.json()["result_available"] is True
            failed_read = await client.get(f"{base}/v1/jobs/{failed_job}/result",
                                           headers={"Authorization": "Bearer t2"})
            assert failed_read.status_code == 409
            assert failed_read.json()["code"] == "job_failed"
            assert failed_read.json()["error_code"] == "probe_failure"
            assert _row_count(data_dir, "jobs") == 3


@needs_aiohttp
@pytest.mark.asyncio
async def test_create_guard_edges_have_no_side_effect(tmp_path, monkeypatch):
    """DB guard 的等值/超一字节两侧：等值放行，超出则不留 uploads 行与源文件。"""
    data_dir = tmp_path / "httpdata"
    payload = b"p" * 4096
    async with _running_server(data_dir) as (server, base):
        async with httpx.AsyncClient(timeout=30) as client:
            db_now = _db_file_bytes(data_dir)
            monkeypatch.setattr(store_module, "DB_GUARD_BYTES", db_now - 1)
            refused = await _http_create(client, base, payload, "t1", "k1")
            assert refused.status_code == 507
            assert refused.json()["code"] == "storage_guard_full"
            assert _row_count(data_dir, "uploads") == 0
            assert list((data_dir / "sources").iterdir()) == []
            # 等值放行（额度类等值合法）
            monkeypatch.setattr(store_module, "DB_GUARD_BYTES", db_now)
            assert (await _http_create(client, base, payload, "t1", "k1")).status_code == 201


# ------------------------------------------------------------------ 并发（真实 TCP）


def _trace_spans(store, name: str):
    """给 store 方法套一层区间记录：证明两个并发请求在 store 内没有交叠。"""
    original = getattr(store, name)
    spans: list[tuple[float, float]] = []
    lock = threading.Lock()

    def traced(*args, **kwargs):
        start = time.monotonic()
        try:
            return original(*args, **kwargs)
        finally:
            with lock:
                spans.append((start, time.monotonic()))

    setattr(store, name, traced)
    return spans, lambda: setattr(store, name, original)


@needs_aiohttp
@pytest.mark.asyncio
async def test_two_real_tcp_commits_race_for_last_storage_capacity(tmp_path, monkeypatch):
    """两个真实 TCP commit 争最后一个容量名额：只应有一个真被受理。"""
    data_dir = tmp_path / "httpdata"
    payload = b"p" * 4096
    async with _running_server(data_dir) as (server, base):
        async with httpx.AsyncClient(timeout=30) as client:
            upload_ids = []
            for index in (1, 2):
                upload_id = (await _http_create(client, base, payload, f"t{index}",
                                                f"k{index}")).json()["upload_id"]
                assert (await _http_patch(client, base, upload_id, f"t{index}", 0,
                                          payload)).status_code == 204
                upload_ids.append(upload_id)
            per_job = await server._worker.run(server._store._result_reservation_bytes)
            # 只放得下一个待处理 Job 的预留
            monkeypatch.setattr(store_module, "DB_GUARD_BYTES", _db_file_bytes(data_dir) + per_job)

            spans, restore = _trace_spans(server._store, "commit_upload")
            try:
                responses = await asyncio.gather(
                    _http_commit(client, base, upload_ids[0], "t1"),
                    _http_commit(client, base, upload_ids[1], "t2"),
                )
            finally:
                restore()

            assert sorted(r.status_code for r in responses) == [202, 507], \
                [r.text for r in responses]
            assert [r for r in responses if r.status_code == 507][0].json()["code"] == "storage_guard_full"
            accepted = [r for r in responses if r.status_code == 202]
            # 返回 202 的真实 Job 数与 SQLite 行数一致
            assert len(accepted) == 1
            assert [row["job_id"] for row in _read_rows(data_dir, "SELECT job_id FROM jobs")] == \
                [accepted[0].json()["job_id"]]
            assert _row_count(data_dir, "jobs") == 1
            # 两个请求在 store 内不交叠：单 I/O worker 让「查预算→登记」原子化
            assert len(spans) == 2
            (start_a, end_a), (start_b, end_b) = sorted(spans)
            assert end_a <= start_b or end_b <= start_a, f"store 区间交叠：{spans}"
            assert await server._worker.run(server._store._pending_result_reservation_bytes) == per_job


@needs_aiohttp
@pytest.mark.asyncio
async def test_two_real_patches_race_for_physical_margin(tmp_path, monkeypatch):
    """两个真实 TCP PATCH 争物理余量：余量不足时都不写盘、offset 都不前进。"""
    data_dir = tmp_path / "httpdata"
    margin = 8 * MB
    monkeypatch.setattr(store_module, "FREE_SPACE_MARGIN_BYTES", margin)
    size = 512 * 1024
    payloads = {"t1": b"a" * size, "t2": b"b" * size}
    async with _running_server(data_dir) as (server, base):
        async with httpx.AsyncClient(timeout=60) as client:
            upload_ids = {}
            for token, blob in payloads.items():
                response = await _http_create(client, base, blob, token, f"key-{token}")
                upload_ids[token] = response.json()["upload_id"]
            # free 只够一条尾：两条都超卖，两个请求都必须被拒
            monkeypatch.setattr(store_module.shutil, "disk_usage", _fixed_free(margin + size))
            spans, restore = _trace_spans(server._store, "append_bytes")
            try:
                responses = await asyncio.gather(*[
                    _http_patch(client, base, upload_ids[token], token, 0, blob)
                    for token, blob in payloads.items()
                ])
            finally:
                restore()

            assert [r.status_code for r in responses] == [507, 507]
            assert all(r.json()["code"] == "disk_guard_full" for r in responses)
            for token in payloads:
                assert _source_path(data_dir, upload_ids[token]).stat().st_size == 0
                assert _offset_of(data_dir, upload_ids[token]) == 0
            (start_a, end_a), (start_b, end_b) = sorted(spans)
            assert len(spans) == 2
            assert end_a <= start_b or end_b <= start_a, f"store 区间交叠：{spans}"
            # 余量放开后同一条路径确实放行（闸是真的，不是一律失败）
            monkeypatch.setattr(store_module.shutil, "disk_usage", _fixed_free(margin + 2 * size))
            allowed = await _http_patch(client, base, upload_ids["t1"], "t1", 0, payloads["t1"])
            assert allowed.status_code == 204
            assert allowed.headers["Upload-Offset"] == str(size)
            assert _source_path(data_dir, upload_ids["t1"]).read_bytes() == payloads["t1"]


# ------------------------------------------------------------------ 跨重启


def test_restart_releases_pending_but_keeps_partial_and_skips_deleted_terminal(tmp_path, monkeypatch):
    """重启把 QUEUED/RUNNING 收敛成 FAILED 后 pending 不再永久收费；
    保留的 partial 与保留的终态源仍计费，已删除的终态源不复活也不多计。"""
    data_dir = tmp_path / "data"
    size = 4096
    payload = b"p" * size
    store = HttpStore(data_dir, inference_ready=lambda: True).open()
    try:
        partial = _create(store, payload, "t-partial", "k-partial")
        store.append_bytes(partial.upload_id, "t-partial", 0, payload[:8])
        queued_upload = _uploaded(store, payload, "t-queued", "k-queued")
        queued_job = store.commit_upload(queued_upload.upload_id, "t-queued")
        done_upload = _uploaded(store, payload, "t-done", "k-done")
        done_job = store.commit_upload(done_upload.upload_id, "t-done")
        store.record_result(done_job.job_id, _final_result(done_job.job_id))
        assert store._pending_result_reservation_bytes() == store._result_reservation_bytes()
        # C2 的第一步：到期终态源的 unlink 已完成
        os.unlink(_source_path(data_dir, done_upload.upload_id))
    finally:
        store.close()

    reopened = HttpStore(data_dir).open()
    try:
        assert reopened.get_upload(queued_upload.upload_id, "t-queued").state == "COMMITTED"
        assert reopened.job_record(queued_job.job_id, "t-queued").state == "FAILED"
        assert reopened._pending_result_reservation_bytes() == 0, "FAILED 的 Job 不该继续收费"
        # 只认「仍在磁盘上的已登记源」：partial + 收敛后的 FAILED 源 = 2 份
        assert reopened._reserved_source_bytes() == 2 * size
        # 等值放行：再加一份正好用满三份
        monkeypatch.setattr(store_module, "SOURCE_RESERVE_BYTES", 3 * size)
        assert _create(reopened, payload, "t-new", "k-new").upload_id
        monkeypatch.setattr(store_module, "SOURCE_RESERVE_BYTES", 3 * size - 1)
        with pytest.raises(HttpStoreError) as info:
            _create(reopened, payload, "t-new2", "k-new2")
        assert info.value.code == "source_reserve_full"
        # 外部身份、结果与 partial 前缀都没动
        assert reopened.get_upload(partial.upload_id, "t-partial").confirmed_offset == 8
        assert reopened.get_result(done_job.job_id, "t-done")["text"] == "已完成"
        assert not _source_path(data_dir, done_upload.upload_id).exists()
        assert _source_path(data_dir, partial.upload_id).stat().st_size == 8
        assert _row_count(data_dir, "uploads") == 4
    finally:
        reopened.close()