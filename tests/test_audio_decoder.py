import asyncio

import numpy as np
import pytest

from core.server.connection.audio_decoder import AudioDecodeError, AudioDecoder


async def _collect(decoder):
    return [chunk async for chunk in decoder.pcm_chunks()]


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
