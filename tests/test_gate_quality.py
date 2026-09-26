"""质量入口 producer 契约：真实子进程 + 替身 uv 写文件。"""
import json
import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRY = REPO_ROOT / "scripts" / "gate-quality"
BASH = shutil.which("bash")
SENTINEL_KEY = "GATE_QUALITY_UNPERMITTED_PROBE"
SENTINEL_VAL = "unpermitted-probe-value"
PKGS = [
    "numpy", "rich", "websockets==15.0.1", "colorama",
    "pytest", "soundfile", "pytest-asyncio",
]
EXPECTED_ARGV = ["run", "--no-project", "--python", "3.12"]
for _pkg in PKGS:
    EXPECTED_ARGV += ["--with", _pkg]
EXPECTED_ARGV += ["python", "-m", "pytest", "tests/", "-q"]


def _write_fake_uv(bindir: Path, record: Path, exit_code: int) -> None:
    script = bindir / "uv"
    script.write_text(
        "#!/usr/bin/python3\n"
        "import json, os, sys\n"
        f"if {SENTINEL_KEY!r} in os.environ:\n"
        "    raise SystemExit(99)\n"
        f"open({str(record)!r}, 'w', encoding='utf-8').write(\n"
        "    json.dumps({'argv': sys.argv, 'PATH': os.environ.get('PATH', ''), 'cwd': os.getcwd()}))\n"
        f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    script.chmod(0o755)


def _run_entry(tmp_path: Path, *, uv_exit=0, with_ffmpeg=True, with_uv=True):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    record = tmp_path / "uv-producer.json"
    if with_ffmpeg:
        ff = bindir / "ffmpeg"
        ff.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        ff.chmod(0o755)
    os.symlink(shutil.which("dirname"), bindir / "dirname")
    if with_uv:
        _write_fake_uv(bindir, record, uv_exit)
    completed = subprocess.run(
        [BASH, str(ENTRY)],
        cwd=REPO_ROOT,
        env={"PATH": str(bindir), "HOME": str(home), "TMPDIR": str(tmp_path)},
        text=True,
        capture_output=True,
        check=False,
    )
    return completed, record


def _assert_clean(completed, record: Path) -> None:
    blob = completed.stdout + completed.stderr
    if record.exists():
        blob += record.read_text(encoding="utf-8")
    assert SENTINEL_KEY not in blob
    assert SENTINEL_VAL not in blob


def test_gate_quality_uv_argv_matches_ci_full_suite(tmp_path, monkeypatch):
    monkeypatch.setenv(SENTINEL_KEY, SENTINEL_VAL)
    completed, record = _run_entry(tmp_path, uv_exit=0)
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(record.read_text(encoding="utf-8"))
    assert set(payload) == {"argv", "PATH", "cwd"}
    assert payload["argv"][0].endswith("/uv")
    assert payload["argv"][1:] == EXPECTED_ARGV
    assert payload["cwd"] == str(REPO_ROOT)
    assert payload["PATH"].split(os.pathsep)[0].endswith("/bin")
    assert "dirname: command not found" not in completed.stderr
    _assert_clean(completed, record)


def test_gate_quality_propagates_uv_nonzero_exit(tmp_path, monkeypatch):
    monkeypatch.setenv(SENTINEL_KEY, SENTINEL_VAL)
    completed, record = _run_entry(tmp_path, uv_exit=7)
    assert completed.returncode == 7, completed.stderr
    payload = json.loads(record.read_text(encoding="utf-8"))
    assert payload["argv"][1:] == EXPECTED_ARGV
    assert payload["cwd"] == str(REPO_ROOT)
    _assert_clean(completed, record)


def test_gate_quality_missing_ffmpeg_fails(tmp_path, monkeypatch):
    monkeypatch.setenv(SENTINEL_KEY, SENTINEL_VAL)
    completed, record = _run_entry(tmp_path, with_ffmpeg=False)
    assert completed.returncode != 0
    assert "gate-quality: ffmpeg 未安装" in completed.stderr
    assert not record.exists()
    _assert_clean(completed, record)


def test_gate_quality_missing_uv_fails(tmp_path, monkeypatch):
    monkeypatch.setenv(SENTINEL_KEY, SENTINEL_VAL)
    completed, record = _run_entry(tmp_path, with_ffmpeg=True, with_uv=False)
    assert completed.returncode != 0
    assert "gate-quality: uv 未安装" in completed.stderr
    assert not record.exists()
    _assert_clean(completed, record)
