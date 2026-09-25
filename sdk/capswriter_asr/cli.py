"""命令行入口。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .client import AsrError, transcribe_file_sync
from .outputs import save_outputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m capswriter_asr",
        description="通过 CapsWriter ASR 协议 v2 转录本地音频文件",
    )
    parser.add_argument("audio_file", type=Path, help="待转录音频文件")
    parser.add_argument("--url", required=True, help="WebSocket 服务端地址")
    parser.add_argument("--encoding", default="flac", choices=["flac", "ogg_opus", "f32le", "s16le"])
    parser.add_argument("--out-dir", type=Path, default=None, help="输出目录（默认音频所在目录）")
    parser.add_argument("--format", default="srt", help="逗号分隔的输出格式：srt,txt,json")
    args = parser.parse_args(argv)
    formats = set(args.format.split(","))
    if not formats or not formats <= {"srt", "txt", "json"}:
        parser.error("--format 只接受 srt、txt、json 的逗号分隔组合")
    try:
        transcript = transcribe_file_sync(args.audio_file, args.url, encoding=args.encoding)
    except AsrError as exc:
        print(f"转录失败 [{exc.code}]: {exc.message}", file=sys.stderr)
        return 1
    save_outputs(args.audio_file, transcript, args.out_dir, formats)
    return 0
