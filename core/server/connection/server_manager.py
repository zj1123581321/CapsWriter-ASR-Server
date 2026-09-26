# coding: utf-8
"""
WebSocket 管理器 (SocketManager)

负责维护 ASR 服务器的异步通讯层，包括 WebSocket Server 的生命周期管理、
心跳监控、数据发送任务的编排。
"""

import asyncio
import functools
import websockets
from config_server import ServerConfig as Config
from .ws_recv import ws_recv
from .ws_send import ws_send
from .health import process_request
from .. import logger # Server module logger


class SocketManager:
    """
    WebSocket 网络管理器
    
    负责拉起并维护 WebSocket Server 以及识别结果的异步发送任务。
    """
    def __init__(self, app):
        self.app = app
        self._is_running = False
        self._server = None  # websockets.serve 返回的 server 对象

    async def start(self):
        """
        启动 WebSocket 网络服务
        """
        if self._is_running: return
        
        self._is_running = True

        loop = self.app.loop
        
        # 1. 优化守护线程执行器 (防止阻塞事件循环)
        from core.tools.daemon_executor import SimpleDaemonExecutor
        loop.set_default_executor(SimpleDaemonExecutor())

        # 2. 准备连接处理器 (注入 app 引用)
        handler = functools.partial(ws_recv, app=self.app)

        # 3. 启动服务
        logger.info(f"正在拉起 WebSocket 服务 (监听: {Config.addr}:{Config.port})")
        
        serve = websockets.serve(
            handler,
            Config.addr,
            Config.port,
            max_size=None,
            process_request=functools.partial(process_request, app=self.app),
            # 禁用 keepalive ping：超长音频上传/识别期间，客户端忙于连续发送大帧，
            # pong 无法在默认 20s 内送达，服务端会误判超时并以 1011 断连
            # （与 core/proxy/proxy_server.py 的 serve 保持一致）
            ping_interval=None,
        )
        try:
            server = await serve
        except OSError as exc:
            logger.error(f"端口被占用：{Config.addr}:{Config.port}，监听异常：{exc}")
            raise SystemExit(1)

        async with server:
            self._server = server  # 保存 server 引用，用于外部关闭

            # sender 与 worker 看门狗并行；任一异常结束都让服务端主循环退出。
            logger.info("WebSocket 发送协程与推理进程看门狗已就绪")
            sender_task = asyncio.create_task(ws_send(self.app))
            monitor_task = asyncio.create_task(self.app.process_manager.monitor())
            tasks = {sender_task, monitor_task}
            try:
                done, _ = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    await task
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
            
        self._is_running = False
        logger.info("SocketManager: WebSocket 服务已退出")

    def stop(self):
        """停止 WebSocket 网络服务"""
        # 主动关闭 WebSocket 服务器，让 ws_send 的 await 尽快返回
        if self._server:
            self._server.close()
        self._is_running = False
