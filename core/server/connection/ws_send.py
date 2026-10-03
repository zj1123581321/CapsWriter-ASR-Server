import asyncio
import queue
from ..schema import Result
from ..state import (
    acknowledge_segment_result,
    ensure_server_runtime,
    make_task_key,
    task_key_from_result,
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
    if task_id:
        transition_terminal(
            state,
            make_task_key('ws', task_id, socket_id),
            'FAILED',
            code=code,
        )
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
    """给全部活动任务排 error 并等待各连接冲刷/关闭。

    HTTP 任务没有连接可刷：必须先经 finalize_http_job 可靠落库 FAILED，
    落库成功后才释放 owner/唤醒等待者；落库超时或失败不释放 owner，
    由调用方按原语义非零退出，重启收敛兜底。
    """
    ensure_server_runtime(state)
    from ..http_file_runner import finalize_http_job
    closing = []
    for key, record in list(state.tasks.items()):
        if key == skip_key or record.status in {'DONE', 'FAILED'}:
            continue
        if key[0] == 'http':
            await finalize_http_job(state, key, 'FAILED', code, message)
            continue
        websocket = state.sockets.get(key[1])
        if websocket is None:
            transition_terminal(state, key, 'FAILED')
            continue
        closing.append(schedule_error_close(
            state, websocket, key[1], key[2], code, message, True
        ))
    if closing:
        await asyncio.wait_for(asyncio.gather(*closing), timeout=15)


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
        try:
            result: Result = await to_thread(queue_out.get, True, 1)
        except queue.Empty:
            # 空队列是正常空闲态；限时轮询可让停止时的工作线程及时返回。
            continue
        if result is None:
            logger.info("收到退出通知，停止结果分发任务")
            return
        # 测试骨架独立启动 sender 时，worker 的就绪信号可能仍在队列中。
        if result is True:
            continue

        key = task_key_from_result(result)
        record = state.tasks.get(key)
        if record is None:
            # HTTP 任务在结果持久化（或可靠 FAILED）之后就会释放运行态记录，
            # 因此无记录的 HTTP 结果按契约属于迟到结果：记账丢弃，不覆盖终态。
            logger.debug(
                f"丢弃终态或无主迟到结果 task={result.task_id} "
                f"socket={result.socket_id} owner_kind={result.owner_kind}"
            )
            continue
        if record.status in {'DONE', 'FAILED'}:
            logger.debug(
                f"丢弃终态或无主迟到结果 task={result.task_id} "
                f"socket={result.socket_id}"
            )
            continue

        if result.owner_kind == 'http':
            sink = state.http_result_sink
            if sink is None:
                raise RuntimeError('HTTP 结果持久消费者未注入')
            # HTTP 终态唯一收尾人是结果 sink（落库→转换→释放一体完成）；
            # 本分支只做段确认，不得再做任何终态转换——重复转换与
            # 两套终态责任并存的旧形态由结构约束钉死。
            await sink(result)
            acknowledge_segment_result(state, key)
            logger.debug(f"已提交 HTTP 识别结果 task={result.task_id}")
            continue

        acknowledge_segment_result(state, key)
        websocket = state.sockets.get(key[1])
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
