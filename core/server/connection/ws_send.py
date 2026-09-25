import asyncio
from ..schema import Result
from ..state import (
    acknowledge_segment_result,
    ensure_server_runtime,
    transition_terminal,
)
from core.protocol import ErrorMessage, RecognitionMessage
from core.tools.asyncio_to_thread import to_thread
from .. import logger


async def _drain_then_close(outbound, websocket, code: str) -> None:
    try:
        await asyncio.wait_for(outbound.join(), timeout=5)
    except TimeoutError:
        logger.warning(f"出站队列冲刷超时，关闭连接 code={code}")
    try:
        await websocket.close(code=4000, reason=code)
    except Exception:
        logger.debug(f"连接关闭时 socket 已不可用 code={code}", exc_info=True)


async def _close_now(websocket, code: str) -> None:
    try:
        await websocket.close(code=4000, reason=code)
    except Exception:
        logger.debug(f"连接立即关闭时 socket 已不可用 code={code}", exc_info=True)


def schedule_error_close(
    state,
    websocket,
    socket_id: str,
    task_id: str,
    code: str,
    message: str,
    retryable: bool,
) -> asyncio.Task:
    """将 error 放入单连接队列，并安排队列冲刷后关闭连接。"""
    ensure_server_runtime(state)
    transition_terminal(state, (socket_id, task_id), 'FAILED')
    outbound = state.out_queues.get(socket_id)
    if outbound is None:
        logger.debug(f"连接 {socket_id} 无出站队列，直接关闭 code={code}")
        return asyncio.create_task(_close_now(websocket, code))

    error = ErrorMessage(task_id, code, message, retryable).to_json()
    try:
        outbound.put_nowait(error)
    except asyncio.QueueFull:
        logger.warning(f"连接 {socket_id} 出站队列已满 code={code}")
        return asyncio.create_task(_close_now(websocket, code))
    return asyncio.create_task(_drain_then_close(outbound, websocket, code))


async def queue_error_and_close(
    state,
    websocket,
    socket_id: str,
    task_id: str,
    code: str,
    message: str,
    retryable: bool,
) -> None:
    close_task = schedule_error_close(
        state, websocket, socket_id, task_id, code, message, retryable
    )
    await close_task


async def fail_active_tasks(state, code: str, message: str, *, skip_key=None) -> None:
    """给全部活动任务排 error 并等待各连接冲刷/关闭。"""
    ensure_server_runtime(state)
    closing = []
    for key, record in list(state.tasks.items()):
        if key == skip_key or record.status in {'DONE', 'FAILED'}:
            continue
        websocket = state.sockets.get(key[0])
        if websocket is None:
            transition_terminal(state, key, 'FAILED')
            continue
        closing.append(schedule_error_close(
            state, websocket, key[0], key[1], code, message, True
        ))
    if closing:
        await asyncio.gather(*closing)


def _result_json(result: Result) -> str:
    message = RecognitionMessage(
        task_id=result.task_id,
        is_final=result.is_final,
        duration=result.duration,
        time_start=result.time_start,
        time_submit=result.time_submit,
        time_complete=result.time_complete,
        text=result.text,
        text_accu=result.text_accu,
        tokens=result.tokens,
        timestamps=result.timestamps,
    )
    return message.to_json()


async def ws_send(app):
    state = app.state
    queue_out = state.queue_out
    ensure_server_runtime(state)
    logger.info("WebSocket 结果分发任务已启动")

    while True:
        result: Result = await to_thread(queue_out.get)
        if result is None:
            logger.info("收到退出通知，停止结果分发任务")
            return
        # 测试骨架独立启动 sender 时，worker 的就绪信号可能仍在队列中。
        if result is True:
            continue

        key = (result.socket_id, result.task_id)
        record = state.tasks.get(key)
        if record is None or record.status in {'DONE', 'FAILED'}:
            logger.debug(f"丢弃终态或无主迟到结果 task={result.task_id} socket={result.socket_id}")
            continue

        acknowledge_segment_result(state, key)
        websocket = state.sockets.get(result.socket_id)
        outbound = state.out_queues.get(result.socket_id)
        if websocket is None or outbound is None:
            logger.debug(f"客户端 {result.socket_id} 已断开，丢弃结果 task={result.task_id}")
            transition_terminal(state, key, 'FAILED')
            continue

        if result.error_code:
            schedule_error_close(
                state,
                websocket,
                result.socket_id,
                result.task_id,
                result.error_code,
                result.error_message,
                result.error_code in {'inference_failed', 'inference_timeout', 'internal', 'slow_consumer'},
            )
            continue

        try:
            outbound.put_nowait(_result_json(result))
        except asyncio.QueueFull:
            schedule_error_close(
                state,
                websocket,
                result.socket_id,
                result.task_id,
                'slow_consumer',
                '客户端出站队列已满，服务端关闭该连接',
                True,
            )
            continue

        if result.is_final:
            transition_terminal(state, key, 'DONE')
        logger.debug(f"已分发识别结果 task={result.task_id} socket={result.socket_id}")
