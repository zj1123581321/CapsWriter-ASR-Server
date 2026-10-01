# coding: utf-8
"""
HTTP 文件任务的本机持久存储（HttpStore）

职责边界：
  * 单个 server 进程对稳定数据目录持有 OS 独占锁，不支持多实例共写、不支持网络盘；
  * SQLite（WAL + synchronous=FULL + foreign_keys ON + busy_timeout=0）只在受监督的
    单 I/O worker 线程里创建和使用，绝不放回 WS 事件循环；
  * 可靠提交顺序固定为「字节 → fsync → offset 记录 → ACK」；完整文件身份核对后
    在同一事务里建立唯一 Job；完整 result 与 DONE 必须同一次提交；
  * 不做任何自动重试、静默 fallback 或后台猜测：SQLite busy、锁冲突、坏 schema
    一律 fail fast 上抛。

所有写入都走 SQL 参数化，文件路径只由服务端生成的 UUID 决定，不接受任何外部路径。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


# ---- R7 资源起点（启用 HTTP 后的起始 guard，不是实测吞吐）----
MAX_FILE_BYTES = 1 * 1024 * 1024 * 1024          # 1 GiB / file
MAX_CHUNK_BYTES = 1 * 1024 * 1024                # 1 MiB / PATCH
READ_CHUNK_BYTES = 64 * 1024                      # 64 KiB / read
MAX_JSON_BYTES = 16 * 1024                        # 16 KiB 小 JSON
MAX_HANDLERS = 16                                 # 16 并发 handler
MAX_BODY_CONCURRENCY = 2                          # 同时 body 操作 2
MAX_OPEN_UPLOADS = 32                             # 32 个未完成上传会话
MAX_HTTP_JOBS = 8                                 # 最多 8 个 QUEUED+RUNNING
IO_MAILBOX = 32                                   # I/O mailbox 32 有界操作
SOURCE_RESERVE_BYTES = 16 * 1024 * 1024 * 1024   # 16 GiB source 声明长度总预留
DB_GUARD_BYTES = 2 * 1024 * 1024 * 1024           # 2 GiB DB+WAL+SHM/结果整体 guard
MAX_RESULT_BYTES = 64 * 1024 * 1024               # 64 MiB 最终 JSON
FREE_SPACE_MARGIN_BYTES = 2 * 1024 * 1024 * 1024  # 实际剩余文件系统 2 GiB 安全余量
UPLOAD_TTL_SECONDS = 7 * 24 * 3600                # 未完成上传 TTL 7 天

SCHEMA_VERSION = 1

UPLOAD_UPLOADING = "UPLOADING"
UPLOAD_COMMITTED = "COMMITTED"
UPLOAD_EXPIRED = "EXPIRED"

JOB_QUEUED = "QUEUED"
JOB_RUNNING = "RUNNING"
JOB_DONE = "DONE"
JOB_FAILED = "FAILED"

OPTION_FIELDS = ("model", "language", "context", "seg_duration", "seg_overlap")


class HttpStoreError(Exception):
    """带机器可读 code 的存储/准入错误；一律由 route 原样映射为 HTTP 状态。"""

    def __init__(self, code: str, message: str, status: int = 400, confirmed_offset: Optional[int] = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status
        self.confirmed_offset = confirmed_offset


class StoreUnavailable(HttpStoreError):
    """存储不可读/不可写：调用方必须返回 503，不得回退到 offset=0 或空 DONE。"""

    def __init__(self, message: str = "HTTP 存储当前不可用"):
        super().__init__("store_unavailable", message, status=503)


class DataDirLocked(HttpStoreError):
    def __init__(self, message: str = "HTTP 数据目录已被另一个 server 实例独占"):
        super().__init__("data_dir_locked", message, status=503)


@dataclass(frozen=True)
class UploadRecord:
    upload_id: str
    state: str
    size_bytes: int
    sha256: str
    confirmed_offset: int
    expires_at: float
    job_id: Optional[str]

    def public(self) -> dict:
        payload = {
            "upload_id": self.upload_id,
            "state": self.state,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "confirmed_offset": self.confirmed_offset,
            "expires_at": _iso_utc(self.expires_at),
        }
        if self.job_id:
            payload["job_id"] = self.job_id
        return payload


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    state: str
    error_code: Optional[str]
    time_start: Optional[float]
    time_submit: Optional[float]
    time_complete: Optional[float]
    result_available: bool
    source_available: bool

    def public(self) -> dict:
        return {
            "job_id": self.job_id,
            "state": self.state,
            "error_code": self.error_code,
            "result_available": self.result_available,
            "source_available": self.source_available,
            "time_start": self.time_start,
            "time_submit": self.time_submit,
            "time_complete": self.time_complete,
        }


def _iso_utc(epoch: float) -> str:
    """统一 UTC ISO 字符串（带 Z），客户端按字符串读取 expires_at。"""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def hash_token(token: str) -> str:
    """只存令牌指纹，日志/URL/数据库都不出现明文令牌。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_matches(stored_hash: str, token: str) -> bool:
    return hmac.compare_digest(stored_hash, hash_token(token))


def _ensure_private_file(path: Path) -> None:
    """POSIX 下以 0600 建立或收紧 HTTP 持久文件，不受进程 umask 放宽。"""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def _acquire_exclusive_lock(lock_path: Path):
    """在 POSIX 上用 flock、在 Windows 上用 msvcrt 对单目录持有 OS 独占锁。

    不删除 PID 锁猜活跃：拿不到就是拿不到，直接 fail fast。
    """
    handle = open(lock_path, "a+b")
    if os.name == "posix":
        os.fchmod(handle.fileno(), 0o600)
    try:
        if os.name == "nt":  # pragma: no cover - Windows 部署路径
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise DataDirLocked(f"无法独占 HTTP 数据目录锁：{lock_path}（{exc}）") from exc
    return handle


class HttpStore:
    """上传/Job/结果的唯一持久真源。所有方法都是同步的，只能在 I/O worker 里调用。"""

    def __init__(self, data_dir: Path, inference_ready: Optional[Callable[[], bool]] = None):
        self.data_dir = Path(data_dir)
        self.sources_dir = self.data_dir / "sources"
        self.db_path = self.data_dir / "http.sqlite3"
        self.lock_path = self.data_dir / "http.lock"
        self._lock_handle = None
        self._conn: Optional[sqlite3.Connection] = None
        # 实际推理协调者（E3 的 runner）是否可用；本增量没有真实 runner 时恒为 False，
        # 于是外部 commit 明确 503，而不是受理后永远排队。
        self._inference_ready = inference_ready or (lambda: False)

    # ---------------- 生命周期 ----------------

    def open(self) -> "HttpStore":
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.data_dir.is_absolute():  # pragma: no cover - 由 resolve_http_settings 先行拦截
            raise HttpStoreError("invalid_data_dir", "HTTP 数据目录必须是稳定绝对路径", status=500)
        self.sources_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(self.data_dir, 0o700)
            os.chmod(self.sources_dir, 0o700)
        self._lock_handle = _acquire_exclusive_lock(self.lock_path)
        if os.name == "posix":
            for path in (self.db_path, Path(f"{self.db_path}-wal"), Path(f"{self.db_path}-shm")):
                _ensure_private_file(path)
        conn = sqlite3.connect(self.db_path, timeout=0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=0")
        version = self._schema_version(conn)
        if version == 0:
            self._create_schema(conn)
        elif version != SCHEMA_VERSION:
            # 不支持/坏 schema：fail fast，绝不删库重建（会丢掉已确认前缀和已完成结果）
            conn.close()
            self.close()
            raise HttpStoreError("unsupported_schema", f"HTTP 存储 schema 版本不受支持：{version}", status=500)
        self._conn = conn
        self._converge_restart()
        return self

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if self._lock_handle is not None:
            self._lock_handle.close()
            self._lock_handle = None

    def __enter__(self) -> "HttpStore":
        return self.open()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StoreUnavailable("HTTP 存储连接已关闭")
        return self._conn

    @staticmethod
    def _schema_version(conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        if row is None:
            return 0
        version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return 0 if version is None else int(version["value"])

    @staticmethod
    def _create_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE uploads (
                upload_id TEXT PRIMARY KEY,
                create_key TEXT NOT NULL,
                token_hash TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                options_json TEXT NOT NULL,
                source_name TEXT NOT NULL,
                state TEXT NOT NULL,
                confirmed_offset INTEGER NOT NULL,
                job_id TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE UNIQUE INDEX uploads_create_key ON uploads(create_key);
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                upload_id TEXT NOT NULL UNIQUE REFERENCES uploads(upload_id),
                state TEXT NOT NULL,
                error_code TEXT,
                time_start REAL,
                time_submit REAL,
                time_complete REAL,
                created_at REAL NOT NULL,
                started_at REAL,
                terminal_at REAL
            );
            CREATE TABLE results (
                job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
                payload TEXT NOT NULL
            );
            INSERT INTO meta(key, value) VALUES('schema_version', '1');
            COMMIT;
            """
        )

    def _converge_restart(self) -> None:
        """重启收敛：旧 QUEUED/RUNNING 标 FAILED(server_restarted)，partial 前缀与 DONE 不动。"""
        now = time.time()
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE jobs SET state=?, error_code=?, terminal_at=? WHERE state IN (?, ?)",
                (JOB_FAILED, "server_restarted", now, JOB_QUEUED, JOB_RUNNING),
            )
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    # ---------------- 内部工具 ----------------

    def _source_path(self, source_name: str) -> Path:
        # source_name 只由服务端生成（UUID），不接受任何外部路径成分
        return self.sources_dir / source_name

    def _db_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self.db_path) + suffix)
            if candidate.exists():
                total += candidate.stat().st_size
        return total

    def _free_bytes(self) -> int:
        return shutil.disk_usage(self.data_dir).free

    def _expire_if_due(self, row: sqlite3.Row) -> sqlite3.Row:
        if row["state"] == UPLOAD_UPLOADING and row["expires_at"] <= time.time():
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE uploads SET state=? WHERE upload_id=? AND state=?",
                    (UPLOAD_EXPIRED, row["upload_id"], UPLOAD_UPLOADING),
                )
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            return self.conn.execute("SELECT * FROM uploads WHERE upload_id=?", (row["upload_id"],)).fetchone()
        return row

    def _row_for_token(self, upload_id: str, token: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM uploads WHERE upload_id=?", (upload_id,)).fetchone()
        # 未知资源与错误令牌同 404：不泄露编号本身是否赋权
        if row is None or not token_matches(row["token_hash"], token):
            raise HttpStoreError("not_found", "上传不存在或凭据不匹配", status=404)
        row = self._expire_if_due(row)
        if row["state"] == UPLOAD_EXPIRED:
            raise HttpStoreError("upload_expired", "上传已过期", status=410)
        return row

    def _job_row_for_token(self, job_id: str, token: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT jobs.*, uploads.token_hash AS token_hash FROM jobs JOIN uploads ON uploads.upload_id=jobs.upload_id WHERE jobs.job_id=?",
            (job_id,),
        ).fetchone()
        if row is None or not token_matches(row["token_hash"], token):
            raise HttpStoreError("not_found", "任务不存在或凭据不匹配", status=404)
        return row

    @staticmethod
    def _upload_record(row: sqlite3.Row) -> UploadRecord:
        return UploadRecord(
            upload_id=row["upload_id"],
            state=row["state"],
            size_bytes=row["size_bytes"],
            sha256=row["sha256"],
            confirmed_offset=row["confirmed_offset"],
            expires_at=row["expires_at"],
            job_id=row["job_id"],
        )

    # ---------------- 准入（错误不删除任何旧数据） ----------------

    def _check_create_admission(self, size_bytes: int) -> None:
        conn = self.conn
        open_uploads = conn.execute(
            "SELECT COUNT(*) AS n FROM uploads WHERE state=?", (UPLOAD_UPLOADING,)
        ).fetchone()["n"]
        if open_uploads >= MAX_OPEN_UPLOADS:
            raise HttpStoreError("too_many_uploads", "未完成上传会话已达上限", status=429)
        declared = conn.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) AS total FROM uploads WHERE state != ?",
            (UPLOAD_EXPIRED,),
        ).fetchone()["total"]
        if declared + size_bytes > SOURCE_RESERVE_BYTES:
            raise HttpStoreError("source_reserve_full", "源音频声明长度总预留已满", status=507)
        if self._db_bytes() > DB_GUARD_BYTES:
            raise HttpStoreError("storage_guard_full", "HTTP 数据库与结果整体 guard 已满", status=507)
        if self._free_bytes() < FREE_SPACE_MARGIN_BYTES:
            raise HttpStoreError("disk_guard_full", "实际剩余文件系统低于安全余量", status=507)

    def _check_job_admission(self) -> None:
        active = self.conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE state IN (?, ?)", (JOB_QUEUED, JOB_RUNNING)
        ).fetchone()["n"]
        if active >= MAX_HTTP_JOBS:
            raise HttpStoreError("too_many_jobs", "HTTP 任务准入已满", status=429)

    # ---------------- 对外操作 ----------------

    def create_upload(self, *, size_bytes: int, sha256: str, options: dict, token: str, create_key: str) -> UploadRecord:
        """身份/不可变参数/key/capacity → 独占空文件 fsync → 数据库 0 offset 提交。"""
        normalized = normalize_options(options)
        options_json = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        token_hash = hash_token(token)
        existing = self.conn.execute("SELECT * FROM uploads WHERE create_key=?", (create_key,)).fetchone()
        if existing is not None:
            # 同 key 同身份幂等返回 200；同 key 不同内容 409，绝不覆写已有文件
            same = (
                existing["token_hash"] == token_hash
                and existing["size_bytes"] == size_bytes
                and existing["sha256"] == sha256
                and existing["options_json"] == options_json
            )
            if not same:
                raise HttpStoreError("idempotency_conflict", "同一 Idempotency-Key 已用于不同的上传身份", status=409)
            return self._upload_record(self._expire_if_due(existing))

        self._check_create_admission(size_bytes)
        upload_id = str(uuid.uuid4())
        source_name = f"{upload_id}.bin"
        path = self._source_path(source_name)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        try:
            if os.name == "posix":
                os.fchmod(fd, 0o600)
            os.fsync(fd)
        finally:
            os.close(fd)
        now = time.time()
        expires_at = now + UPLOAD_TTL_SECONDS
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO uploads(upload_id, create_key, token_hash, size_bytes, sha256, options_json,"
                " source_name, state, confirmed_offset, job_id, created_at, updated_at, expires_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (upload_id, create_key, token_hash, size_bytes, sha256, options_json, source_name,
                 UPLOAD_UPLOADING, 0, None, now, now, expires_at),
            )
        except sqlite3.IntegrityError:
            # 并发同 key：唯一索引挡住了第二条插入，回到幂等路径而不是报错
            conn.execute("ROLLBACK")
            os.unlink(path)
            existing = conn.execute("SELECT * FROM uploads WHERE create_key=?", (create_key,)).fetchone()
            if existing is None or existing["token_hash"] != token_hash:
                raise HttpStoreError("idempotency_conflict", "同一 Idempotency-Key 已用于不同的上传身份", status=409)
            return self._upload_record(self._expire_if_due(existing))
        except BaseException:
            conn.execute("ROLLBACK")
            os.unlink(path)
            raise
        conn.execute("COMMIT")
        return UploadRecord(upload_id, UPLOAD_UPLOADING, size_bytes, sha256, 0, expires_at, None)

    def get_upload(self, upload_id: str, token: str) -> UploadRecord:
        return self._upload_record(self._row_for_token(upload_id, token))

    def append_bytes(self, upload_id: str, token: str, offset: int, data) -> int:
        """字节 flush/fsync → 条件 offset 事务 → 返回新 offset（route 据此发 204 ACK）。"""
        payload = bytes(data)
        if len(payload) > MAX_CHUNK_BYTES:
            raise HttpStoreError("payload_too_large", "单次 PATCH 超过 1 MiB", status=413)
        row = self._row_for_token(upload_id, token)
        if row["state"] != UPLOAD_UPLOADING:
            raise HttpStoreError("not_uploadable", "上传已受理，不能继续写入", status=409,
                                 confirmed_offset=row["confirmed_offset"])
        confirmed = row["confirmed_offset"]
        if offset != confirmed:
            raise HttpStoreError("offset_conflict", "上传位置与服务端确认位置不一致", status=409,
                                 confirmed_offset=confirmed)
        end = confirmed + len(payload)
        if end > row["size_bytes"]:
            raise HttpStoreError("payload_too_large", "写入超出声明文件长度", status=413,
                                 confirmed_offset=confirmed)
        if len(payload) == 0:
            return confirmed
        path = self._source_path(row["source_name"])
        fd = os.open(path, os.O_RDWR)
        try:
            # 未确认尾显式恢复：先按数据库 offset 截断，再写新块，绝不以文件长度冒充确认
            os.ftruncate(fd, confirmed)
            os.lseek(fd, confirmed, os.SEEK_SET)
            written = 0
            while written < len(payload):
                count = os.write(fd, payload[written:])
                if count == 0:
                    raise OSError("HTTP 文件写入未取得进展")
                written += count
            os.fsync(fd)
        finally:
            os.close(fd)
        now = time.time()
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                "UPDATE uploads SET confirmed_offset=?, updated_at=?, expires_at=? WHERE upload_id=? AND confirmed_offset=?",
                (end, now, now + UPLOAD_TTL_SECONDS, upload_id, confirmed),
            )
            if cursor.rowcount != 1:
                raise HttpStoreError("offset_conflict", "上传位置竞争失败", status=409)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        return end

    def _verify_source(self, row: sqlite3.Row) -> None:
        """完整长度与 SHA-256 核对；任何不符都 422，保留已确认前缀供显式继续。"""
        path = self._source_path(row["source_name"])
        if not path.exists():
            raise HttpStoreError("integrity_mismatch", "源文件缺失，无法核验完整性", status=422)
        size = path.stat().st_size
        if size != row["size_bytes"]:
            raise HttpStoreError("integrity_mismatch", "源文件长度与声明不一致", status=422)
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            while chunk := stream.read(READ_CHUNK_BYTES):
                digest.update(chunk)
        if digest.hexdigest() != row["sha256"]:
            raise HttpStoreError("integrity_mismatch", "源文件 SHA-256 与声明不一致", status=422)

    def commit_upload(self, upload_id: str, token: str) -> JobRecord:
        """核对完整 hash/length 后，在唯一事务里建立唯一 Job 并置 COMMITTED。"""
        row = self._row_for_token(upload_id, token)
        if row["state"] == UPLOAD_COMMITTED and row["job_id"]:
            # 已经存在的 Job 重复 commit 必须仍返回同一 Job，不因推理协调者不可用另造
            return self.job_record(row["job_id"], token)
        if row["confirmed_offset"] != row["size_bytes"]:
            raise HttpStoreError("upload_incomplete", "上传尚未达到声明长度", status=409,
                                 confirmed_offset=row["confirmed_offset"])
        self._verify_source(row)
        self._check_job_admission()
        if not self._inference_ready():
            # 没有真实推理协调者就不受理，绝不受理后永远排队
            raise HttpStoreError("inference_unavailable", "识别协调者当前不可用，未受理该上传", status=503)
        job_id = str(uuid.uuid4())
        now = time.time()
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                "UPDATE uploads SET state=?, job_id=?, updated_at=? WHERE upload_id=? AND state=?",
                (UPLOAD_COMMITTED, job_id, now, upload_id, UPLOAD_UPLOADING),
            )
            if cursor.rowcount != 1:
                raise HttpStoreError("commit_conflict", "上传状态已变化", status=409)
            conn.execute(
                "INSERT INTO jobs(job_id, upload_id, state, error_code, time_start, time_submit,"
                " time_complete, created_at, started_at, terminal_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (job_id, upload_id, JOB_QUEUED, None, now, now, None, now, None, None),
            )
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        return self.job_record(job_id, token)

    def job_record(self, job_id: str, token: str) -> JobRecord:
        row = self._job_row_for_token(job_id, token)
        result_available = self.conn.execute(
            "SELECT 1 FROM results WHERE job_id=?", (job_id,)
        ).fetchone() is not None
        source_available = self.conn.execute(
            "SELECT 1 FROM uploads WHERE upload_id=? AND state != ?",
            (row["upload_id"], UPLOAD_EXPIRED),
        ).fetchone() is not None
        return JobRecord(
            job_id=row["job_id"],
            state=row["state"],
            error_code=row["error_code"],
            time_start=row["time_start"],
            time_submit=row["time_submit"],
            time_complete=row["time_complete"],
            result_available=bool(result_available and row["state"] == JOB_DONE),
            source_available=bool(source_available),
        )

    def get_result(self, job_id: str, token: str) -> dict:
        row = self._job_row_for_token(job_id, token)
        payload = self.conn.execute("SELECT payload FROM results WHERE job_id=?", (job_id,)).fetchone()
        if row["state"] == JOB_FAILED:
            raise HttpStoreError("job_failed", "任务已失败", status=409)
        if row["state"] != JOB_DONE or payload is None:
            raise HttpStoreError("result_not_ready", "任务结果尚未就绪", status=409)
        # 只接受真实持久化的完整结果，不接受任何内存态或推断结果
        return json.loads(payload["payload"])

    def record_result(self, job_id: str, result: dict) -> None:
        """完整 result 与 DONE 必须同一次提交（供 E3 runner 使用，本增量不主动调用）。"""
        if not isinstance(result, dict):
            raise HttpStoreError("invalid_result", "识别结果必须是对象", status=422)
        numeric_fields = ("duration", "time_start", "time_submit", "time_complete")
        numeric_values = [result.get(field) for field in numeric_fields]
        tokens = result.get("tokens")
        timestamps = result.get("timestamps")
        valid_numbers = all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            and (not isinstance(value, float) or math.isfinite(value))
            for value in numeric_values
        )
        valid_timestamps = (
            isinstance(timestamps, list)
            and all(isinstance(value, (int, float)) and not isinstance(value, bool)
                    and (not isinstance(value, float) or math.isfinite(value)) for value in timestamps)
        )
        if (
            result.get("task_id") != job_id
            or result.get("type") != "file"
            or result.get("socket_id") != ""
            or result.get("owner_kind") != "http"
            or result.get("is_final") is not True
            or not isinstance(result.get("text"), str)
            or not isinstance(result.get("text_accu"), str)
            or not isinstance(tokens, list)
            or not all(isinstance(token, str) for token in tokens)
            or not valid_timestamps
            or len(tokens) != len(timestamps)
            or not valid_numbers
        ):
            raise HttpStoreError("invalid_result", "识别结果字段与完整文件任务不匹配", status=422)
        payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        if len(payload.encode("utf-8")) > MAX_RESULT_BYTES:
            raise HttpStoreError("result_too_large", "识别结果超过 64 MiB 上限", status=507)
        now = time.time()
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            job = conn.execute("SELECT state FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if job is None:
                raise HttpStoreError("unknown_job", "没有对应的 HTTP 文件任务", status=404)
            if job["state"] not in (JOB_QUEUED, JOB_RUNNING):
                raise HttpStoreError("result_terminal", "终态任务不能发布或覆盖结果", status=409)
            cursor = conn.execute(
                "UPDATE jobs SET state=?, time_complete=?, terminal_at=?, started_at=COALESCE(started_at, ?)"
                " WHERE job_id=? AND state IN (?,?)",
                (JOB_DONE, now, now, now, job_id, JOB_QUEUED, JOB_RUNNING),
            )
            if cursor.rowcount != 1:
                raise HttpStoreError("result_terminal", "终态任务不能发布或覆盖结果", status=409)
            conn.execute("INSERT INTO results(job_id, payload) VALUES(?,?)", (job_id, payload))
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def stats(self) -> dict:
        """测试与诊断用的真实库计数（不作为对外 API）。"""
        row = self.conn.execute(
            "SELECT (SELECT COUNT(*) FROM uploads) AS uploads,"
            " (SELECT COUNT(*) FROM jobs) AS jobs,"
            " (SELECT COALESCE(SUM(confirmed_offset),0) FROM uploads) AS confirmed"
        ).fetchone()
        return {"uploads": row["uploads"], "jobs": row["jobs"], "confirmed": row["confirmed"]}


def normalize_options(options: dict) -> dict:
    """任务参数用旧校验语义；一旦受理不可改，所以这里做严格归一化。"""
    if not isinstance(options, dict):
        raise HttpStoreError("invalid_options", "options 必须是对象")
    unknown = set(options) - set(OPTION_FIELDS)
    if unknown:
        raise HttpStoreError("invalid_options", f"options 含未知字段：{sorted(unknown)}")
    normalized = {}
    for field in ("model", "language", "context"):
        value = options.get(field)
        if value is None:
            normalized[field] = None
        elif isinstance(value, str):
            normalized[field] = value
        else:
            raise HttpStoreError("invalid_options", f"{field} 必须是字符串")
    for field, default in (("seg_duration", 15.0), ("seg_overlap", 2.0)):
        value = options.get(field, default)
        if value is None:
            value = default
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HttpStoreError("invalid_options", f"{field} 必须是有限数字")
        if value != value or value in (float("inf"), float("-inf")) or value < 0:
            raise HttpStoreError("invalid_options", f"{field} 必须是非负有限数字")
        normalized[field] = float(value)
    return normalized


def validate_identity(size_bytes, sha256: str) -> None:
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes <= 0:
        raise HttpStoreError("invalid_request", "size_bytes 必须是正整数")
    if size_bytes > MAX_FILE_BYTES:
        raise HttpStoreError("file_too_large", "文件超过 1 GiB 上限", status=413)
    if not isinstance(sha256, str) or len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise HttpStoreError("invalid_request", "sha256 必须是 64 位小写十六进制")


__all__ = [
    "HttpStore",
    "HttpStoreError",
    "StoreUnavailable",
    "DataDirLocked",
    "UploadRecord",
    "JobRecord",
    "normalize_options",
    "validate_identity",
    "hash_token",
    "token_matches",
]
