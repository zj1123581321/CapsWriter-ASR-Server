# coding: utf-8
"""CW_HTTP_PORT / CW_HTTP_DATA_DIR 的启用契约测试（真实子进程消费环境）。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

PROBE = '''\
import json, sys
sys.path.insert(0, {repo!r})
from config_server import HttpConfigError, resolve_http_settings

try:
    settings = resolve_http_settings()
except HttpConfigError as exc:
    print("ERROR:" + str(exc))
    raise SystemExit(2)
print("OK:" + json.dumps(None if settings is None else [settings[0], settings[1], str(settings[2])]))
'''


def _probe(tmp_path: Path, **env_overrides) -> subprocess.CompletedProcess:
    """在无 PI/DELEGATE 身份的裸环境里真实导入 config_server 并解析启用配置。"""
    script = tmp_path / "probe_config.py"
    script.write_text(PROBE.format(repo=str(REPO_ROOT)), encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "PYTHONPATH": str(REPO_ROOT),
        "LANG": "C.UTF-8",
    }
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, env=env, timeout=60,
        stdin=subprocess.DEVNULL,
    )


def test_http_is_disabled_by_default(tmp_path):
    """默认关闭：旧 WS 部署不提供任何 HTTP 变量时不做任何 HTTP 装配。"""
    result = _probe(tmp_path, CW_PORT="6016")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK:null"


def test_enabled_requires_both_port_and_absolute_data_dir(tmp_path):
    data_dir = tmp_path / "state" / "http"
    ok = _probe(tmp_path, CW_HTTP_PORT="6116", CW_HTTP_DATA_DIR=str(data_dir), CW_PORT="6016")
    assert ok.returncode == 0, ok.stderr
    assert ok.stdout.strip() == f'OK:["0.0.0.0", 6116, "{data_dir}"]'

    missing_dir = _probe(tmp_path, CW_HTTP_PORT="6116", CW_PORT="6016")
    assert missing_dir.returncode == 2
    assert "必须同时提供" in missing_dir.stdout

    missing_port = _probe(tmp_path, CW_HTTP_DATA_DIR=str(data_dir), CW_PORT="6016")
    assert missing_port.returncode == 2
    assert "必须同时提供" in missing_port.stdout


def test_relative_data_dir_and_same_port_are_rejected(tmp_path):
    relative = _probe(tmp_path, CW_HTTP_PORT="6116", CW_HTTP_DATA_DIR="relative/state", CW_PORT="6016")
    assert relative.returncode == 2
    assert "绝对路径" in relative.stdout

    same_port = _probe(tmp_path, CW_HTTP_PORT="6016", CW_HTTP_DATA_DIR=str(tmp_path / "d"), CW_PORT="6016")
    assert same_port.returncode == 2
    assert "不得与 WebSocket 端口相同" in same_port.stdout


def test_invalid_port_is_rejected_without_auto_selection(tmp_path):
    for raw in ("not-a-port", "0", "70000"):
        result = _probe(tmp_path, CW_HTTP_PORT=raw, CW_HTTP_DATA_DIR=str(tmp_path / "d"), CW_PORT="6016")
        assert result.returncode == 2, (raw, result.stdout)
    # 不推算邻近端口：失败时没有任何 listener 被创建


def test_addr_follows_current_asr_address(tmp_path):
    result = _probe(tmp_path, CW_ADDR="127.0.0.1", CW_PORT="6017",
                    CW_HTTP_PORT="6117", CW_HTTP_DATA_DIR=str(tmp_path / "d"))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().startswith('OK:["127.0.0.1", 6117,')


IDLE_PROBE = '''\
import json, sys
sys.path.insert(0, {repo!r})
from config_server import ServerConfig
from core.server.http_server import HttpServer
print("IDLE:" + json.dumps({{
    "config": ServerConfig.upload_idle_seconds,
    "reader": HttpServer._read_idle_seconds(),
}}))
'''


def _idle_probe(tmp_path: Path, **env_overrides) -> subprocess.CompletedProcess:
    script = tmp_path / "probe_idle.py"
    script.write_text(IDLE_PROBE.format(repo=str(REPO_ROOT)), encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "PYTHONPATH": str(REPO_ROOT),
        "LANG": "C.UTF-8",
    }
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, env=env, timeout=60,
        stdin=subprocess.DEVNULL,
    )


def test_upload_idle_default_300_reaches_http_reader(tmp_path):
    """裸子进程读既有 CW_UPLOAD_IDLE_SECONDS：默认 300，有值传给 HTTP reader。"""
    default = _idle_probe(tmp_path)
    assert default.returncode == 0, default.stderr
    payload = json.loads(default.stdout.strip().split("IDLE:", 1)[1])
    assert payload["config"] == 300
    assert payload["reader"] == 300
    override = _idle_probe(tmp_path, CW_UPLOAD_IDLE_SECONDS="12")
    assert override.returncode == 0, override.stderr
    payload = json.loads(override.stdout.strip().split("IDLE:", 1)[1])
    assert payload["config"] == 12
    assert payload["reader"] == 12