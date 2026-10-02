# coding: utf-8
"""HttpStore 的持久性契约测试：真实 SQLite、真实文件字节、真实子进程竞争。"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
from hashlib import sha256
from pathlib import Path

import pytest

from core.server.http_store import (
    MAX_OPEN_UPLOADS,
    UPLOAD_TTL_SECONDS,
    DataDirLocked,
    HttpStore,
    HttpStoreError,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

LOCK_PROBE = '''\
import sys
from pathlib import Path

sys.path.insert(0, {repo!r})
from core.server.http_store import HttpStore, HttpStoreError

try:
    store = HttpStore(Path({data_dir!r})).open()
except HttpStoreError as exc:
    print(f"LOCKED:{{exc.code}}")
    raise SystemExit(3)
store.close()
print("ACQUIRED")
raise SystemExit(0)
'''


def _options():
    return {
        "model": None,
        "language": None,
        "context": None,
        "seg_duration": 15.0,
        "seg_overlap": 2.0,
    }


def _upload(store: HttpStore, payload: bytes, token: str = "token-a", create_key: str = "key-a"):
    return store.create_upload(
        size_bytes=len(payload),
        sha256=sha256(payload).hexdigest(),
        options=_options(),
        token=token,
        create_key=create_key,
    )


def _final_result(job_id: str, text: str = "已完成") -> dict:
    return {
        "task_id": job_id,
        "socket_id": "",
        "type": "file",
        "owner_kind": "http",
        "duration": 1.0,
        "time_start": 1.0,
        "time_submit": 2.0,
        "time_complete": 3.0,
        "text": text,
        "text_accu": text,
        "tokens": [text],
        "timestamps": [0.1],
        "is_final": True,
    }


def test_create_is_exclusive_file_then_zero_offset_commit(tmp_path):
    payload = b"abcdefghij" * 10
    with HttpStore(tmp_path / "data") as store:
        record = _upload(store, payload)
        assert record.state == "UPLOADING"
        assert record.confirmed_offset == 0
        assert record.job_id is None
        assert record.expires_at > time.time()
        source = tmp_path / "data" / "sources" / f"{record.upload_id}.bin"
        assert source.stat().st_size == 0
        # 令牌只以指纹落库，明文不得出现在数据库里
        blob = (tmp_path / "data" / "http.sqlite3").read_bytes()
        assert b"token-a" not in blob


def test_append_fsync_offset_transaction_and_ack_order(tmp_path):
    payload = b"0123456789"
    with HttpStore(tmp_path / "data") as store:
        record = _upload(store, payload)
        assert store.append_bytes(record.upload_id, "token-a", 0, payload[:4]) == 4
        source = tmp_path / "data" / "sources" / f"{record.upload_id}.bin"
        assert source.read_bytes() == payload[:4]
        # 旧 offset / 超前 offset 都拒绝，并带可信 offset
        with pytest.raises(HttpStoreError) as info:
            store.append_bytes(record.upload_id, "token-a", 0, payload[4:8])
        assert info.value.status == 409
        assert info.value.confirmed_offset == 4
        with pytest.raises(HttpStoreError) as ahead:
            store.append_bytes(record.upload_id, "token-a", 6, payload[4:6])
        assert ahead.value.confirmed_offset == 4
        assert store.append_bytes(record.upload_id, "token-a", 4, payload[4:]) == 10
        assert source.read_bytes() == payload


def test_append_uses_portable_positioned_write_and_completes_short_writes(tmp_path, monkeypatch):
    payload = b"portable-write-payload"
    with HttpStore(tmp_path / "data") as store:
        record = _upload(store, payload)
        original_write = os.write

        def short_write(fd, data):
            return original_write(fd, data[:max(1, len(data) // 2)])

        # 旧实现会调用 pwrite；让它声称写入成功但不写字节，最终文件字节断言必须变红。
        with monkeypatch.context() as mutation:
            mutation.setattr(os, "write", short_write)
            mutation.setattr(os, "pwrite", lambda _fd, data, _offset: len(data), raising=False)
            assert store.append_bytes(record.upload_id, "token-a", 0, payload) == len(payload)
        source = tmp_path / "data" / "sources" / f"{record.upload_id}.bin"
        assert source.read_bytes() == payload
        assert store.get_upload(record.upload_id, "token-a").confirmed_offset == len(payload)


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are not Windows ACL proof")
def test_http_store_files_are_private_under_umask_0002(tmp_path):
    data_dir = tmp_path / "private-data"
    previous_umask = os.umask(0o002)
    try:
        with HttpStore(data_dir) as store:
            payload = b"private-source"
            record = _upload(store, payload)
            store.append_bytes(record.upload_id, "token-a", 0, payload)
            source = data_dir / "sources" / f"{record.upload_id}.bin"
            targets = {
                data_dir: 0o700,
                data_dir / "sources": 0o700,
                data_dir / "http.lock": 0o600,
                data_dir / "http.sqlite3": 0o600,
                data_dir / "http.sqlite3-wal": 0o600,
                data_dir / "http.sqlite3-shm": 0o600,
                source: 0o600,
            }
            modes = {path: stat.S_IMODE(path.stat().st_mode) for path in targets}
            assert modes == targets
    finally:
        os.umask(previous_umask)


def test_unconfirmed_tail_is_truncated_to_database_offset(tmp_path):
    """崩溃残留的未确认尾不能冒充已确认：下一次写入按 DB offset 截断后再写。"""
    payload = b"A" * 20
    with HttpStore(tmp_path / "data") as store:
        record = _upload(store, payload)
        store.append_bytes(record.upload_id, "token-a", 0, payload[:8])
        source = tmp_path / "data" / "sources" / f"{record.upload_id}.bin"
        with open(source, "ab") as handle:  # 模拟 ACK 前崩溃留下的尾字节
            handle.write(b"garbage-tail")
        assert source.stat().st_size == 8 + len(b"garbage-tail")
        store.append_bytes(record.upload_id, "token-a", 8, payload[8:12])
        assert source.read_bytes() == payload[:12]
        assert store.get_upload(record.upload_id, "token-a").confirmed_offset == 12


def test_real_sqlite_busy_does_not_ack(tmp_path):
    """真实 SQLite 锁：busy_timeout=0 下立即失败，绝不假装 ACK 成功。"""
    payload = b"x" * 32
    with HttpStore(tmp_path / "data") as store:
        record = _upload(store, payload)
        blocker = sqlite3.connect(tmp_path / "data" / "http.sqlite3", timeout=0, isolation_level=None)
        blocker.execute("PRAGMA busy_timeout=0")
        blocker.execute("BEGIN EXCLUSIVE")
        try:
            with pytest.raises(sqlite3.OperationalError):
                store.append_bytes(record.upload_id, "token-a", 0, payload[:8])
            # 没有 ACK：数据库 offset 不前进（字节可能已落盘，但不算已确认）
            assert store.get_upload(record.upload_id, "token-a").confirmed_offset == 0
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()
        # 锁释放后重试：未确认尾被截断到 DB offset，最终字节仍是源字节
        assert store.append_bytes(record.upload_id, "token-a", 0, payload[:8]) == 8
        assert (tmp_path / "data" / "sources" / f"{record.upload_id}.bin").read_bytes() == payload[:8]


def test_commit_requires_real_inference_coordinator(tmp_path):
    payload = b"y" * 16
    with HttpStore(tmp_path / "data") as store:
        record = _upload(store, payload)
        store.append_bytes(record.upload_id, "token-a", 0, payload)
        with pytest.raises(HttpStoreError) as info:
            store.commit_upload(record.upload_id, "token-a")
        assert info.value.code == "inference_unavailable"
        assert info.value.status == 503
        # 503 不受理：状态仍是 UPLOADING，也没有 Job
        assert store.get_upload(record.upload_id, "token-a").state == "UPLOADING"
        assert store.conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 0


def test_commit_creates_single_job_and_repeat_returns_same(tmp_path):
    payload = b"z" * 16
    store = HttpStore(tmp_path / "data", inference_ready=lambda: True).open()
    try:
        record = _upload(store, payload)
        store.append_bytes(record.upload_id, "token-a", 0, payload)
        job = store.commit_upload(record.upload_id, "token-a")
        assert job.state == "QUEUED"
        assert job.source_available is True
        assert job.result_available is False
        again = store.commit_upload(record.upload_id, "token-a")
        assert again.job_id == job.job_id
        assert store.conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 1
        assert store.get_upload(record.upload_id, "token-a").state == "COMMITTED"
        # 已受理的上传不能再写
        with pytest.raises(HttpStoreError) as info:
            store.append_bytes(record.upload_id, "token-a", len(payload), b"more")
        assert info.value.status == 409
    finally:
        store.close()


def test_commit_rejects_wrong_identity_without_losing_prefix(tmp_path):
    payload = b"good" * 8
    with HttpStore(tmp_path / "data", inference_ready=lambda: True) as store:
        record = _upload(store, payload)
        store.append_bytes(record.upload_id, "token-a", 0, payload)
        source = tmp_path / "data" / "sources" / f"{record.upload_id}.bin"
        with open(source, "ab") as handle:
            handle.write(b"tampered")
        with pytest.raises(HttpStoreError) as info:
            store.commit_upload(record.upload_id, "token-a")
        assert info.value.code == "integrity_mismatch"
        assert info.value.status == 422
        assert store.get_upload(record.upload_id, "token-a").state == "UPLOADING"
        assert store.get_upload(record.upload_id, "token-a").confirmed_offset == len(payload)


def test_restart_converges_queued_and_running_but_keeps_partial_and_done(tmp_path):
    payload = b"partial-and-final" * 4
    store = HttpStore(tmp_path / "data", inference_ready=lambda: True).open()
    try:
        partial = _upload(store, payload, token="token-p", create_key="key-p")
        store.append_bytes(partial.upload_id, "token-p", 0, payload[:16])
        queued = _upload(store, payload, token="token-q", create_key="key-q")
        store.append_bytes(queued.upload_id, "token-q", 0, payload)
        job_q = store.commit_upload(queued.upload_id, "token-q")
        conn = store.conn
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE jobs SET state='RUNNING' WHERE job_id=?", (job_q.job_id,))
        conn.execute("COMMIT")
        done = _upload(store, payload, token="token-d", create_key="key-d")
        store.append_bytes(done.upload_id, "token-d", 0, payload)
        job_d = store.commit_upload(done.upload_id, "token-d")
        store.record_result(job_d.job_id, _final_result(job_d.job_id))
    finally:
        store.close()

    reopened = HttpStore(tmp_path / "data").open()
    try:
        rows = {
            row["job_id"]: row
            for row in reopened.conn.execute("SELECT job_id, state, error_code FROM jobs")
        }
        assert rows[job_q.job_id]["state"] == "FAILED"
        assert rows[job_q.job_id]["error_code"] == "server_restarted"
        assert rows[job_d.job_id]["state"] == "DONE"
        assert rows[job_d.job_id]["error_code"] is None
        assert reopened.get_result(job_d.job_id, "token-d")["text"] == "已完成"
        # partial 前缀不动
        assert reopened.get_upload(partial.upload_id, "token-p").confirmed_offset == 16
        assert (tmp_path / "data" / "sources" / f"{partial.upload_id}.bin").stat().st_size == 16
        assert reopened.get_upload(queued.upload_id, "token-q").state == "COMMITTED"
    finally:
        reopened.close()


def test_record_result_requires_matching_complete_payload_and_preserves_terminal_jobs(tmp_path):
    payload = b"result-source"
    store = HttpStore(tmp_path / "data", inference_ready=lambda: True).open()
    try:
        upload = _upload(store, payload, token="result-token", create_key="result-key")
        store.append_bytes(upload.upload_id, "result-token", 0, payload)
        job = store.commit_upload(upload.upload_id, "result-token")

        with pytest.raises(HttpStoreError) as mismatch:
            store.record_result(job.job_id, _final_result("different-task"))
        assert mismatch.value.code == "invalid_result"
        partial = _final_result(job.job_id)
        partial["is_final"] = False
        with pytest.raises(HttpStoreError) as unfinished:
            store.record_result(job.job_id, partial)
        assert unfinished.value.code == "invalid_result"
        missing_field = _final_result(job.job_id)
        del missing_field["timestamps"]
        with pytest.raises(HttpStoreError) as incomplete:
            store.record_result(job.job_id, missing_field)
        assert incomplete.value.code == "invalid_result"
        assert store.conn.execute(
            "SELECT state FROM jobs WHERE job_id=?", (job.job_id,)
        ).fetchone()["state"] == "QUEUED"
        assert store.conn.execute(
            "SELECT COUNT(*) FROM results WHERE job_id=?", (job.job_id,)
        ).fetchone()[0] == 0

        store.record_result(job.job_id, _final_result(job.job_id))
        before_job = dict(store.conn.execute(
            "SELECT state, time_complete, terminal_at, started_at FROM jobs WHERE job_id=?",
            (job.job_id,),
        ).fetchone())
        before_payload = store.conn.execute(
            "SELECT payload FROM results WHERE job_id=?", (job.job_id,)
        ).fetchone()["payload"]
        for text in ("迟到覆盖一", "迟到覆盖二"):
            with pytest.raises(HttpStoreError) as duplicate:
                store.record_result(job.job_id, _final_result(job.job_id, text))
            assert duplicate.value.code == "result_terminal"
        after_job = dict(store.conn.execute(
            "SELECT state, time_complete, terminal_at, started_at FROM jobs WHERE job_id=?",
            (job.job_id,),
        ).fetchone())
        after_payload = store.conn.execute(
            "SELECT payload FROM results WHERE job_id=?", (job.job_id,)
        ).fetchone()["payload"]
        assert after_job == before_job
        assert after_payload == before_payload

        failed_upload = _upload(store, payload, token="failed-token", create_key="failed-key")
        store.append_bytes(failed_upload.upload_id, "failed-token", 0, payload)
        failed = store.commit_upload(failed_upload.upload_id, "failed-token")
        store.conn.execute(
            "UPDATE jobs SET state='FAILED', error_code='probe' WHERE job_id=?", (failed.job_id,)
        )
        failed_before = dict(store.conn.execute(
            "SELECT state, error_code, time_complete, terminal_at FROM jobs WHERE job_id=?",
            (failed.job_id,),
        ).fetchone())
        with pytest.raises(HttpStoreError) as terminal:
            store.record_result(failed.job_id, _final_result(failed.job_id))
        assert terminal.value.code == "result_terminal"
        assert dict(store.conn.execute(
            "SELECT state, error_code, time_complete, terminal_at FROM jobs WHERE job_id=?",
            (failed.job_id,),
        ).fetchone()) == failed_before
        assert store.conn.execute(
            "SELECT COUNT(*) FROM results WHERE job_id=?", (failed.job_id,)
        ).fetchone()[0] == 0
    finally:
        store.close()


def test_expired_upload_is_410_and_metadata_kept(tmp_path):
    payload = b"expire-me"
    with HttpStore(tmp_path / "data") as store:
        record = _upload(store, payload)
        store.conn.execute("UPDATE uploads SET expires_at=? WHERE upload_id=?", (time.time() - 1, record.upload_id))
        with pytest.raises(HttpStoreError) as info:
            store.get_upload(record.upload_id, "token-a")
        assert info.value.status == 410
        assert info.value.code == "upload_expired"
        rows = store.conn.execute("SELECT state FROM uploads WHERE upload_id=?", (record.upload_id,)).fetchall()
        assert rows[0]["state"] == "EXPIRED"
        # 元数据不自动删除，源文件也不自动删除
        assert (tmp_path / "data" / "sources" / f"{record.upload_id}.bin").exists()


def test_wrong_token_and_unknown_upload_are_both_not_found(tmp_path):
    with HttpStore(tmp_path / "data") as store:
        record = _upload(store, b"data")
        for upload_id, token in ((record.upload_id, "wrong"), ("00000000-0000-4000-8000-000000000000", "token-a")):
            with pytest.raises(HttpStoreError) as info:
                store.get_upload(upload_id, token)
            assert info.value.status == 404
        assert UPLOAD_TTL_SECONDS == 7 * 24 * 3600


def test_admission_refuses_new_upload_without_touching_old_data(tmp_path):
    payload = b"0123456789"
    with HttpStore(tmp_path / "data") as store:
        first = _upload(store, payload, token="token-a", create_key="key-a")
        conn = store.conn
        for index in range(1, MAX_OPEN_UPLOADS):
            _upload(store, payload, token=f"token-{index}", create_key=f"key-{index}")
        with pytest.raises(HttpStoreError) as info:
            _upload(store, payload, token="token-overflow", create_key="key-overflow")
        assert info.value.status == 429
        # 拒收不删除旧数据
        assert store.get_upload(first.upload_id, "token-a").state == "UPLOADING"
        assert len(list((tmp_path / "data" / "sources").iterdir())) == MAX_OPEN_UPLOADS


def test_second_process_cannot_share_the_same_data_dir(tmp_path):
    """真实第二进程竞争同一目录：必须失败并给出明确退出码，不删锁猜活跃。"""
    data_dir = tmp_path / "data"
    probe = tmp_path / "probe.py"
    probe.write_text(LOCK_PROBE.format(repo=str(REPO_ROOT), data_dir=str(data_dir)), encoding="utf-8")
    store = HttpStore(data_dir).open()
    try:
        result = subprocess.run(
            [sys.executable, str(probe)], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL
        )
        assert result.returncode == 3, (result.returncode, result.stdout, result.stderr)
        assert "LOCKED:data_dir_locked" in result.stdout
    finally:
        store.close()
    # 锁释放后新进程可以正常获得
    result = subprocess.run(
        [sys.executable, str(probe)], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL
    )
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    assert "ACQUIRED" in result.stdout


def test_unsupported_schema_fails_fast_without_rebuilding(tmp_path):
    data_dir = tmp_path / "data"
    with HttpStore(data_dir) as store:
        _upload(store, b"payload")
    conn = sqlite3.connect(data_dir / "http.sqlite3")
    conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(HttpStoreError) as info:
        HttpStore(data_dir).open()
    assert info.value.code == "unsupported_schema"
    # 不删库重建：已确认前缀仍在
    conn = sqlite3.connect(data_dir / "http.sqlite3")
    try:
        assert conn.execute("SELECT COUNT(*) FROM uploads").fetchone()[0] == 1
    finally:
        conn.close()
