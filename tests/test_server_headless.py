# coding: utf-8
"""
服务端无头化行为验收测试（T1）

验证服务端作为局域网守护进程（pm2 / systemd / Windows 计划任务）运行时：
1. 端口被占用 → 立即以退出码 1 退出，无任何「按回车」交互；
2. `import core.server` / `import core.server.app` 不拉起 tkinter / core.ui / pystray；
3. 一次 SIGTERM 即触发 stop() 清理并以退出码 0 退出（仅 POSIX）；
4. 服务端源码不再含 input() 交互路径；
5. 环境变量 CW_LOG_LEVEL 可覆盖 server logger 级别。

所有子进程探测都以 stdin=DEVNULL 运行，模拟无 tty 的无人值守环境；
不依赖真实模型：识别子进程与网络监听按用例桩掉，只走真实被测路径。
"""

import logging
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _child_env(**overrides) -> dict:
    """子进程环境：仓库根入 PYTHONPATH，其余继承（CW_* 覆盖项按用例传入）。"""
    env = {**os.environ}
    env['PYTHONPATH'] = str(REPO_ROOT) + os.pathsep + env.get('PYTHONPATH', '')
    env.update(overrides)
    return env


def _write_probe(tmp_path: Path, name: str, content: str) -> Path:
    script = tmp_path / name
    script.write_text(content, encoding='utf-8')
    return script


# 端口占用探测：桩掉识别子进程（不加载真实模型），走真实的端口自检 → SystemExit(1)
PORT_CONFLICT_PROBE = '''\
from core.server.app import CapsWriterServer

server = CapsWriterServer()
server.process_manager.start = lambda: None  # 桩掉识别子进程，不加载真实模型
server.start()
'''

# 导入隔离探测：导入服务端全部真实链路后检查 GUI 模块是否被拉起
IMPORT_ISOLATION_PROBE = '''\
import sys
import core.server
import core.server.app

bad = [m for m in ('tkinter', 'core.ui', 'pystray') if m in sys.modules]
print('BAD_MODULES:', bad)
sys.exit(1 if bad else 0)
'''

# SIGTERM 探测：桩掉识别子进程与网络监听，事件循环挂起等待信号
SIGTERM_PROBE = '''\
import asyncio
from core.server.app import CapsWriterServer

server = CapsWriterServer()

async def _hang():
    print('SERVER_READY', flush=True)  # 就绪信号：信号处理器已注册、事件循环已运行
    await asyncio.Event().wait()

server.process_manager.start = lambda: None  # 桩掉识别子进程
server.socket_manager.start = _hang           # 桩掉网络监听
server.start()
'''

# 日志级别探测：打印 server logger 的生效级别
LOG_LEVEL_PROBE = '''\
import logging
import core.server

print('SERVER_LOGGER_LEVEL:', logging.getLogger('server').getEffectiveLevel())
'''


def test_port_conflict_exits_1_without_input(tmp_path):
    """端口被占用时：退出码 1，无「按回车」交互，无 tty 下不挂死。"""
    blocker = socket.socket()
    blocker.bind(('127.0.0.1', 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        script = _write_probe(tmp_path, 'port_conflict_probe.py', PORT_CONFLICT_PROBE)
        proc = subprocess.run(
            [sys.executable, str(script)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            env=_child_env(CW_ADDR='127.0.0.1', CW_PORT=str(port)),
            cwd=str(REPO_ROOT),
        )
    finally:
        blocker.close()

    output = proc.stdout + proc.stderr
    assert proc.returncode == 1, f'期望退出码 1，实际 {proc.returncode}\n{output}'
    assert '按回车' not in output, f'出现了交互提示\n{output}'


def test_import_core_server_does_not_pull_gui_modules(tmp_path):
    """import core.server（含 app 链路）后 tkinter / core.ui / pystray 不得出现在 sys.modules。"""
    script = _write_probe(tmp_path, 'import_isolation_probe.py', IMPORT_ISOLATION_PROBE)
    proc = subprocess.run(
        [sys.executable, str(script)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        env=_child_env(),
        cwd=str(REPO_ROOT),
    )
    print(proc.stdout, proc.stderr)
    assert proc.returncode == 0, f'GUI 模块被拉起：{proc.stdout}\n{proc.stderr}'


@pytest.mark.skipif(sys.platform == 'win32', reason='Windows 不投递 SIGTERM')
def test_sigterm_triggers_stop_and_exits_0(tmp_path):
    """一次 SIGTERM → stop() 清理 → 5 秒内退出码 0。"""
    script = _write_probe(tmp_path, 'sigterm_probe.py', SIGTERM_PROBE)
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=_child_env(),
        cwd=str(REPO_ROOT),
    )
    try:
        # 等探测脚本宣告就绪（信号处理器已注册、事件循环已挂起），避免注册前竞态
        deadline = time.monotonic() + 15
        ready = False
        for line in proc.stdout:
            if 'SERVER_READY' in line:
                ready = True
                break
            if time.monotonic() > deadline or proc.poll() is not None:
                break
        assert ready, f'探测脚本未就绪即退出 (rc={proc.poll()})'

        proc.send_signal(signal.SIGTERM)
        try:
            rc = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = proc.wait()
            raise AssertionError(f'SIGTERM 后 5 秒未退出（已 kill，rc={rc}）')

        assert rc == 0, f'期望退出码 0，实际 {rc}'
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_server_sources_have_no_input_interaction():
    """服务端源码不再含 input() 交互路径（端口自检、模型检查均 fail fast）。"""
    for rel in ('core/server/connection/server_manager.py', 'core/server/worker/check_model.py'):
        source = (REPO_ROOT / rel).read_text(encoding='utf-8')
        assert 'input(' not in source, f'{rel} 仍含 input() 调用'
        assert '按回车' not in source, f'{rel} 仍含按回车提示'


@pytest.mark.parametrize('level_name, expected', [
    ('DEBUG', logging.DEBUG),
    # 默认 config_server.log_level 已是 DEBUG，INFO 用例才能证明环境变量真的被读取
    ('INFO', logging.INFO),
])
def test_cw_log_level_env_overrides_server_logger(tmp_path, level_name, expected):
    """CW_LOG_LEVEL 覆盖 server logger 级别（未设置时才沿用 Config.log_level）。"""
    script = _write_probe(tmp_path, f'log_level_probe_{level_name}.py', LOG_LEVEL_PROBE)
    proc = subprocess.run(
        [sys.executable, str(script)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        env=_child_env(CW_LOG_LEVEL=level_name),
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, f'{proc.stdout}\n{proc.stderr}'
    actual = None
    for line in proc.stdout.splitlines():
        if line.startswith('SERVER_LOGGER_LEVEL:'):
            actual = int(line.split(':', 1)[1])
    assert actual == expected, f'CW_LOG_LEVEL={level_name} 期望 {expected}，实际 {actual}\n{proc.stdout}'
