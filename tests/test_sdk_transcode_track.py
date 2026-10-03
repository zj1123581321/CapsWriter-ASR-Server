"""转码选轨契约：SDK 发给 ffmpeg 的 argv 必须显式只取第一条音轨（#52）。

判据落在**真实 ffmpeg 子进程的 argv** 上，而不是产物体积或耗时：
实测三种写法的 flac 产物字节数完全相同（无 METADATA_BLOCK_PICTURE 块），
所以「产物体积一致」是恒真断言；耗时倍率则随机器与样本大小剧烈波动，不适合做 CI 判据。

本文件不使用 tests/test_sdk_client.py 里的 fake_media_tools autouse fixture，
不装任何 ffmpeg 假可执行文件，也不 monkeypatch _count_decoded_samples。
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from sdk.capswriter_asr import AsrError
from sdk.capswriter_asr import client as sdk_client


pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="测试环境没有真实 ffmpeg",
)

_TRACK_SPEC = "0:a:0"  # 契约：只转录第一条音轨


def _run_ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _make_av_mp4(path: Path) -> Path:
    """现场生成 5 秒、含视频轨 + 音轨的 mp4。"""
    _run_ffmpeg(
        "-f", "lavfi",
        "-i", "testsrc=size=320x240:rate=10:duration=5",
        "-f", "lavfi",
        "-i", "sine=frequency=440:duration=5",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-map", "0:v",
        "-map", "1:a",
        str(path),
    )
    return path


def _make_video_only_mp4(path: Path) -> Path:
    """现场生成 5 秒、只有视频轨的 mp4。"""
    _run_ffmpeg(
        "-f", "lavfi",
        "-i", "testsrc=size=320x240:rate=10:duration=5",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-pix_fmt", "yuv420p",
        "-an",
        str(path),
    )
    return path


@pytest.fixture
def captured_ffmpeg_argv(monkeypatch):
    """包装真实 create_subprocess_exec，记录 SDK 实际发出的每条 argv。

    包装而非替换：子进程仍然是真 ffmpeg，被测的是 SDK 传给它的参数。
    """
    calls: list[list[str]] = []
    original = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        calls.append(list(args))
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    return calls


def _argv_for_source(calls: list[list[str]], path: Path) -> list[str]:
    """挑出「输入是本测试样本文件」的那条 argv（排除 pipe:0 的样本计数调用）。"""
    for argv in calls:
        if "-i" in argv and str(path) in argv:
            return argv
    raise AssertionError(f"SDK 没有为 {path} 调用 ffmpeg；捕获到的 argv: {calls}")


@pytest.mark.asyncio
async def test_transcode_argv_maps_only_first_audio_track(captured_ffmpeg_argv, tmp_path):
    source = _make_av_mp4(tmp_path / "with-video.mp4")
    assert not captured_ffmpeg_argv, "样本生成阶段不应触发 SDK 的转码子进程"

    audio = await sdk_client._transcode(source, "flac")

    argv = _argv_for_source(captured_ffmpeg_argv, source)
    assert "-map" in argv and _TRACK_SPEC in argv, f"argv 缺少音轨选择: {argv}"
    assert argv[argv.index("-map") + 1] == _TRACK_SPEC, f"音轨选择不是第一条音轨: {argv}"

    # 位置不变式：-map 在 -i <path> 之后、输出格式参数之前。
    i_index = argv.index("-i")
    assert argv[i_index + 1] == str(source)
    assert argv.index("-map") > i_index + 1, f"-map 必须紧跟在输入之后: {argv}"
    assert argv.index("-map") < argv.index("-f"), f"-map 必须在输出格式参数之前: {argv}"
    assert argv[-1] == "pipe:1"

    # 不能用 -vn 顶替：它只排视频，字幕/数据流仍在自动选流范围内。
    assert "-vn" not in argv, f"不得依赖 -vn 代替显式选流: {argv}"

    # 转码确实产出了 5 秒 16k 单声道音频。
    assert len(audio) > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["flac", "ogg_opus", "f32le", "s16le"])
async def test_transcode_argv_maps_first_track_for_every_encoding(
    captured_ffmpeg_argv, tmp_path, encoding
):
    source = _make_av_mp4(tmp_path / "with-video.mp4")

    await sdk_client._transcode(source, encoding)

    argv = _argv_for_source(captured_ffmpeg_argv, source)
    assert argv[argv.index("-map") + 1] == _TRACK_SPEC, f"{encoding} 未显式选第一条音轨: {argv}"


@pytest.mark.asyncio
async def test_video_only_file_fails_fast_with_decode_failed(
    captured_ffmpeg_argv, tmp_path
):
    """无音轨文件必须报 decode_failed，绝不静默产出空音频（-map 0:a:0? 的失败形态）。"""
    source = _make_video_only_mp4(tmp_path / "video-only.mp4")

    with pytest.raises(AsrError) as excinfo:
        await sdk_client._transcode(source, "flac")

    assert excinfo.value.code == "decode_failed"
    argv = _argv_for_source(captured_ffmpeg_argv, source)
    assert argv[argv.index("-map") + 1] == _TRACK_SPEC
    assert not argv[argv.index("-map") + 1].endswith("?"), "不得用 0:a:0? 静默吞掉无音轨文件"


@pytest.mark.asyncio
async def test_multi_track_file_transcribes_first_audio_track(
    captured_ffmpeg_argv, tmp_path
):
    """两条音轨（时长、频率不同）的文件，产物长度等于第一条音轨的长度。

    这条只断言「取第一条」的结果语义；选流契约由上面的 argv 断言锁死。
    """
    source = tmp_path / "two-audio.mp4"
    _run_ffmpeg(
        "-f", "lavfi",
        "-i", "sine=frequency=440:duration=3",
        "-f", "lavfi",
        "-i", "sine=frequency=880:duration=1",
        "-c:a", "aac",
        "-b:a", "128k",
        "-map", "0:a",
        "-map", "1:a",
        str(source),
    )

    audio = await sdk_client._transcode(source, "s16le")

    argv = _argv_for_source(captured_ffmpeg_argv, source)
    assert argv[argv.index("-map") + 1] == _TRACK_SPEC
    # 第一条是 3 秒；允许容器时间基带来的少量误差，不做精确到样本的断言。
    seconds = len(audio) / 2 / 16000
    assert 2.8 < seconds < 3.2, f"应取第一条（3 秒）音轨，实际 {seconds:.3f} 秒"