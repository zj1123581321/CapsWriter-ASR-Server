# coding: utf-8
"""连接运行中的 CapsWriter v1 服务端，保存识别文本与时间戳基线。"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import statistics
import time
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import numpy as np
import soundfile as sf
import websockets

from core.protocol import AudioMessage


def _cer(reference: str, hypothesis: str) -> float:
    """按字符计算编辑距离比率，使用一行内存保存动态规划状态。"""
    if not reference:
        raise ValueError("参考文本不能为空")
    previous = list(range(len(hypothesis) + 1))
    for i, ref_char in enumerate(reference, 1):
        current = [i]
        for j, hyp_char in enumerate(hypothesis, 1):
            current.append(min(
                current[-1] + 1,
                previous[j] + 1,
                previous[j - 1] + (ref_char != hyp_char),
            ))
        previous = current
    return previous[-1] / len(reference)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="保存 CapsWriter 服务端识别基线")
    parser.add_argument("--server", required=True, help="服务端 WebSocket 地址，例如 ws://127.0.0.1:6016")
    parser.add_argument("--wav", required=True, help="16 kHz WAV 文件")
    parser.add_argument("--ref", help="参考文本文件；提供时计算 CER")
    parser.add_argument("--out", required=True, help="输出 JSON 路径")
    return parser.parse_args()


async def _transcribe(server: str, samples: np.ndarray) -> tuple[dict, float]:
    raw_audio = samples.astype("<f4", copy=False).tobytes()
    task_id = f"baseline-{time.time_ns()}"
    started = time.monotonic()
    common = {
        "task_id": task_id,
        "source": "file",
        "time_start": time.time(),
        "seg_duration": 60.0,
        "seg_overlap": 4.0,
        "context": "",
        "language": "auto",
    }
    async with websockets.connect(
        server, max_size=None, ping_interval=None, open_timeout=30
    ) as websocket:
        await websocket.send(AudioMessage(
            data=base64.b64encode(raw_audio).decode("ascii"),
            is_final=False,
            **common,
        ).to_json())
        await websocket.send(AudioMessage(data="", is_final=True, **common).to_json())
        async with asyncio.timeout(30):
            while True:
                message = json.loads(await websocket.recv())
                if message.get("task_id") == task_id and message.get("is_final"):
                    return message, time.monotonic() - started


def main() -> int:
    args = _parse_args()
    audio, sample_rate = sf.read(args.wav, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sample_rate != 16000:
        raise ValueError(f"需要 16000 Hz WAV，实际为 {sample_rate} Hz")

    result, elapsed_s = asyncio.run(_transcribe(args.server, audio))
    timestamps = [float(value) for value in result.get("timestamps", [])]
    gaps = [timestamps[i + 1] - timestamps[i] for i in range(len(timestamps) - 1)]
    reference = Path(args.ref).read_text(encoding="utf-8").strip() if args.ref else None
    text = result.get("text_accu") or result.get("text", "")
    baseline = {
        "server": args.server,
        "wav": str(args.wav),
        "duration_s": len(audio) / sample_rate,
        "text": text,
        "cer": _cer(reference, text) if reference is not None else None,
        "n_tokens": len(result.get("tokens", [])),
        "timestamps_monotonic": all(
            timestamps[i] <= timestamps[i + 1] for i in range(len(timestamps) - 1)
        ),
        "timestamps_s": timestamps,
        "timestamps_uniform_suspect": (
            len(gaps) >= 2 and statistics.pvariance(gaps) <= 1e-6
        ),
        "elapsed_s": elapsed_s,
    }
    Path(args.out).write_text(
        json.dumps(baseline, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
