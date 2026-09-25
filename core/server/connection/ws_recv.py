# coding: utf-8
"""
WebSocket 接收处理模块

处理客户端发送的音频数据，进行分段和缓冲，提交到识别队列。
"""

import asyncio
import binascii
import json
import time
from base64 import b64decode

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
from config_server import ServerConfig as Config
from core.protocol import AudioMessage
from core.constants import AudioFormat
from core.tools.my_status import Status
from .segmenter import get_cut_finder
from .. import logger
from .ws_send import queue_error_and_close


# 麦克风接收状态指示器
status_mic = Status('正在接收音频', spinner='point')


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


async def _submit_segments(msg: AudioMessage, cache: AudioCache, queue_in, socket_id: str, state=None) -> None:
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
        while cache.duration >= nominal + overlap * 2:
            _cut_and_submit(msg, cache, queue_in, socket_id, cut=nominal, state=state)
        return

    w_before, w_after = Config.seg_search_before, Config.seg_search_after
    max_cut = max(Config.seg_max_cut, nominal + w_after)
    lo = max(nominal - w_before, min(nominal, 1.0))
    finder = get_cut_finder()
    loop = asyncio.get_running_loop()

    while cache.duration >= nominal + w_after + overlap:
        if msg.source == 'file':
            hi = min(cache.duration - overlap, max_cut)
            # 弹性等待期间没有新增可搜索区域时，等下一条消息再扫
            if hi < max_cut and hi <= cache.search_to:
                return
        else:
            hi = nominal + w_after

        cut, confident = await loop.run_in_executor(
            None, finder.find, cache.chunks, lo, hi, nominal
        )

        if not confident and msg.source == 'file' and hi < max_cut:
            cache.search_to = hi
            logger.debug(f"切点吸附: [{lo:.1f}, {hi:.1f}]s 无可信断点，等待更多音频延长搜索")
            return
        if confident:
            logger.debug(f"切点吸附: 名义 {nominal}s，在 {cut:.2f}s 找到静音断点")
        else:
            logger.info(f"切点吸附: [{lo:.1f}, {hi:.1f}]s 内无可信静音断点，取最低分点 {cut:.2f}s 下刀")

        cache.search_to = 0.0
        _cut_and_submit(msg, cache, queue_in, socket_id, cut=cut, state=state)


def _cut_and_submit(msg: AudioMessage, cache: AudioCache, queue_in, socket_id: str, cut: float, state=None) -> None:
    """从缓冲区头部切出 [0, cut+overlap] 提交识别，缓冲区前移 cut 秒。"""
    n_stride = int(round(cut * AudioFormat.SAMPLE_RATE))
    n_segment = n_stride + int(round(msg.seg_overlap * AudioFormat.SAMPLE_RATE))
    stride_bytes = n_stride * AudioFormat.BYTES_PER_SAMPLE
    segment_bytes = n_segment * AudioFormat.BYTES_PER_SAMPLE

    segment_data = cache.chunks[:segment_bytes]
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
    queue_in.put(task)
    if state is not None:
        register_segment_submission(state, (socket_id, msg.task_id), time.monotonic())
    logger.debug(
        f"提交音频片段，任务ID: {msg.task_id}, 切点: {cut:.2f}s, "
        f"偏移: {cache.offset}s, 缓冲区: {len(cache.chunks)} bytes"
    )


async def message_handler(websocket, msg: AudioMessage, cache: AudioCache, app) -> None:
    """
    处理客户端发送的音频消息

    根据消息中的分段参数，将音频数据分段后提交到识别队列。
    """
    queue_in = app.state.queue_in

    global status_mic
    is_start = not bool(cache.chunks)
    socket_id = str(websocket.id)

    # 麦克风首次消息 → GPU 加速
    if is_start and msg.source == 'mic' and Config.gpu_boost_enabled:
        queue_in.put(Task(
            type='cmd',
            task_id='gpu_boost',
            data=b'', offset=0, overlap=0,
            socket_id=socket_id, is_final=False,
            time_start=0, time_submit=0,
            command='gpu_boost'
        ))

    try:
        # base64 解码音频数据（float32, 16kHz, mono）
        data = b64decode(msg.data, validate=True)
        cache.chunks += data
        cache.byte_count += len(data)

        if not msg.is_final:
            # 打印状态消息
            if msg.source == 'mic':
                status_mic.start()
            if msg.source == 'file' and is_start:
                console.print('正在接收音频文件...')
                logger.info(f"开始接收音频文件，任务ID: {msg.task_id}")

            # 若缓冲已达到分段阈值，将片段作为任务提交
            await _submit_segments(msg, cache, queue_in, socket_id, app.state)

        else:  # is_final
            # 打印状态消息
            if msg.source == 'mic':
                status_mic.stop()
            elif msg.source == 'file':
                print(f'音频文件接收完毕，时长 {cache.total_duration:.2f}s')
                logger.info(f"音频文件接收完毕，任务ID: {msg.task_id}, 时长: {cache.total_duration:.2f}s")

            # 提交最终片段
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
            queue_in.put(task)
            register_segment_submission(app.state, (socket_id, msg.task_id), time.monotonic())
            logger.debug(f"提交最终片段，任务ID: {msg.task_id}, 数据大小: {len(cache.chunks)} bytes")

            # 重置缓冲区
            cache.reset()

    except Exception as e:
        logger.error(f"音频数据处理错误，任务ID: {msg.task_id}: {e}", exc_info=True)
        raise


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
        async for raw_message in websocket:
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
                    transition_terminal(state, active, 'FAILED')
                if task_id and active != (socket_id, task_id):
                    malformed_key = (socket_id, task_id)
                    begin_task(state, malformed_key)
                    transition_terminal(state, malformed_key, 'FAILED')
                await queue_error_and_close(
                    state, websocket, socket_id, task_id, 'bad_request',
                    f"{type(e).__name__}: {e}", False,
                )
                return

            key = (socket_id, msg.task_id)
            active = state.connection_tasks.get(socket_id)
            if active is not None and active != key:
                transition_terminal(state, active, 'FAILED')
                begin_task(state, key)
                transition_terminal(state, key, 'FAILED')
                await queue_error_and_close(
                    state, websocket, socket_id, msg.task_id, 'task_conflict',
                    f"连接上任务 {active[1]} 尚未终结，不能开始任务 {msg.task_id}", False,
                )
                return

            if active is None:
                begin_task(state, key)
            record = state.tasks[key]
            if record.status in {'DONE', 'FAILED'}:
                logger.warning(f"丢弃终态任务 {msg.task_id} 的迟到上行帧")
                continue
            if msg.is_final and not set_task_draining(state, key):
                logger.warning(f"丢弃任务 {msg.task_id} 重复的 is_final 帧")
                continue

            try:
                await message_handler(websocket, msg, cache, app)
            except binascii.Error as e:
                transition_terminal(state, key, 'FAILED')
                await queue_error_and_close(
                    state, websocket, socket_id, msg.task_id, 'decode_failed',
                    f"{type(e).__name__}: {e}", False,
                )
                return
            except Exception as e:
                logger.error(f"连接 {socket_id} 处理任务异常", exc_info=True)
                transition_terminal(state, key, 'FAILED')
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
            transition_terminal(state, active, 'FAILED')
            await queue_error_and_close(
                state, websocket, socket_id, active[1], 'internal',
                f"{type(e).__name__}: {e}", True,
            )
    finally:
        # 清理资源
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
                await sender
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
            payload = await outbound.get()
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
    string_fields = ('task_id', 'source', 'data', 'context', 'language')
    for name in string_fields:
        if name in data and not isinstance(data[name], str):
            raise TypeError(f"字段 {name} 必须是字符串")
    if type(data['is_final']) is not bool:
        raise TypeError("字段 is_final 必须是布尔值")
    if data['source'] not in {'mic', 'file'}:
        raise ValueError("字段 source 只能是 mic 或 file")
    number_fields = ('time_start', 'seg_duration', 'seg_overlap')
    for name in number_fields:
        if name in data and (isinstance(data[name], bool) or not isinstance(data[name], (int, float))):
            raise TypeError(f"字段 {name} 必须是数字")
