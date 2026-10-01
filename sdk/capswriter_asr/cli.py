"""命令行入口。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .client import AsrError, transcribe_file_sync
from .http_client import (
    get_file_job_http_sync,
    get_file_result_http_sync,
    resume_file_http_sync,
    submit_file_http_sync,
)
from .outputs import save_outputs


def _formats(value: str) -> set[str]:
    formats = set(value.split(","))
    if not formats or not formats <= {"srt", "txt", "json"}:
        raise ValueError("--format 只接受 srt、txt、json 的逗号分隔组合")
    return formats


def _http_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m capswriter_asr http",
        description="提交、恢复或查询 HTTP 文件任务",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    submit = commands.add_parser("submit", help="提交原文件并保存恢复信息")
    submit.add_argument("file", type=Path, help="待提交的原文件")
    submit.add_argument("--url", required=True, help="HTTP 服务端地址")
    submit.add_argument("--resume-file", required=True, type=Path, help="恢复信息路径")
    submit.add_argument("--language")
    submit.add_argument("--context")
    submit.add_argument("--model")
    submit.add_argument("--seg-duration", type=float, default=15.0)
    submit.add_argument("--seg-overlap", type=float, default=2.0)
    submit.add_argument("--chunk-bytes", type=int, default=1048576)

    resume = commands.add_parser("resume", help="显式恢复未完成的上传")
    resume.add_argument("file", type=Path, help="原文件")
    resume.add_argument("--url", required=True, help="HTTP 服务端地址")
    resume.add_argument("--resume-file", required=True, type=Path, help="恢复信息路径")

    for name, help_text in (
        ("status", "查询任务状态"),
        ("result", "领取 DONE 任务结果"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--url", required=True, help="HTTP 服务端地址")
        command.add_argument("--resume-file", required=True, type=Path, help="恢复信息路径")
        if name == "result":
            command.add_argument(
                "file",
                nargs="?",
                type=Path,
                help="输出文件名来源；省略时使用 result",
            )
            command.add_argument("--out-dir", type=Path, default=None, help="输出目录")
            command.add_argument("--format", default="srt", help="srt、txt、json 的逗号分隔组合")
    return parser


def _http_main(argv: list[str]) -> int:
    parser = _http_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "submit":
            handle = submit_file_http_sync(
                args.file,
                args.url,
                resume_path=args.resume_file,
                language=args.language,
                context=args.context,
                model=args.model,
                seg_duration=args.seg_duration,
                seg_overlap=args.seg_overlap,
                chunk_bytes=args.chunk_bytes,
            )
            print(
                f"HTTP 文件已受理：任务 {handle.job_id or '待提交'}，状态 {handle.state}；"
                f"恢复信息已保存到 {args.resume_file}。受理不等于识别完成。"
            )
            return 0
        if args.command == "resume":
            handle = resume_file_http_sync(
                args.file,
                args.url,
                resume_path=args.resume_file,
            )
            print(
                f"HTTP 文件已恢复并受理：任务 {handle.job_id or '待提交'}，状态 {handle.state}；"
                f"恢复信息在 {args.resume_file}。受理不等于识别完成。"
            )
            return 0
        if args.command == "status":
            status = get_file_job_http_sync(args.url, resume_path=args.resume_file)
            suffix = f"，错误码 {status.error_code}" if status.error_code else ""
            print(f"HTTP 文件任务状态：{status.state}（任务 {status.job_id}{suffix}）")
            return 0
        transcript = get_file_result_http_sync(args.url, resume_path=args.resume_file)
        try:
            formats = _formats(args.format)
        except ValueError as exc:
            parser.error(str(exc))
        output_source = args.file if args.file is not None else Path("result")
        save_outputs(output_source, transcript, args.out_dir, formats)
        directory = args.out_dir if args.out_dir is not None else output_source.parent
        print(f"HTTP 文件结果已写入 {directory}（格式：{','.join(sorted(formats))}）。")
        return 0
    except AsrError as exc:
        print(f"HTTP 文件操作失败 [{exc.code}]: {exc.message}", file=sys.stderr)
        if exc.recovery_path is not None:
            print(f"可使用该恢复文件显式重试：{exc.recovery_path}", file=sys.stderr)
        return 1


def _legacy_main(argv: list[str]) -> int:
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
    try:
        formats = _formats(args.format)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        transcript = transcribe_file_sync(args.audio_file, args.url, encoding=args.encoding)
    except AsrError as exc:
        print(f"转录失败 [{exc.code}]: {exc.message}", file=sys.stderr)
        return 1
    save_outputs(args.audio_file, transcript, args.out_dir, formats)
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] == "http":
        return _http_main(arguments[1:])
    return _legacy_main(arguments)
