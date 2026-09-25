"""升级脚本语法与 /health 发布边界测试。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
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
    (seed / "deploy").mkdir()
    shutil.copy2(DEPLOY_DIR / "update.sh", seed / "deploy" / "update.sh")
    (seed / "tracked.txt").write_text("fixture\n", encoding="utf-8")
    run_git("-C", seed, "add", "requirements-server-linux.txt", "deploy/update.sh", "tracked.txt")
    run_git("-C", seed, "commit", "-m", "deployment fixture")
    run_git("-C", seed, "remote", "add", "origin", origin)
    run_git("-C", seed, "push", "-u", "origin", "main")
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
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"venv\" ]; then\n"
        "  target=\"$3\"\n"
        "  mkdir -p \"$target/bin\"\n"
        "  cat > \"$target/bin/python\" <<'PYTHON_WRAPPER'\n"
        "#!/bin/sh\n"
        "printf '%s\\n' \"$@\" > \"$DEPLOY_TEST_PYTHON_ARGS\"\n"
        "printf '%s\\n' \"${CW_MODEL_TYPE-unset}\" > \"$DEPLOY_TEST_MODEL_TYPE\"\n"
        "exit \"${DEPLOY_TEST_PIP_EXIT:-0}\"\n"
        "PYTHON_WRAPPER\n"
        "  chmod +x \"$target/bin/python\"\n"
        "  exit 0\n"
        "fi\n"
        f"exec {real_python} \"$@\"\n",
        encoding="utf-8",
    )
    pm2 = bin_dir / "pm2"
    pm2.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$DEPLOY_TEST_PM2_ARGS\"\n"
        "printf '%s\\n' \"${CW_MODEL_TYPE-unset}\" > \"$DEPLOY_TEST_PM2_MODEL_TYPE\"\n",
        encoding="utf-8",
    )
    python3.chmod(0o755)
    pm2.chmod(0o755)
    return bin_dir


def update_environment(tmp_path, bin_dir, port):
    env = os.environ.copy()
    env.update({
        "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
        "CW_MODEL_TYPE": "paraformer",
        "DEPLOY_PROCESS_NAME": "fake-server",
        "DEPLOY_PORT": str(port),
        "DEPLOY_HEALTH_TIMEOUT": "3",
        "DEPLOY_HEALTH_INTERVAL": "1",
        "DEPLOY_TEST_PYTHON_ARGS": str(tmp_path / "python-argv.log"),
        "DEPLOY_TEST_MODEL_TYPE": str(tmp_path / "python-model.log"),
        "DEPLOY_TEST_PM2_ARGS": str(tmp_path / "pm2-argv.log"),
        "DEPLOY_TEST_PM2_MODEL_TYPE": str(tmp_path / "pm2-model.log"),
    })
    return env


def run_update(clone, env):
    return subprocess.run(
        ["bash", str(clone / "deploy" / "update.sh"), "main"],
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
    pwsh = shutil.which("pwsh")
    if pwsh:
        subprocess.run(
            [pwsh, "-NoProfile", "-Command",
             f"$null = [scriptblock]::Create((Get-Content -Raw '{powershell_script}'))"],
            check=True, capture_output=True, text=True,
        )


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

    assert (deployment_clone / ".venv" / "bin" / "python").is_file()
    assert (tmp_path / "python-argv.log").read_text(encoding="utf-8").splitlines() == [
        "-m", "pip", "install", "-r", "requirements-server-linux.txt",
    ]
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
