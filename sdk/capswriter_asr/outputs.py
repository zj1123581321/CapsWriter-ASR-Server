"""字幕和转录文本输出。"""
from __future__ import annotations

import json
from pathlib import Path

from .client import Transcript


SRT_SOFT_LIMIT = 18


def _fmt_timestamp(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    milliseconds = total_ms % 1000
    total_seconds = total_ms // 1000
    total_minutes, second = divmod(total_seconds, 60)
    hour, minute = divmod(total_minutes, 60)
    return f"{hour:02d}:{minute:02d}:{second:02d},{milliseconds:03d}"


def write_srt(transcript: Transcript, path: Path) -> None:
    if not transcript.tokens:
        path.write_text("", encoding="utf-8")
        return
    lines: list[str] = []
    index, buffer, start = 1, "", None
    for token_index, (token, timestamp) in enumerate(
        zip(transcript.tokens, transcript.timestamps)
    ):
        if start is None:
            start = timestamp
        buffer += token
        end_segment = token in "。！？!?" or (
            len(buffer.strip()) >= SRT_SOFT_LIMIT and token in "，、,;；"
        )
        if end_segment or token_index == len(transcript.tokens) - 1:
            end = (
                transcript.timestamps[token_index + 1]
                if token_index + 1 < len(transcript.timestamps)
                else timestamp + 0.5
            )
            if buffer.strip():
                lines.append(
                    f"{index}\n{_fmt_timestamp(start)} --> {_fmt_timestamp(end)}\n"
                    f"{buffer.strip()}\n"
                )
                index += 1
            buffer, start = "", None
    path.write_text("\n".join(lines), encoding="utf-8")


def save_outputs(audio_path: Path, transcript: Transcript, out_dir: Path | None, formats: set[str]) -> None:
    directory = out_dir if out_dir is not None else audio_path.parent
    directory.mkdir(parents=True, exist_ok=True)
    stem = audio_path.stem
    if "srt" in formats:
        write_srt(transcript, directory / f"{stem}.srt")
    if "txt" in formats:
        (directory / f"{stem}.txt").write_text(transcript.text, encoding="utf-8")
    if "json" in formats:
        (directory / f"{stem}.json").write_text(
            json.dumps(transcript.raw, ensure_ascii=False), encoding="utf-8"
        )
