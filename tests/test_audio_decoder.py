import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

import core.server.connection.audio_decoder as audio_decoder
from core.server.connection.audio_decoder import AudioDecodeError, AudioDecoder


async def _collect(decoder):
    return [chunk async for chunk in decoder.pcm_chunks()]


def _ffmpeg(*args, input_data=None):
    return subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", *args],
        input=input_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout


@pytest.fixture(scope="module")
def compressed_audio():
    sample_rate = 16000
    times = np.arange(sample_rate * 3, dtype=np.float64) / sample_rate
    signal = (
        0.36 * np.sin(2 * np.pi * 440 * times)
        + 0.19 * np.sin(2 * np.pi * 880 * times)
    ).astype("<f4")
    raw = signal.tobytes()
    flac = _ffmpeg(
        "-f", "f32le", "-ar", str(sample_rate), "-ac", "1", "-i", "pipe:0",
        "-c:a", "flac", "-f", "flac", "pipe:1", input_data=raw,
    )
    opus = _ffmpeg(
        "-f", "f32le", "-ar", str(sample_rate), "-ac", "1", "-i", "pipe:0",
        "-c:a", "libopus", "-b:a", "64k", "-f", "ogg", "pipe:1", input_data=raw,
    )
    return signal, flac, opus


def _irregular_chunks(data):
    offset = 0
    for size in (1, 7, 4096):
        yield data[offset:offset + size]
        offset += size
    if offset < len(data):
        yield data[offset:]


async def _decode_chunks(encoding, chunks):
    decoder = AudioDecoder(encoding)
    consumer = asyncio.create_task(_collect(decoder))
    async with decoder:
        for chunk in chunks:
            await decoder.feed(chunk)
        await decoder.finish()
    return np.concatenate(await consumer), decoder


@pytest.mark.asyncio
async def test_f32le_preserves_values_across_aligned_chunks():
    expected = np.array([0.0, -0.25, 0.5, 1.0, -1.0], dtype="<f4")
    decoder = AudioDecoder("f32le")
    consumer = asyncio.create_task(_collect(decoder))

    for data in (expected.tobytes()[:4], expected.tobytes()[4:12], expected.tobytes()[12:]):
        await decoder.feed(data)
    await decoder.finish()
    actual = np.concatenate(await consumer)

    np.testing.assert_array_equal(actual, expected)
    assert decoder.samples_emitted == expected.size


@pytest.mark.asyncio
async def test_f32le_rejects_unaligned_chunk():
    decoder = AudioDecoder("f32le")

    with pytest.raises(AudioDecodeError) as caught:
        await decoder.feed(b"12345")

    assert caught.value.code == "decode_failed"


@pytest.mark.asyncio
async def test_s16le_converts_little_endian_samples_to_float32():
    expected_i16 = np.array([-32768, -1000, 0, 1234, 32767], dtype="<i2")
    decoder = AudioDecoder("s16le")
    consumer = asyncio.create_task(_collect(decoder))

    for data in (expected_i16.tobytes()[:2], expected_i16.tobytes()[2:8], expected_i16.tobytes()[8:]):
        await decoder.feed(data)
    await decoder.finish()
    actual = np.concatenate(await consumer)

    np.testing.assert_array_equal(actual, expected_i16.astype(np.float32) / 32768.0)
    assert decoder.samples_emitted == expected_i16.size


@pytest.mark.asyncio
async def test_s16le_rejects_odd_byte_chunk():
    decoder = AudioDecoder("s16le")

    with pytest.raises(AudioDecodeError) as caught:
        await decoder.feed(b"123")

    assert caught.value.code == "decode_failed"


@pytest.mark.asyncio
async def test_flac_stream_accepts_irregular_input_and_preserves_samples(compressed_audio):
    expected, flac, _ = compressed_audio

    actual, decoder = await _decode_chunks("flac", _irregular_chunks(flac))

    assert abs(actual.size - expected.size) <= 4608
    assert np.max(np.abs(actual[:min(actual.size, expected.size)] - expected[:min(actual.size, expected.size)])) < 1e-3
    assert decoder.process.returncode == 0
    assert decoder.samples_emitted == actual.size


@pytest.mark.asyncio
async def test_ogg_opus_stream_preserves_duration_and_signal(compressed_audio):
    expected, _, opus = compressed_audio

    actual, decoder = await _decode_chunks("ogg_opus", _irregular_chunks(opus))
    fft_size = 1 << (actual.size + expected.size - 2).bit_length()
    correlation = np.fft.irfft(
        np.fft.rfft(actual, fft_size) * np.conj(np.fft.rfft(expected, fft_size)),
        fft_size,
    )
    normalized_peak = np.max(np.abs(correlation)) / np.sqrt(
        np.sum(actual * actual) * np.sum(expected * expected)
    )

    assert abs(actual.size - expected.size) / 16000 < 0.1
    assert normalized_peak > 0.9
    assert decoder.process.returncode == 0


@pytest.mark.asyncio
async def test_truncated_flac_matches_ffmpeg_short_success_behavior(compressed_audio):
    expected, flac, _ = compressed_audio
    decoder = AudioDecoder("flac")
    consumer = asyncio.create_task(_collect(decoder))

    async with decoder:
        await decoder.feed(flac[:int(len(flac) * 0.4)])
        await decoder.finish()
    actual = np.concatenate(await consumer)

    assert decoder.process.returncode == 0
    assert 0 < actual.size < expected.size * 0.8


@pytest.mark.asyncio
async def test_random_bytes_fail_and_reap_ffmpeg():
    decoder = AudioDecoder("flac")
    consumer = asyncio.create_task(_collect(decoder))

    async with decoder:
        await decoder.feed(os.urandom(4096))
        with pytest.raises(AudioDecodeError) as caught:
            await decoder.finish()
    await asyncio.gather(consumer, return_exceptions=True)

    assert caught.value.code == "decode_failed"
    assert "ffmpeg stderr 末尾：" in caught.value.message
    assert decoder.process.returncode is not None


@pytest.mark.asyncio
async def test_normal_finish_reaps_ffmpeg(compressed_audio):
    _, flac, _ = compressed_audio
    decoder = AudioDecoder("flac")
    consumer = asyncio.create_task(_collect(decoder))

    async with decoder:
        await decoder.feed(flac)
        await decoder.finish()
    await consumer

    assert decoder.process.returncode is not None


@pytest.mark.asyncio
async def test_cancel_closes_blocked_ffmpeg_stdin_and_reaps_process():
    decoder = AudioDecoder("flac")
    await decoder.feed(b"")
    process = decoder.process
    assert process.returncode is None
    started = time.monotonic()

    await decoder.cancel()

    assert time.monotonic() - started < 5
    assert process.returncode is not None


@pytest.mark.asyncio
async def test_cancel_kills_and_waits_for_unresponsive_ffmpeg(tmp_path, monkeypatch):
    wrapper = Path(tmp_path) / "ffmpeg"
    wrapper.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(audio_decoder, "_CANCEL_TIMEOUT_SECONDS", 0.05)
    decoder = AudioDecoder("flac")
    await decoder.feed(b"wait")
    process = decoder.process

    try:
        await decoder.cancel()
        assert process.returncode is not None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        await asyncio.gather(
            decoder._writer_task, decoder._reader_task, decoder._stderr_task,
            return_exceptions=True,
        )


@pytest.mark.asyncio
async def test_large_flac_stream_finishes_with_concurrent_pcm_consumption():
    started = time.monotonic()
    flac = _ffmpeg(
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=600",
        "-ac", "1", "-c:a", "flac", "-f", "flac", "pipe:1",
    )
    decoder = AudioDecoder("flac")
    consumer = asyncio.create_task(_collect(decoder))

    async def decode():
        async with decoder:
            for offset in range(0, len(flac), 65536):
                await decoder.feed(flac[offset:offset + 65536])
            await decoder.finish()
        return np.concatenate(await consumer)

    actual = await asyncio.wait_for(decode(), timeout=30)

    assert abs(actual.size - 600 * 16000) < 1600
    assert decoder.process.returncode == 0
    assert time.monotonic() - started < 30


def test_compressed_encodings_require_ffmpeg_on_path(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))

    assert audio_decoder.available_encodings() == ["f32le", "s16le"]
    with pytest.raises(AudioDecodeError) as caught:
        AudioDecoder("flac")
    assert caught.value.code == "unsupported_encoding"


def test_unknown_encoding_is_unsupported():
    with pytest.raises(AudioDecodeError) as caught:
        AudioDecoder("wav")
    assert caught.value.code == "unsupported_encoding"
