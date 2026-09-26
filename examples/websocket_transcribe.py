"""短 PCM16 WAV 文件的协议 v2 WebSocket 示例。"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
import uuid
import wave
from pathlib import Path

import websockets


SAMPLE_RATE = 16000
MAX_SECONDS = 60
OVERALL_TIMEOUT_SECONDS = 120


def read_short_pcm16_wav(path: Path) -> bytes:
    """读取不超过 60 秒的 16 kHz 单声道 PCM16 WAV 裸音频。"""
    conversion = "ffmpeg -nostdin -i input.m4a -ar 16000 -ac 1 -c:a pcm_s16le output.wav"
    try:
        audio_file = wave.open(str(path), "rb")
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"输入必须是 PCM16 WAV。转码示例：{conversion}") from exc
    with audio_file as audio:
        if (
            audio.getnchannels() != 1
            or audio.getsampwidth() != 2
            or audio.getframerate() != SAMPLE_RATE
            or audio.getcomptype() != "NONE"
        ):
            raise ValueError(
                f"只支持 16 kHz、单声道、未压缩 PCM16 WAV。转码示例：{conversion}"
            )
        frames = audio.getnframes()
        if frames == 0 or frames > SAMPLE_RATE * MAX_SECONDS:
            raise ValueError(f"音频必须大于 0 秒且不超过 {MAX_SECONDS} 秒")
        pcm = audio.readframes(frames)
        if len(pcm) != frames * 2:
            raise ValueError("WAV 文件头声明的样本数与实际 PCM 数据长度不符")
        return pcm


async def transcribe_short_wav(path: Path, url: str) -> dict[str, object]:
    """上传严格校验的短 WAV，并等待协议 v2 的最终结果或明确失败。"""
    pcm = read_short_pcm16_wav(path)
    task_id = str(uuid.uuid4())
    frame = {
        "task_id": task_id,
        "source": "file",
        "encoding": "s16le",
        "data": base64.b64encode(pcm).decode("ascii"),
        "is_final": True,
        "time_start": time.time(),
        "seg_duration": 15.0,
        "seg_overlap": 2.0,
        "samples_total": len(pcm) // 2,
    }

    async def exchange() -> dict[str, object]:
        async with websockets.connect(
            url, ping_interval=None, max_size=None, max_queue=None, proxy=None
        ) as socket:
            upload = asyncio.create_task(socket.send(json.dumps(frame)))
            try:
                while True:
                    message = await socket.recv()
                    if not isinstance(message, str):
                        raise RuntimeError("服务端返回了非文本协议帧")
                    response = json.loads(message)
                    if response.get("type") == "error":
                        raise RuntimeError(
                            f"服务端错误 {response.get('code', 'unknown')}: "
                            f"{response.get('message', '未提供错误说明')}"
                        )
                    if response.get("type") != "result":
                        raise RuntimeError("服务端返回了未知协议消息")
                    if response.get("is_final") is True:
                        if response.get("task_id") != task_id:
                            raise RuntimeError("服务端最终结果的 task_id 与当前任务不符")
                        await upload
                        return response
            finally:
                if not upload.done():
                    upload.cancel()
                await asyncio.gather(upload, return_exceptions=True)

    return await asyncio.wait_for(exchange(), timeout=OVERALL_TIMEOUT_SECONDS)


def main() -> None:
    parser = argparse.ArgumentParser(description="转录短 PCM16 WAV 文件")
    parser.add_argument("audio", type=Path, help="16 kHz、单声道、PCM16 WAV 文件")
    parser.add_argument("--url", default="ws://127.0.0.1:6016", help="ASR WebSocket 地址")
    args = parser.parse_args()
    result = asyncio.run(transcribe_short_wav(args.audio, args.url))
    print(result["text"])


if __name__ == "__main__":
    main()
