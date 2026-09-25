# CapsWriter ASR SDK

Python SDK for the CapsWriter ASR protocol v2. Requires Python 3.10 or newer and `ffmpeg` plus `ffprobe` on `PATH` for audio conversion and duration probing.

```bash
pip install "git+https://github.com/zj1123581321/CapsWriter-Offline-with-AI.git#subdirectory=sdk"
```

```python
import asyncio
from capswriter_asr import transcribe_file

async def main():
    result = await transcribe_file("meeting.m4a", "ws://127.0.0.1:6016")
    print(result.text)

asyncio.run(main())
```

Use `transcribe_file_sync(path, url, ...)` from synchronous programs. Both entry points accept `encoding` (`flac`, `ogg_opus`, `f32le`, or `s16le`), `language`, `context`, segment settings, `deadline_total`, `idle_timeout`, and an `on_progress(result_dict)` callback. Failures raise `AsrError`; the SDK checks `/health` for protocol v2 and never retries or falls back to v1.

The command line writes SRT by default. Choose multiple formats with commas:

```bash
python -m capswriter_asr recording.m4a --url ws://127.0.0.1:6016 --out-dir subtitles --format srt,txt,json
```
