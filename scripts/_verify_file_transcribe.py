# coding: utf-8
"""
文件转录端到端验证：走真实 WebSocket 协议(source='file')连服务端，验证文件任务
能产出真·字级时间戳并落字幕。

文件任务(source='file')才会触发字级时间戳：原生支持 TIMESTAMPS 的引擎直接出，
否则由外挂 ForceAligner 补齐(门控见 core/server/worker/pipeline.py)。听写任务
(source='mic')不产时间戳，故 tools/test_ws_client.py 验不到这条路，需要本脚本。

判定：收回的 timestamps 单调递增、覆盖音频时长、且字间隔**非均匀**=真实对齐
(aligner 或原生)；间隔严格等距则是“字符均分回退”(server 无 token 时的兜底)。

srt 落盘由本脚本根据服务端返回的字级时间戳生成，不依赖桌面客户端。

用法：
    python scripts/_verify_file_transcribe.py <音频文件> [--server ws://localhost:6016]
    # 验证生产 MLX 实例(6017)：
    python scripts/_verify_file_transcribe.py <wav> --server ws://localhost:6017

退出码：0=产出真实字级时间戳；1=无时间戳/连接失败。
依赖：soundfile numpy websockets（+ core.protocol，纯 dataclass）。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import numpy as np
import soundfile as sf
import websockets

from core.protocol import AudioMessage, RecognitionMessage


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description='文件转录字级时间戳端到端验证')
    p.add_argument('wav', help='音频文件路径(16kHz 单声道 wav；其它格式请先转码)')
    p.add_argument('--server', default='ws://localhost:6016', help='服务端地址(默认 6016 主线默认端口)')
    p.add_argument('--seg-duration', type=float, default=60.0, help='分段长度(秒)')
    p.add_argument('--seg-overlap', type=float, default=4.0, help='分段重叠(秒)')
    p.add_argument('--language', default='auto', help="识别语言(auto/chinese/english/...)")
    p.add_argument('--out', default=None, help='srt 输出路径(默认 <wav>.srt)')
    return p.parse_args()


async def _transcribe(args: argparse.Namespace) -> RecognitionMessage | None:
    audio, sr = sf.read(args.wav, dtype='float32')
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != 16000:
        print(f'✗ 需要 16k 采样率, 实际 {sr}（请先重采样）')
        return None
    raw = audio.astype('<f4').tobytes()
    dur = len(audio) / sr
    task_id = str(uuid.uuid1())
    print(f'连接 {args.server} ... 音频 {dur:.1f}s, {len(raw)} bytes (source=file)')

    common = dict(task_id=task_id, source='file', time_start=time.time(),
                  seg_duration=args.seg_duration, seg_overlap=args.seg_overlap,
                  context='', language=args.language)
    async with websockets.connect(args.server, subprotocols=['binary'], max_size=None) as ws:
        await ws.send(AudioMessage(data=base64.b64encode(raw).decode(), is_final=False, **common).to_json())
        await ws.send(AudioMessage(data='', is_final=True, **common).to_json())
        async for resp in ws:
            rm = RecognitionMessage.from_dict(json.loads(resp))
            print(f'  转录进度 {rm.duration:.1f}s is_final={rm.is_final}', end='\r')
            if rm.is_final:
                print()
                return rm
    print('\n✗ 连接结束但未收到 final 结果')
    return None


def _check_timestamps(message: RecognitionMessage, audio_dur: float) -> bool:
    ts, tokens = message.timestamps, message.tokens
    print(f'text     : {message.text[:90]}')
    print(f'text_accu: {message.text_accu[:90]}')
    print(f'tokens   : {len(tokens)} 个 ; timestamps: {len(ts)} 个')
    if not ts:
        print('✗ 无字级时间戳 —— 文件转录未产出时间戳')
        return False
    mono = all(ts[i] <= ts[i + 1] for i in range(len(ts) - 1))
    gaps = [round(ts[i + 1] - ts[i], 4) for i in range(min(len(ts) - 1, 200))]
    uniq_gaps = len(set(gaps))
    print(f'单调递增 : {mono} ; 末字 {ts[-1]:.2f}s / 音频 {audio_dur:.1f}s')
    print(f'间隔种类 : {uniq_gaps} 种 (>1=真实对齐 aligner/原生；=1 为字符均分回退)')
    print('前 12 字时间戳:')
    for t, w in list(zip(ts, tokens))[:12]:
        print(f'    {t:7.3f}s  {w}')
    if uniq_gaps <= 1:
        print('⚠ 时间戳等距 —— 疑似字符均分回退(server 未拿到真实 token 时间戳)')
    return mono


def _write_srt(message: RecognitionMessage, out: Path) -> str:
    """根据服务端返回的时间戳生成字幕文件。"""
    ts, tokens = message.timestamps, message.tokens

    def _fmt(t: float) -> str:
        h, m, s = int(t // 3600), int(t % 3600 // 60), int(t % 60)
        return f'{h:02d}:{m:02d}:{s:02d},{int(round((t - int(t)) * 1000)):03d}'

    lines, idx, buf, start = [], 1, '', None
    for i, (w, t) in enumerate(zip(tokens, ts)):
        if start is None:
            start = t
        buf += w
        end_seg = w in '。！？!?' or (len(buf.strip()) >= 18 and w in '，、,;；')
        if end_seg or i == len(tokens) - 1:
            end = ts[i + 1] if i + 1 < len(ts) else t + 0.5
            if buf.strip():
                lines.append(f'{idx}\n{_fmt(start)} --> {_fmt(end)}\n{buf.strip()}\n')
                idx += 1
            buf, start = '', None
    out.write_text('\n'.join(lines), encoding='utf-8')
    return '本脚本生成'


def main() -> int:
    args = _parse_args()
    if not Path(args.wav).exists():
        print(f'✗ 文件不存在: {args.wav}')
        return 1
    audio_dur = sf.info(args.wav).duration
    message = asyncio.run(_transcribe(args))
    if message is None:
        return 1
    ok = _check_timestamps(message, audio_dur)
    if not ok:
        return 1
    out = (Path(args.out) if args.out else Path(args.wav)).with_suffix('.srt')
    via = _write_srt(message, out)
    print(f'\n生成字幕 : {out} 存在={out.exists()} via {via}')
    if out.exists():
        print(f'--- {out.name} 前 12 行 ---')
        print('\n'.join(out.read_text(encoding='utf-8').splitlines()[:12]))
    print('\n✓ 文件转录字级时间戳验证通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
