#!/usr/bin/env python3
# coding: utf-8
"""三路各上传一小时合成音频，采样服务端及识别进程 RSS 峰值。"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def rss_kib(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except FileNotFoundError:
        return None
    raise RuntimeError(f"/proc/{pid}/status 中没有 VmRSS: pid={pid}")


def listener_pid(port: int) -> int:
    inodes = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            rows = Path(table).read_text().splitlines()[1:]
        except FileNotFoundError:
            continue
        for row in rows:
            fields = row.split()
            if fields[1].rsplit(":", 1)[-1].upper() == f"{port:04X}" and fields[3] == "0A":
                inodes.add(fields[9])
    for proc in Path("/proc").glob("[0-9]*"):
        for descriptor in (proc / "fd").glob("*"):
            try:
                target = os.readlink(descriptor)
            except FileNotFoundError:
                continue
            if target.startswith("socket:[") and target[8:-1] in inodes:
                return int(proc.name)
    raise RuntimeError(f"本机未找到监听端口 {port} 的服务进程；远程服务请传 --server-pid")


def child_pids(pid: int) -> list[int]:
    path = Path(f"/proc/{pid}/task/{pid}/children")
    if not path.exists():
        return []
    return [int(child) for child in path.read_text().split()]


def frame(task_id: str, data: str, is_final: bool) -> str:
    return json.dumps({
        "task_id": task_id,
        "source": "mic",
        "data": data,
        "is_final": is_final,
        "time_start": 0.0,
        "seg_duration": 15.0,
        "seg_overlap": 0.0,
    })


async def upload_hour(url: str, task_id: str, data: str) -> int:
    import websockets

    async with websockets.connect(
        url,
        max_size=None,
        max_queue=1,
        ping_interval=None,
        compression=None,
        write_limit=64 * 1024,
    ) as websocket:
        async def receive_final() -> int:
            count = 0
            while True:
                response = json.loads(await websocket.recv())
                if response.get("type") == "error":
                    raise RuntimeError(f"服务端返回 {response['code']}: {response['message']}")
                count += 1
                if response["is_final"]:
                    return count

        receiving = asyncio.create_task(receive_final())
        for index in range(240):
            await websocket.send(frame(task_id, data, index == 239))
        return await receiving


async def run(args) -> dict:
    fake_server = None
    if args.server:
        url = args.server
        parsed = urlparse(url)
        server_pid = args.server_pid or listener_pid(parsed.port)
        children = [args.worker_pid] if args.worker_pid else child_pids(server_pid)
        if not children:
            raise RuntimeError("没有发现服务端子进程；请用 --worker-pid 指定识别进程")
        mode = "server"
    else:
        os.environ.setdefault("CW_MAX_INFLIGHT_SEGMENTS", "4")
        from tests.harness.server import ManagedFakeServerHarness

        fake_server = await ManagedFakeServerHarness.start()
        url = fake_server.url
        server_pid = fake_server.process.pid
        children = [fake_server.worker_pid]
        mode = "fake"

    audio = base64.b64encode(bytes(15 * 16000 * 4)).decode("ascii")
    stop_sampling = asyncio.Event()
    samples = []
    started = time.monotonic()

    async def sample_rss():
        while not stop_sampling.is_set():
            child_rss = [rss_kib(pid) for pid in children]
            samples.append({
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "server_rss_kib": rss_kib(server_pid),
                "child_rss_kib": sum(value for value in child_rss if value is not None),
            })
            await asyncio.sleep(1)

    sampler = asyncio.create_task(sample_rss())
    try:
        results = await asyncio.gather(*(
            upload_hour(url, f"memory-{index}", audio) for index in range(3)
        ))
    finally:
        stop_sampling.set()
        await sampler
        if fake_server is not None:
            await fake_server.stop()

    return {
        "mode": mode,
        "audio_seconds_per_connection": 3600,
        "connections": 3,
        "results_per_connection": results,
        "sample_count": len(samples),
        "peak_rss_kib": {
            "server": max(sample["server_rss_kib"] or 0 for sample in samples),
            "children": max(sample["child_rss_kib"] for sample in samples),
        },
        "samples": samples,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", help="真实服务端 WebSocket URL（本机，或同时给 PID）")
    parser.add_argument("--server-pid", type=int, help="真实服务端主进程 PID")
    parser.add_argument("--worker-pid", type=int, help="真实服务端识别子进程 PID")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
