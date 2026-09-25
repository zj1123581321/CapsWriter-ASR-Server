# coding: utf-8
"""
WebSocket 接收处理模块

处理客户端发送的音频数据，进行分段和缓冲，提交到识别队列。
"""

import asyncio
import binascii
import json
import math
import os
import time
from base64 import b64decode

import numpy as np
import websockets

from ..state import console
from ..state import (
    begin_task,
    ensure_server_runtime,
    register_segment_submission,
    transition_terminal,
    set_task_draining,
)
from ..schema import Task
from config_server import (
    ServerConfig as Config,
    Qwen3ASRGGUFArgs,
    QwenASRMLXArgs,
)
from core.protocol import AudioMessage
from core.constants import AudioFormat
from core.tools.my_status import Status
from .segmenter import get_cut_finder
from .audio_decoder import AudioDecodeError, AudioDecoder
from .. import logger
from .ws_send import queue_error_and_close


# 麦克风接收状态指示器
status_mic = Status('正在接收音频', spinner='point')
MAX_AUDIO_FRAME_BYTES = 64 * 1024 * 1024
SAMPLES_TOTAL_TOLERANCE = 16000


class AudioCache:
    """
    音频缓冲区

    用于缓存接收到的音频数据，直到达到分段阈值后提交处理。
    """
    def __init__(self):
        self.chunks: bytes = b''    # 音频数据缓冲
        self.offset: float = 0.0    # 当前偏移时间（秒）
        self.byte_count: int = 0    # 累计接收字节数
        self.search_to: float = 0.0 # 切点吸附已搜索到的位置（秒），避免重复扫描
        self.task_id: str | None = None
        self.segmentation_params: tuple[float, float] | None = None
        self.started = False
        self.decoder: AudioDecoder | None = None
        self.decoder_task: asyncio.Task | None = None

    @property
    def duration(self) -> float:
        """缓冲区音频时长（秒）"""
        return AudioFormat.bytes_to_seconds(len(self.chunks))

    @property
    def total_duration(self) -> float:
        """累计接收的音频总时长（秒）"""
        return AudioFormat.bytes_to_seconds(self.byte_count)

    def reset(self) -> None:
        """重置缓冲区"""
        self.chunks = b''
        self.offset = 0.0
        self.byte_count = 0
        self.search_to = 0.0
        self.task_id = None
        self.segmentation_params = None
        self.started = False
        self.decoder = None
        self.decoder_task = None


def _engine_segment_limit() -> float | None:
    """读取当前 ASR 配置的单段上限，不加载识别模型。"""
    model_type = Config.model_type.lower()
    if model_type == 'qwen_asr':
        return Qwen3ASRGGUFArgs.chunk_size
    if model_type == 'qwen_asr_mlx':
        return QwenASRMLXArgs.chunk_size
    return None


def _validate_segmentation(msg: AudioMessage, cache: AudioCache) -> None:
    """校验当前帧分段参数，并锁定任务首帧的参数值。"""
    nominal, overlap = float(msg.seg_duration), float(msg.seg_overlap)
    if not math.isfinite(nominal) or nominal < 5:
        raise ValueError(f"seg_duration={nominal:g} 不在允许范围 [5, +∞)")
    if not math.isfinite(overlap) or overlap < 0 or overlap >= nominal / 2:
        raise ValueError(
            f"seg_overlap={overlap:g} 不在允许范围 [0, seg_duration/2={nominal / 2:g})"
        )

    limit = _engine_segment_limit()
    if limit is not None:
        if Config.seg_cut_snap:
            max_cut = max(Config.seg_max_cut, nominal + Config.seg_search_after)
            max_segment = max_cut + overlap
            values = (
                f"seg_max_cut={Config.seg_max_cut:g}, seg_duration={nominal:g}, "
                f"seg_search_after={Config.seg_search_after:g}, seg_overlap={overlap:g}"
            )
        else:
            max_segment = nominal + overlap
            values = f"seg_duration={nominal:g}, seg_overlap={overlap:g}"
        if max_segment > limit:
            raise ValueError(
                f"{values} 导致单段最长 {max_segment:g}s，允许范围 ≤ 引擎上限 {limit:g}s"
            )

    if cache.task_id == msg.task_id:
        first_nominal, first_overlap = cache.segmentation_params
        if nominal != first_nominal:
            raise ValueError(
                f"seg_duration={nominal:g} 与该任务首帧值 {first_nominal:g} 不同"
            )
        if overlap != first_overlap:
            raise ValueError(
                f"seg_overlap={overlap:g} 与该任务首帧值 {first_overlap:g} 不同"
            )
    elif cache.task_id is not None:
        raise ValueError(f"任务 {msg.task_id} 与缓冲任务 {cache.task_id} 不匹配")
    else:
        cache.task_id = msg.task_id
        cache.segmentation_params = (nominal, overlap)


def _assert_segment_within_limit(duration: float) -> None:
    limit = _engine_segment_limit()
    if limit is not None and duration > limit:
        raise AssertionError(
            f"提交段长 {duration:.6f}s 超过引擎单段上限 {limit:.6f}s"
        )


async def _submit_segments(
    msg: AudioMessage,
    cache: AudioCache,
    queue_in,
    socket_id: str,
    state=None,
    is_final: bool = False,
) -> bool:
    """缓冲达到阈值后切分并提交识别任务。

    seg_cut_snap 开启时，在名义切点附近吸附"最不像人声"的断点下刀
    （silero-VAD 优先，RMS 能量兜底），避免硬切在连续语音中间导致
    对齐器时间戳畸变：
    - file 任务：窗口内无可信断点时暂不下刀，等更多音频到达后延长
      搜索（上限 seg_max_cut，保证段长不超过引擎 chunk_size）；
    - mic 任务：音频按 1 倍速实时到达，只在已有缓冲内就地取最优点，
      绝不额外等待，不增加实时反馈延迟。
    """
    nominal, overlap = msg.seg_duration, msg.seg_overlap

    if not Config.seg_cut_snap:
        # 固定时长盲切（原始行为）
        limit = _engine_segment_limit()
        final_limit = limit if limit is not None else nominal + overlap
        while (
            cache.duration > final_limit
            if is_final
            else cache.duration >= nominal + overlap * 2
        ):
            if not await _cut_and_submit(
                msg, cache, queue_in, socket_id, cut=nominal, state=state,
                websocket=state.sockets.get(socket_id) if state is not None else None,
            ):
                return False
        return True

    w_before, w_after = Config.seg_search_before, Config.seg_search_after
    max_cut = max(Config.seg_max_cut, nominal + w_after)
    lo = max(nominal - w_before, min(nominal, 1.0))
    limit = _engine_segment_limit()
    final_limit = limit if limit is not None else max_cut + overlap
    finder = get_cut_finder()
    loop = asyncio.get_running_loop()

    while True:
        if is_final:
            if cache.duration <= final_limit:
                return True
        elif cache.duration < nominal + w_after + overlap:
            return True

        if msg.source == 'file':
            hi = min(cache.duration - overlap, max_cut)
            # 弹性等待期间没有新增可搜索区域时，等下一条消息再扫
            if not is_final and hi < max_cut and hi <= cache.search_to:
                return True
        else:
            hi = nominal + w_after

        cut, confident = await loop.run_in_executor(
            None, finder.find, cache.chunks, lo, hi, nominal
        )

        if not is_final and not confident and msg.source == 'file' and hi < max_cut:
            cache.search_to = hi
            logger.debug(f"切点吸附: [{lo:.1f}, {hi:.1f}]s 无可信断点，等待更多音频延长搜索")
            return True
        if confident:
            logger.debug(f"切点吸附: 名义 {nominal}s，在 {cut:.2f}s 找到静音断点")
        else:
            logger.info(f"切点吸附: [{lo:.1f}, {hi:.1f}]s 内无可信静音断点，取最低分点 {cut:.2f}s 下刀")

        cache.search_to = 0.0
        if not await _cut_and_submit(
            msg, cache, queue_in, socket_id, cut=cut, state=state,
            websocket=state.sockets.get(socket_id) if state is not None else None,
        ):
            return False


async def _acquire_segment_slot(state, key, websocket) -> bool:
    """等待本任务的结果名额；终态或断连会取消等待并丢弃信号量。"""
    record = state.tasks.get(key)
    if record is None or record.status in {'DONE', 'FAILED'} or record.segment_slots is None:
        return False

    semaphore = record.segment_slots
    acquire = asyncio.create_task(semaphore.acquire())
    terminal = asyncio.create_task(record.terminal_event.wait())
    closed = asyncio.create_task(websocket.wait_closed())
    waiters = (acquire, terminal, closed)
    paused_at = None
    try:
        if not acquire.done():
            paused_at = asyncio.get_running_loop().time()
            record.backpressured = True
            record.idle_state_event.set()
        done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        if terminal in done or closed in done or record.status in {'DONE', 'FAILED'}:
            if acquire.done() and acquire.result():
                semaphore.release()
            if closed in done:
                transition_terminal(state, key, 'FAILED')
            return False
        return True
    finally:
        if paused_at is not None:
            if record.status == 'RECEIVING' and record.idle_deadline is not None:
                record.idle_deadline += asyncio.get_running_loop().time() - paused_at
            record.backpressured = False
            record.idle_state_event.set()
        for waiter in waiters:
            if not waiter.done():
                waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)


async def _cut_and_submit(
    msg: AudioMessage,
    cache: AudioCache,
    queue_in,
    socket_id: str,
    cut: float,
    state=None,
    websocket=None,
) -> bool:
    """从缓冲区头部切出 [0, cut+overlap] 提交识别，缓冲区前移 cut 秒。"""
    n_stride = int(round(cut * AudioFormat.SAMPLE_RATE))
    n_segment = n_stride + int(round(msg.seg_overlap * AudioFormat.SAMPLE_RATE))
    stride_bytes = n_stride * AudioFormat.BYTES_PER_SAMPLE
    segment_bytes = n_segment * AudioFormat.BYTES_PER_SAMPLE

    segment_data = cache.chunks[:segment_bytes]
    _assert_segment_within_limit(len(segment_data) / AudioFormat.BYTES_PER_SECOND)
    cache.chunks = cache.chunks[stride_bytes:]

    task = Task(
        type=msg.source,
        data=segment_data,
        offset=cache.offset,
        task_id=msg.task_id,
        socket_id=socket_id,
        overlap=msg.seg_overlap,
        is_final=False,
        time_start=msg.time_start,
        time_submit=time.time(),
        context=msg.context,
        language=msg.language,
    )
    cache.offset += stride_bytes / AudioFormat.BYTES_PER_SECOND
    if state is not None and not await _acquire_segment_slot(
        state, (socket_id, msg.task_id), websocket
    ):
        return False
    queue_in.put(task)
    if state is not None:
        record = state.tasks.get((socket_id, msg.task_id))
        if record is not None:
            record.segments += 1
        register_segment_submission(state, (socket_id, msg.task_id), time.monotonic())
    logger.debug(
        f"提交音频片段，任务ID: {msg.task_id}, 切点: {cut:.2f}s, "
        f"偏移: {cache.offset}s, 缓冲区: {len(cache.chunks)} bytes"
    )
    return True


def _check_task_duration(samples: int) -> None:
    max_seconds = float(os.environ.get('CW_MAX_TASK_SECONDS', '14400'))
    max_samples = max_seconds * AudioFormat.SAMPLE_RATE
    if samples > max_samples:
        raise AudioDecodeError(
            'audio_too_long',
            f"解码后样本数 {samples} 超过时长上限 {max_seconds:g}s",
        )


async def _consume_compressed_pcm(websocket, msg, cache, app) -> bool:
    key = (str(websocket.id), msg.task_id)
    async for pcm in cache.decoder.pcm_chunks():
        data = pcm.astype('<f4', copy=False).tobytes()
        cache.chunks += data
        cache.byte_count += len(data)
        record = app.state.tasks[key]
        record.samples_total = cache.byte_count // AudioFormat.BYTES_PER_SAMPLE
        _check_task_duration(record.samples_total)
        if not await _submit_segments(
            msg, cache, app.state.queue_in, key[0], app.state
        ):
            return False
    return True


async def _feed_compressed(cache, data: bytes) -> bool:
    feed = asyncio.create_task(cache.decoder.feed(data))
    try:
        done, _ = await asyncio.wait(
            (feed, cache.decoder_task), return_when=asyncio.FIRST_COMPLETED
        )
        if cache.decoder_task in done:
            cache.decoder_task.result()
            if not feed.done():
                feed.cancel()
                await asyncio.gather(feed, return_exceptions=True)
            return False
        await feed
        return True
    except BaseException:
        if not feed.done():
            feed.cancel()
            await asyncio.gather(feed, return_exceptions=True)
        raise


async def _submit_final_audio(websocket, msg, cache, app, socket_id: str) -> bool:
    if msg.source == 'mic':
        status_mic.stop()
    else:
        print(f'音频文件接收完毕，时长 {cache.total_duration:.2f}s')
        logger.info(f"音频文件接收完毕，任务ID: {msg.task_id}, 时长: {cache.total_duration:.2f}s")

    if not await _submit_segments(
        msg, cache, app.state.queue_in, socket_id, app.state, is_final=True
    ):
        return False
    _assert_segment_within_limit(cache.duration)
    task = Task(
        type=msg.source,
        data=cache.chunks,
        offset=cache.offset,
        task_id=msg.task_id,
        socket_id=socket_id,
        overlap=msg.seg_overlap,
        is_final=True,
        time_start=msg.time_start,
        time_submit=time.time(),
        context=msg.context,
        language=msg.language,
    )
    if not await _acquire_segment_slot(
        app.state, (socket_id, msg.task_id), websocket
    ):
        return False
    app.state.queue_in.put(task)
    app.state.tasks[(socket_id, msg.task_id)].segments += 1
    register_segment_submission(app.state, (socket_id, msg.task_id), time.monotonic())
    logger.debug(f"提交最终片段，任务ID: {msg.task_id}, 数据大小: {len(cache.chunks)} bytes")
    cache.reset()
    return True


def _verify_samples_total(msg, record) -> None:
    if record.declared_encoding is not None:
        actual = record.samples_total
        if abs(actual - msg.samples_total) > SAMPLES_TOTAL_TOLERANCE:
            raise AudioDecodeError(
                'decode_failed',
                f"samples_total 声明 {msg.samples_total}，实际解码 {actual}，"
                f"差值超过 {SAMPLES_TOTAL_TOLERANCE}",
            )


async def message_handler(websocket, msg: AudioMessage, cache: AudioCache, app) -> bool:
    """按任务编码解码，再交给唯一的缓冲/切段执行者。"""
    global status_mic
    state = app.state
    queue_in = state.queue_in
    key = (str(websocket.id), msg.task_id)
    record = state.tasks[key]
    is_start = not cache.started
    cache.started = True

    if is_start and msg.source == 'mic' and Config.gpu_boost_enabled:
        queue_in.put(Task(
            type='cmd', task_id='gpu_boost', data=b'', offset=0, overlap=0,
            socket_id=key[0], is_final=False, time_start=0, time_submit=0,
            command='gpu_boost'
        ))

    encoding = msg.encoding or 'f32le'
    if cache.decoder is None:
        cache.decoder = AudioDecoder(encoding)
    data = b64decode(msg.data, validate=True)
    if len(data) > MAX_AUDIO_FRAME_BYTES:
        raise AudioDecodeError(
            'bad_request',
            f"单帧解码后 {len(data)} bytes 超过上限 {MAX_AUDIO_FRAME_BYTES} bytes",
        )

    if encoding in {'f32le', 's16le'}:
        sample_width = 4 if encoding == 'f32le' else 2
        if len(data) % sample_width:
            raise AudioDecodeError('decode_failed', f"{encoding} 数据长度必须是 {sample_width} 的倍数")
        if encoding == 's16le':
            pcm = np.frombuffer(data, dtype='<i2').astype(np.float32) / 32768.0
            data = pcm.astype('<f4', copy=False).tobytes()
        samples = len(data) // AudioFormat.BYTES_PER_SAMPLE
        _check_task_duration(cache.byte_count // AudioFormat.BYTES_PER_SAMPLE + samples)
        cache.chunks += data
        cache.byte_count += len(data)
        record.samples_total = cache.byte_count // AudioFormat.BYTES_PER_SAMPLE
        if not msg.is_final:
            if msg.source == 'mic':
                status_mic.start()
            elif is_start:
                console.print('正在接收音频文件...')
                logger.info(f"开始接收音频文件，任务ID: {msg.task_id}")
            return await _submit_segments(msg, cache, queue_in, key[0], state)
        _verify_samples_total(msg, record)
        return await _submit_final_audio(websocket, msg, cache, app, key[0])

    if cache.decoder_task is None:
        cache.decoder_task = asyncio.create_task(
            _consume_compressed_pcm(websocket, msg, cache, app)
        )
    if not await _feed_compressed(cache, data):
        return False
    if not msg.is_final:
        if msg.source == 'mic':
            status_mic.start()
        elif is_start:
            console.print('正在接收音频文件...')
            logger.info(f"开始接收音频文件，任务ID: {msg.task_id}")
        return True

    if msg.source == 'mic':
        status_mic.stop()
    await cache.decoder.finish()
    if not await cache.decoder_task:
        return False
    record.samples_total = cache.decoder.samples_emitted
    _verify_samples_total(msg, record)
    return await _submit_final_audio(websocket, msg, cache, app, key[0])


async def _receive_compressed_frame(websocket, record, consumer):
    receive = asyncio.create_task(websocket.recv())
    changed = None
    try:
        while True:
            if consumer.done():
                consumer.result()
                raise RuntimeError('压缩音频消费协程在末帧前结束')
            record.idle_state_event.clear()
            timeout = None if record.backpressured else max(
                0.0, record.idle_deadline - asyncio.get_running_loop().time()
            )
            changed = asyncio.create_task(record.idle_state_event.wait())
            done, _ = await asyncio.wait(
                (receive, changed, consumer),
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise TimeoutError
            if changed in done:
                continue
            if consumer in done:
                consumer.result()
                raise RuntimeError('压缩音频消费协程在末帧前结束')
            return receive.result()
    finally:
        for task in (receive, changed):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (receive, changed) if task is not None), return_exceptions=True)


async def _cancel_audio_cache(cache: AudioCache) -> None:
    if cache.decoder_task is not None and not cache.decoder_task.done():
        cache.decoder_task.cancel()
    if cache.decoder_task is not None:
        await asyncio.gather(cache.decoder_task, return_exceptions=True)
    if cache.decoder is not None:
        process = cache.decoder.process
        if process is not None and process.returncode is None:
            process.kill()
        await cache.decoder.cancel()


async def ws_recv(websocket, app) -> None:
    """
    WebSocket 接收主函数

    处理单个客户端连接，接收音频数据并分发处理。
    """
    global status_mic

    # 登记 socket 到连接池
    state = app.state
    sockets = state.sockets
    sockets_id = state.sockets_id
    socket_id = str(websocket.id)
    sockets[socket_id] = websocket
    sockets_id.append(socket_id)
    remote = websocket.remote_address
    console.print(f'[bold green]客户端已连接: {remote[0]}:{remote[1]}[/bold green]\n')
    logger.info(f"新客户端连接: {websocket}, ID: {socket_id}")

    ensure_server_runtime(state)
    outbound = asyncio.Queue(maxsize=256)
    state.out_queues[socket_id] = outbound
    sender = asyncio.create_task(_send_connection(websocket, outbound, socket_id))
    state.sender_tasks[socket_id] = sender

    # 创建音频缓冲区
    cache = AudioCache()

    # 接收并处理消息
    try:
        while True:
            active = state.connection_tasks.get(socket_id)
            record = state.tasks.get(active) if active else None
            try:
                if cache.decoder_task is not None and record is not None:
                    raw_message = await _receive_compressed_frame(
                        websocket, record, cache.decoder_task
                    )
                elif record is not None and record.status == 'RECEIVING':
                    remaining = max(
                        0.0,
                        record.idle_deadline - asyncio.get_running_loop().time(),
                    )
                    raw_message = await asyncio.wait_for(
                        websocket.recv(), timeout=remaining
                    )
                else:
                    raw_message = await websocket.recv()
            except TimeoutError:
                if active is not None:
                    await _cancel_audio_cache(cache)
                    await queue_error_and_close(
                        state, websocket, socket_id, active[1], 'bad_request',
                        'upload idle timeout', False,
                    )
                return
            except AudioDecodeError as e:
                if active is not None:
                    await _cancel_audio_cache(cache)
                    await queue_error_and_close(
                        state, websocket, socket_id, active[1], e.code,
                        e.message, False,
                    )
                return
            except websockets.ConnectionClosedOK:
                break

            task_id = ''
            try:
                data = json.loads(raw_message)
                if isinstance(data, dict) and isinstance(data.get('task_id'), str):
                    task_id = data['task_id']
                _validate_audio_dict(data)
                msg = AudioMessage.from_dict(data)
            except Exception as e:
                logger.warning(f"客户端 {socket_id} 发送坏请求: {type(e).__name__}: {e}")
                active = state.connection_tasks.get(socket_id)
                if active:
                    await _cancel_audio_cache(cache)
                    transition_terminal(state, active, 'FAILED', code='bad_request')
                if task_id and active != (socket_id, task_id):
                    malformed_key = (socket_id, task_id)
                    begin_task(state, malformed_key)
                    transition_terminal(state, malformed_key, 'FAILED', code='bad_request')
                await queue_error_and_close(
                    state, websocket, socket_id, task_id, 'bad_request',
                    f"{type(e).__name__}: {e}", False,
                )
                return

            key = (socket_id, msg.task_id)
            active = state.connection_tasks.get(socket_id)
            if active is not None and active != key:
                await _cancel_audio_cache(cache)
                transition_terminal(state, active, 'FAILED', code='task_conflict')
                begin_task(state, key)
                transition_terminal(state, key, 'FAILED', code='task_conflict')
                await queue_error_and_close(
                    state, websocket, socket_id, msg.task_id, 'task_conflict',
                    f"连接上任务 {active[1]} 尚未终结，不能开始任务 {msg.task_id}", False,
                )
                return

            if active is None:
                active_count = sum(
                    item.status not in {'DONE', 'FAILED'}
                    for item in state.tasks.values()
                )
                if active_count >= Config.max_tasks:
                    await queue_error_and_close(
                        state, websocket, socket_id, msg.task_id, 'overloaded',
                        f"服务端活动任务已达上限 {Config.max_tasks}", True,
                    )
                    return
                begin_task(state, key, Config.max_inflight_segments)
            record = state.tasks[key]
            if not record.encoding_set:
                record.declared_encoding = msg.encoding
                record.encoding = msg.encoding or 'v1'
                record.encoding_set = True
            elif msg.encoding != record.declared_encoding:
                await _cancel_audio_cache(cache)
                await queue_error_and_close(
                    state, websocket, socket_id, msg.task_id, 'bad_request',
                    '同一任务的 encoding 不可改变', False,
                )
                return
            if record.status == 'RECEIVING':
                record.idle_deadline = (
                    asyncio.get_running_loop().time() + Config.upload_idle_seconds
                )

            if record.status in {'DONE', 'FAILED'}:
                logger.warning(f"丢弃终态任务 {msg.task_id} 的迟到上行帧")
                continue

            try:
                _validate_segmentation(msg, cache)
            except ValueError as e:
                await _cancel_audio_cache(cache)
                await queue_error_and_close(
                    state, websocket, socket_id, msg.task_id, 'bad_request',
                    str(e), False,
                )
                return

            if msg.is_final and not set_task_draining(state, key):
                logger.warning(f"丢弃任务 {msg.task_id} 重复的 is_final 帧")
                continue

            try:
                if not await message_handler(websocket, msg, cache, app):
                    return
            except AudioDecodeError as e:
                await _cancel_audio_cache(cache)
                await queue_error_and_close(
                    state, websocket, socket_id, msg.task_id, e.code,
                    e.message, False,
                )
                return
            except binascii.Error as e:
                await _cancel_audio_cache(cache)
                await queue_error_and_close(
                    state, websocket, socket_id, msg.task_id, 'decode_failed',
                    f"{type(e).__name__}: {e}", False,
                )
                return
            except Exception as e:
                logger.error(f"连接 {socket_id} 处理任务异常", exc_info=True)
                await _cancel_audio_cache(cache)
                await queue_error_and_close(
                    state, websocket, socket_id, msg.task_id, 'internal',
                    f"{type(e).__name__}: {e}", True,
                )
                return

        logger.info(f"客户端正常关闭连接: {socket_id}")

    except websockets.ConnectionClosed:
        console.print("ConnectionClosed...")
        logger.warning(f"客户端连接已关闭: {socket_id}")
    except websockets.InvalidState:
        console.print("InvalidState...")
        logger.error(f"WebSocket 状态异常: {socket_id}")
    except Exception as e:
        console.print("Exception:", e)
        logger.error(f"WebSocket 接收异常，客户端ID {socket_id}: {e}", exc_info=True)
        active = state.connection_tasks.get(socket_id)
        if active:
            await _cancel_audio_cache(cache)
            await queue_error_and_close(
                state, websocket, socket_id, active[1], 'internal',
                f"{type(e).__name__}: {e}", True,
            )
    finally:
        # 清理资源
        await _cancel_audio_cache(cache)
        status_mic.stop()
        status_mic.on = False
        sockets.pop(socket_id, None)
        if socket_id in sockets_id:
            sockets_id.remove(socket_id)
        active = state.connection_tasks.get(socket_id)
        if active:
            transition_terminal(state, active, 'FAILED')
        for key in [key for key in state.tasks if key[0] == socket_id]:
            state.tasks.pop(key, None)
            state.pending_segments.pop(key, None)
        state.connection_tasks.pop(socket_id, None)
        state.out_queues.pop(socket_id, None)
        state.sender_tasks.pop(socket_id, None)
        if not sender.done():
            sender.cancel()
            try:
                await asyncio.wait_for(sender, timeout=5)
            except asyncio.CancelledError:
                pass

        console.print(f'[bold red]客户端已断开: {remote[0]}:{remote[1]}[/bold red]\n')

        # 注意：session 清理由 TaskHandler 在子进程中定期执行
        # （通过检查 sockets_id 判断客户端是否已断开）
        logger.debug(f"客户端资源已清理: {socket_id}")


async def _send_connection(websocket, outbound: asyncio.Queue, socket_id: str) -> None:
    """每个 WebSocket 独立发送，避免慢连接阻塞其它结果。"""
    try:
        while True:
            try:
                payload = await asyncio.wait_for(outbound.get(), timeout=1)
            except TimeoutError:
                # 没有待发结果是正常空闲态；周期醒来可响应连接关闭取消。
                continue
            try:
                await websocket.send(payload)
            finally:
                outbound.task_done()
    except websockets.ConnectionClosed:
        logger.debug(f"连接 {socket_id} 的发送协程随 socket 关闭")
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.error(f"连接 {socket_id} 的发送协程异常", exc_info=True)
        await websocket.close()


def _validate_audio_dict(data) -> None:
    if not isinstance(data, dict):
        raise TypeError("音频帧必须是 JSON 对象")
    required = ('task_id', 'source', 'data', 'is_final', 'time_start')
    missing = [name for name in required if name not in data]
    if missing:
        raise KeyError(f"缺少必需字段: {', '.join(missing)}")
    string_fields = ('task_id', 'source', 'data', 'context', 'language', 'encoding')
    for name in string_fields:
        if name in data and not isinstance(data[name], str):
            raise TypeError(f"字段 {name} 必须是字符串")
    if type(data['is_final']) is not bool:
        raise TypeError("字段 is_final 必须是布尔值")
    if 'samples_total' in data and (
        type(data['samples_total']) is not int or data['samples_total'] < 0
    ):
        raise TypeError("字段 samples_total 必须是非负整数")
    if 'encoding' in data and data['is_final'] and 'samples_total' not in data:
        raise KeyError("v2 末帧缺少必需字段: samples_total")
    if data['source'] not in {'mic', 'file'}:
        raise ValueError("字段 source 只能是 mic 或 file")
    number_fields = ('time_start', 'seg_duration', 'seg_overlap')
    for name in number_fields:
        if name in data and (isinstance(data[name], bool) or not isinstance(data[name], (int, float))):
            raise TypeError(f"字段 {name} 必须是数字")
