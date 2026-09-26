"""升级脚本语法与 /health 发布边界测试。"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = REPO_ROOT / "deploy"


def run_git(*args, cwd=None):
    return subprocess.run(
        ["git", *map(str, args)], cwd=cwd, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


@pytest.fixture
def deployment_clone(tmp_path):
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    clone = tmp_path / "clone"
    seed.mkdir()
    run_git("init", "--bare", "--initial-branch=main", origin)
    run_git("init", "--initial-branch=main", cwd=seed)
    run_git("-C", seed, "config", "user.name", "Deploy Script Test")
    run_git("-C", seed, "config", "user.email", "deploy-test@example.invalid")
    (seed / "requirements-server-linux.txt").write_text("# fake requirements\n", encoding="utf-8")
    (seed / "requirements-server-macos.txt").write_text("# fake requirements\n", encoding="utf-8")
    (seed / "requirements-server.txt").write_text("# fake requirements\n", encoding="utf-8")
    (seed / "deploy").mkdir()
    shutil.copy2(DEPLOY_DIR / "update.sh", seed / "deploy" / "update.sh")
    (seed / "core" / "server" / "engines").mkdir(parents=True)
    (seed / "tracked.txt").write_text("fixture\n", encoding="utf-8")
    run_git("-C", seed, "add", "requirements-server-linux.txt", "requirements-server-macos.txt",
            "requirements-server.txt", "deploy/update.sh", "tracked.txt")
    run_git("-C", seed, "commit", "-m", "old deployment fixture")
    run_git("-C", seed, "tag", "old")
    (seed / "core" / "server" / "engines" / "llama_build_info.py").write_text(
        'LLAMA_BUILD = "b10621"\n', encoding="utf-8"
    )
    run_git("-C", seed, "add", "core/server/engines/llama_build_info.py")
    run_git("-C", seed, "commit", "-m", "deployment fixture with llama build")
    run_git("-C", seed, "remote", "add", "origin", origin)
    run_git("-C", seed, "push", "-u", "origin", "main", "--tags")
    run_git("clone", origin, clone)
    return clone


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        body = json.dumps(self.server.health_payload).encode("utf-8")
        self.send_response(self.server.health_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


class HealthService:
    def __init__(self, status=200, payload=None):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
        self.server.health_status = status
        self.server.health_payload = payload or {}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self):
        return self.server.server_port

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def prepare_fake_tools(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    real_python = shutil.which("python3")
    assert real_python
    python3 = bin_dir / "python3"
    python3.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\" >> \"$DEPLOY_TEST_PYTHON3_ARGS\"\n"
        f"exec {real_python} \"$@\"\n",
        encoding="utf-8",
    )
    deploy_python = bin_dir / "deploy-python"
    deploy_python.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$DEPLOY_TEST_PYTHON_ARGS\"\n"
        "printf '%s\\n' \"${CW_MODEL_TYPE-unset}\" > \"$DEPLOY_TEST_MODEL_TYPE\"\n"
        "exit \"${DEPLOY_TEST_PIP_EXIT:-0}\"\n",
        encoding="utf-8",
    )
    pm2 = bin_dir / "pm2"
    pm2.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$DEPLOY_TEST_PM2_ARGS\"\n"
        "printf '%s\\n' \"${CW_MODEL_TYPE-unset}\" > \"$DEPLOY_TEST_PM2_MODEL_TYPE\"\n",
        encoding="utf-8",
    )
    uname = bin_dir / "uname"
    uname.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"${DEPLOY_TEST_UNAME:-Linux}\"\n", encoding="utf-8"
    )
    python3.chmod(0o755)
    deploy_python.chmod(0o755)
    pm2.chmod(0o755)
    uname.chmod(0o755)
    return bin_dir


def update_environment(tmp_path, bin_dir, port):
    env = os.environ.copy()
    env.update({
        "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
        "DEPLOY_PYTHON": str(bin_dir / "deploy-python"),
        "CW_MODEL_TYPE": "paraformer",
        "DEPLOY_PROCESS_NAME": "fake-server",
        "DEPLOY_PORT": str(port),
        "DEPLOY_HEALTH_TIMEOUT": "3",
        "DEPLOY_HEALTH_INTERVAL": "1",
        "DEPLOY_TEST_PYTHON_ARGS": str(tmp_path / "python-argv.log"),
        "DEPLOY_TEST_PYTHON3_ARGS": str(tmp_path / "python3-argv.log"),
        "DEPLOY_TEST_MODEL_TYPE": str(tmp_path / "python-model.log"),
        "DEPLOY_TEST_PM2_ARGS": str(tmp_path / "pm2-argv.log"),
        "DEPLOY_TEST_PM2_MODEL_TYPE": str(tmp_path / "pm2-model.log"),
    })
    return env


def install_fake_curl(tmp_path, bin_dir, env):
    real_python = shutil.which("python3")
    assert real_python
    curl = bin_dir / "curl"
    curl.write_text(
        f'''#!{real_python}
import json
import os
import pathlib
import sys

args = sys.argv[1:]
calls_path = pathlib.Path(os.environ["DEPLOY_TEST_CURL_CALLS"])
calls = calls_path.read_text(encoding="utf-8").splitlines() if calls_path.exists() else []
with calls_path.open("a", encoding="utf-8") as calls_file:
    calls_file.write(json.dumps(args) + "\\n")
responses = json.loads(os.environ["DEPLOY_TEST_CURL_RESPONSES"])
status, payload = responses[min(len(calls), len(responses) - 1)]
if status is None:
    sys.exit(7)
output_path = pathlib.Path(args[args.index("--output") + 1])
output_path.write_text(json.dumps(payload), encoding="utf-8")
sys.stdout.write(str(status))
''',
        encoding="utf-8",
    )
    curl.chmod(0o755)
    env["DEPLOY_TEST_CURL_CALLS"] = str(tmp_path / "curl-args.jsonl")
    env["DEPLOY_TEST_CURL_RESPONSES"] = "[]"
    return Path(env["DEPLOY_TEST_CURL_CALLS"])


def run_update(clone, env, ref="main"):
    return subprocess.run(
        ["bash", str(clone / "deploy" / "update.sh"), ref],
        cwd=clone,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=15,
    )


def test_bash_syntax_and_powershell_contract():
    shell_scripts = sorted(DEPLOY_DIR.glob("*.sh"))
    assert shell_scripts
    for script in shell_scripts:
        subprocess.run(["bash", "-n", str(script)], check=True, capture_output=True, text=True)

    powershell_script = DEPLOY_DIR / "update.ps1"
    assert powershell_script.is_file()
    content = powershell_script.read_text(encoding="utf-8")
    for command in (
        "fetch --tags origin",
        "checkout --detach",
        "Stop-ScheduledTask",
        "Start-ScheduledTask",
        "requirements-server.txt",
        "git_sha",
        "worker_alive",
        "Invoke-WebRequest",
    ):
        assert command in content
    assert re.search(
        r"\[Parameter\(Mandatory = \$true\)\]\s*\[string\]\$Python", content
    )
    assert "-m venv" not in content
    assert ".venv" not in content
    for llama_contract in (
        "llama_build_info.py", "LLAMA_BUILD", "ggml.dll", "ggml-base.dll",
        "llama.dll", "llama-$llamaBuild-bin-win-vulkan-x64.zip", "cat-file -e",
        "rev-parse --verify", "git -C $repo show",
    ):
        assert llama_contract in content
    assert content.index("llama 预检失败") < content.index("checkout --detach")
    poll_loop = content.split("while ($healthTimer.Elapsed.TotalSeconds -lt $HealthTimeout) {", 1)[1]
    poll_loop = poll_loop.split("$actualGitSha = if", 1)[0]
    assert poll_loop.count("break") == 1
    assert "if ($lastStatus -eq 200 -and $lastPayload.git_sha -eq $expectedGitSha) { break }" in poll_loop
    pwsh = shutil.which("pwsh")
    if pwsh:
        subprocess.run(
            [pwsh, "-NoProfile", "-Command",
             f"$null = [scriptblock]::Create((Get-Content -Raw '{powershell_script}'))"],
            check=True, capture_output=True, text=True,
        )


def test_update_script_polls_health_until_sha_matches_or_times_out(deployment_clone, tmp_path):
    bin_dir = prepare_fake_tools(tmp_path)
    git_sha = run_git("-C", deployment_clone, "rev-parse", "--short", "main").stdout.strip()
    env = update_environment(tmp_path, bin_dir, 6016)
    env["DEPLOY_HEALTH_TIMEOUT"] = "3"
    calls_path = install_fake_curl(tmp_path, bin_dir, env)
    stale = {"status": "ok", "git_sha": "old-sha", "model": "paraformer", "worker_alive": True}
    matching = {**stale, "git_sha": git_sha}
    cases = (
        ([[200, stale], [200, stale], [200, matching]], "success"),
        ([[200, stale]], "mismatch"), ([[None, {}]], "timeout"),
    )
    for responses, outcome in cases:
        calls_path.write_text("", encoding="utf-8")
        env["DEPLOY_TEST_CURL_RESPONSES"] = json.dumps(responses)
        started_at = time.monotonic()
        result = run_update(deployment_clone, env)
        elapsed = time.monotonic() - started_at
        calls = len(calls_path.read_text(encoding="utf-8").splitlines())
        if outcome == "success":
            assert result.returncode == 0, result.stderr
            assert f"git_sha={git_sha}" in result.stdout
            curl_args = [json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()]
            assert calls == 3 and all(args[-1] == "http://127.0.0.1:6016/health" for args in curl_args)
            assert all(args[args.index("--write-out") + 1] == "%{http_code}" for args in curl_args)
        else:
            assert result.returncode != 0 and calls >= 2
            if outcome == "mismatch":
                assert "健康检查 git_sha 不一致" in result.stderr
                assert f"期望={git_sha}" in result.stderr and "实际=old-sha" in result.stderr
                assert elapsed <= 8
            else:
                assert "健康检查超时" in result.stderr
                assert '"git_sha":null' in result.stderr
                assert "git_sha 不一致" not in result.stderr


def test_update_script_rejects_sha_mismatch_and_accepts_matching_sha(deployment_clone, tmp_path):
    bin_dir = prepare_fake_tools(tmp_path)
    git_sha = run_git("-C", deployment_clone, "rev-parse", "--short", "HEAD").stdout.strip()
    extra = {"private_detail": "must-not-be-printed"}
    with HealthService(200, {
        "status": "ok", "git_sha": "wrong-sha", "model": "paraformer",
        "worker_alive": True, **extra,
    }) as health:
        env = update_environment(tmp_path, bin_dir, health.port)
        mismatch = run_update(deployment_clone, env)
        assert mismatch.returncode != 0
        assert f"期望={git_sha}" in mismatch.stderr
        assert "实际=wrong-sha" in mismatch.stderr
        assert '"git_sha": "wrong-sha"' in mismatch.stderr
        assert "must-not-be-printed" not in mismatch.stderr

        health.server.health_payload["git_sha"] = git_sha
        matched = run_update(deployment_clone, env)
        assert matched.returncode == 0, matched.stderr
        assert f"git_sha={git_sha}" in matched.stdout

    assert not (deployment_clone / ".venv").exists()
    assert (tmp_path / "python-argv.log").read_text(encoding="utf-8").splitlines() == [
        "-m", "pip", "install", "-r", "requirements-server-linux.txt",
    ]
    python3_calls = (tmp_path / "python3-argv.log").read_text(encoding="utf-8").splitlines()
    assert not any(python3_calls[index:index + 2] == ["-m", "venv"]
                   for index in range(len(python3_calls) - 1))
    assert (tmp_path / "python-model.log").read_text(encoding="utf-8").strip() == "paraformer"
    assert (tmp_path / "pm2-argv.log").read_text(encoding="utf-8").splitlines() == [
        "restart", "fake-server",
    ]
    assert (tmp_path / "pm2-model.log").read_text(encoding="utf-8").strip() == "paraformer"


def test_update_script_times_out_when_health_stays_503(deployment_clone, tmp_path):
    bin_dir = prepare_fake_tools(tmp_path)
    with HealthService(503, {
        "status": "unavailable", "git_sha": "old-sha", "model": "paraformer",
        "worker_alive": False, "private_detail": "must-not-be-printed",
    }) as health:
        result = run_update(deployment_clone, update_environment(tmp_path, bin_dir, health.port))
    assert result.returncode != 0
    assert "健康检查超时" in result.stderr
    assert '"status": "unavailable"' in result.stderr
    assert '"git_sha": "old-sha"' in result.stderr
    assert '"model": "paraformer"' in result.stderr
    assert '"worker_alive": false' in result.stderr
    assert "must-not-be-printed" not in result.stderr


def test_update_script_does_not_restart_after_dependency_install_failure(deployment_clone, tmp_path):
    bin_dir = prepare_fake_tools(tmp_path)
    with HealthService(200, {"status": "ok", "git_sha": "unused"}) as health:
        env = update_environment(tmp_path, bin_dir, health.port)
        env["DEPLOY_TEST_PIP_EXIT"] = "7"
        result = run_update(deployment_clone, env)
    assert result.returncode != 0
    assert not (tmp_path / "pm2-argv.log").exists()


def test_update_script_requires_deploy_python_and_does_not_create_venv(deployment_clone, tmp_path):
    bin_dir = prepare_fake_tools(tmp_path)
    with HealthService(200, {"status": "ok"}) as health:
        env = update_environment(tmp_path, bin_dir, health.port)
        env.pop("DEPLOY_PYTHON")
        result = run_update(deployment_clone, env)
    assert result.returncode != 0
    assert "DEPLOY_PYTHON" in result.stderr
    assert not (deployment_clone / ".venv").exists()
    python3_calls = (tmp_path / "python3-argv.log").read_text(encoding="utf-8") if (
        tmp_path / "python3-argv.log"
    ).exists() else ""
    assert "-m\nvenv" not in python3_calls
    assert not (tmp_path / "python-argv.log").exists()

    env = update_environment(tmp_path, bin_dir, 6016)
    env["DEPLOY_PYTHON"] = str(tmp_path / "missing-python")
    missing_interpreter = run_update(deployment_clone, env)
    assert missing_interpreter.returncode != 0
    assert "DEPLOY_PYTHON 不存在或不可执行" in missing_interpreter.stderr
    assert not (tmp_path / "pm2-argv.log").exists()


def llama_lib_dir(clone):
    path = clone / "core" / "server" / "engines" / "llama" / "bin" / "b10621"
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_llama_preflight_stops_restart_when_library_is_missing(deployment_clone, tmp_path):
    run_git("-C", deployment_clone, "checkout", "--detach", "old")
    original_head = run_git("-C", deployment_clone, "rev-parse", "HEAD").stdout.strip()
    bin_dir = prepare_fake_tools(tmp_path)
    lib_dir = llama_lib_dir(deployment_clone)
    for name in ("libggml.dylib", "libggml-base.dylib"):
        (lib_dir / name).touch()
    env = update_environment(tmp_path, bin_dir, 6017)
    env["CW_MODEL_TYPE"] = "qwen_asr_mlx"
    env["DEPLOY_TEST_UNAME"] = "Darwin"

    result = run_update(deployment_clone, env, ref="main")

    assert result.returncode != 0
    assert run_git("-C", deployment_clone, "rev-parse", "HEAD").stdout.strip() == original_head
    assert "libllama.dylib" in result.stderr
    assert "llama-b10621-bin-macos-arm64.tar.gz" in result.stderr
    assert not (tmp_path / "pm2-argv.log").exists()


def test_llama_preflight_restarts_when_all_mac_libraries_exist(deployment_clone, tmp_path):
    bin_dir = prepare_fake_tools(tmp_path)
    lib_dir = llama_lib_dir(deployment_clone)
    for name in ("libggml.dylib", "libggml-base.dylib", "libllama.dylib"):
        (lib_dir / name).touch()
    git_sha = run_git("-C", deployment_clone, "rev-parse", "--short", "main").stdout.strip()
    with HealthService(200, {"status": "ok", "git_sha": git_sha}) as health:
        env = update_environment(tmp_path, bin_dir, health.port)
        env["CW_MODEL_TYPE"] = "qwen_asr_mlx"
        env["DEPLOY_TEST_UNAME"] = "Darwin"
        result = run_update(
            deployment_clone, env,
        )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "pm2-argv.log").read_text(encoding="utf-8").splitlines() == [
        "restart", "fake-server",
    ]


def test_paraformer_skips_llama_preflight(deployment_clone, tmp_path):
    bin_dir = prepare_fake_tools(tmp_path)
    git_sha = run_git("-C", deployment_clone, "rev-parse", "--short", "main").stdout.strip()
    with HealthService(200, {"status": "ok", "git_sha": git_sha}) as health:
        result = run_update(deployment_clone, update_environment(tmp_path, bin_dir, health.port))
    assert result.returncode == 0, result.stderr
    assert not (deployment_clone / "core/server/engines/llama/bin/b10621").exists()
    assert (tmp_path / "pm2-argv.log").is_file()


def test_old_ref_without_build_info_skips_llama_preflight(deployment_clone, tmp_path):
    bin_dir = prepare_fake_tools(tmp_path)
    git_sha = run_git("-C", deployment_clone, "rev-parse", "--short", "old").stdout.strip()
    with HealthService(200, {"status": "ok", "git_sha": git_sha}) as health:
        env = update_environment(tmp_path, bin_dir, health.port)
        env["CW_MODEL_TYPE"] = "qwen_asr_mlx"
        env["DEPLOY_TEST_UNAME"] = "Darwin"
        result = run_update(deployment_clone, env, ref="old")
    assert result.returncode == 0, result.stderr
    assert "llama 预检跳过：old 无 llama_build_info.py" in result.stdout
    assert (tmp_path / "pm2-argv.log").is_file()
