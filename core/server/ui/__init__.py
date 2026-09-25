# coding: utf-8
"""
服务端 UI 子包

托盘功能默认关闭（config_server.ServerConfig.enable_tray = False）。
`core.ui`（会连带 tkinter / pystray）只在 TrayManager.start()/stop() 内
按需惰性导入，保证无头部署（pm2 / systemd / Windows 计划任务）下
`import core.server` 的导入链不触碰任何 GUI 依赖。
"""
