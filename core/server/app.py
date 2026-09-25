# coding: utf-8
"""
CapsWriter Offline 服务端主程序门面类 (Facade)

采用外观模式统一管理进程管理器 (ProcessManager) 和网络管理器 (SocketManager)。
该类是整个服务端应用的中心指挥部，负责初始化生命周期、托盘图标、
并协调子进程与 WebSocket 服务的启动与退出。
"""

import os
import sys
import signal
import asyncio
from pathlib import Path
from config_server import ServerConfig as Config, __version__
from .state import ServerState, console
from .worker.process_manager import ProcessManager
from .connection.server_manager import SocketManager
from .ui.tray_manager import TrayManager
from . import logger

class CapsWriterServer:
    """
    CapsWriter 服务端外观类
    
    管理的外部接口极其简洁：start()。
    """
    def __init__(self):
        # 确保正确的工作目录
        self.base_dir = Path(__file__).parents[2]
        os.chdir(self.base_dir)

        # 初始化事件循环
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        # 初始化状态容器
        self.state = ServerState(app=self)

        # 基本配置与组件实例化
        self.process_manager = ProcessManager(self)
        self.socket_manager = SocketManager(self)
        self.tray_manager = TrayManager(self)

        self.version = __version__
        self.is_alive = False


    def _print_banner(self):
        """打印启动信息"""
        console.line(2)
        console.rule('[bold #d55252]CapsWriter Offline Server[/]'); console.line()
        console.print(f'版本：[bold green]{self.version}[/]', end='\n\n')
        console.print(f'项目地址：[cyan underline]https://github.com/HaujetZhao/CapsWriter-Offline', end='\n\n')
        console.print(f'当前基文件夹：[cyan underline]{self.base_dir}[/]', end='\n\n')
        console.print(f'绑定的服务地址：[cyan underline]{Config.addr}:{Config.port}[/]', end='\n\n')

    def stop(self):
        """
        清理服务端资源
        """
        # 防连续触发
        if not self.is_alive: return
        self.is_alive = False 

        logger.info("=" * 50)
        logger.info("开始清理服务端资源...")

        self.state.queue_out.put(None)

        # 1. 关闭 WebSocket 服务（立即释放端口）
        self.socket_manager.stop()

        # 2. 终止识别子进程
        self.process_manager.stop()

        # 3. 停止托盘图标
        self.tray_manager.stop()

        # 4. 最后停止协程（需在其他资源释放之后）
        self.loop.stop()

        logger.info("服务端资源清理完成")
        console.print('[green4]再见！')


    def _register_exit_signals(self):
        """
        注册退出信号处理（服务端专用，不用 core.tools.signal_handler）

        无头守护（pm2 / systemd / Windows 计划任务）下没有「第二次按键」的
        交互机会，SIGINT / SIGTERM 任一收到一次即触发 stop() 清理（停子进程、
        关 loop），让进程以退出码 0 结束。Windows 无法投递 SIGTERM，仅注册
        SIGINT（hasattr + 平台判断，不写 try-except 吞错）。
        """
        def _stop_on_signal(signum, _frame):
            logger.info(f"收到 {signal.Signals(signum).name}，开始清理退出")
            self.stop()

        signal.signal(signal.SIGINT, _stop_on_signal)
        if sys.platform != 'win32' and hasattr(signal, 'SIGTERM'):
            signal.signal(signal.SIGTERM, _stop_on_signal)

    def start(self):
        """
        同步启动服务端 (主入口)

        注册信号处理、拉起子进程并进入网络服务监听循环。
        """
        # 防连续触发
        if self.is_alive: return
        self.is_alive = True

        # 注册退出信号处理
        self._register_exit_signals()

        # 托盘图标
        self.tray_manager.start()
        self._print_banner()

        # 拉起识别子进程
        self.process_manager.start()
        
        # 开启网络服务监听 (接管当前线程直至退出)
        try:
            self.loop.run_until_complete(self.socket_manager.start()) 
        except RuntimeError:
            pass