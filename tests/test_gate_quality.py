"""质量入口 producer 契约：真实子进程 + 替身 uv 写文件。"""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRY = REPO_ROOT / "scripts" / "gate-quality"
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
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"p = {str(record)!r}\n"
        "open(p, 'w', encoding='utf-8').write(\n"
        "    json.dumps({'argv': sys.argv, 'env': dict(os.environ)})\n"
        ")\n"
        f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_entry(tmp_path: Path, uv_exit: int) -> tuple[subprocess.CompletedProcess[str], dict]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    record = tmp_path / "uv-producer.json"
    _write_fake_uv(bindir, record, uv_exit)
    env = os.environ.copy()
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    completed = subprocess.run(
        ["bash", str(ENTRY)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    payload = json.loads(record.read_text(encoding="utf-8"))
    return completed, payload


def test_gate_quality_uv_argv_matches_ci_full_suite(tmp_path):
    completed, payload = _run_entry(tmp_path, uv_exit=0)
    assert completed.returncode == 0, completed.stderr
    argv = payload["argv"]
    assert argv[0].endswith("/uv"), argv
    assert argv[1:] == EXPECTED_ARGV
    assert payload["env"]["PATH"].split(os.pathsep)[0].endswith("/bin")


def test_gate_quality_propagates_uv_nonzero_exit(tmp_path):
    completed, payload = _run_entry(tmp_path, uv_exit=7)
    assert completed.returncode == 7, completed.stderr
    assert payload["argv"][1:] == EXPECTED_ARGV
