# T1 服务端无头化——进度存档

## 里程碑 1：端口占用 / 模型缺失 fail fast

- **当前阶段**：实现
- **本段结论**：`server_manager.py` 端口自检失败由 `input()+return` 改为 `raise SystemExit(1)`（错误日志已由 `_check_port` 打印）；`check_model.py` 两处 `input('按回车退出')` 删除，保留 `sys.exit(1)`。SystemExit 不被 `app.start()` 的 `except RuntimeError` 捕获，可直通解释器以退出码 1 结束。
- **关键决策与已否决方案**：否决「改成可配置的 input/直接退出双模式」——无头守护是硬需求，交互路径一律删除（反过度设计）。
- **下一步唯一动作**：app.py 注册 SIGTERM/SIGINT 一次触发 stop()。

## 里程碑 2：SIGTERM/SIGINT 一次触发清理退出

- **当前阶段**：实现
- **本段结论**：`app.py` 移除对 `core.tools.signal_handler.register_signal` 的依赖（客户端双击 Ctrl+C 行为不受影响），新增 `_register_exit_signals()`：SIGINT 与 SIGTERM 任一收到一次即调用 `self.stop()`；Windows 以 `sys.platform != 'win32' and hasattr(signal, 'SIGTERM')` 判定只注册 SIGINT。退出码 0 的通路依赖既有 `stop()` 末尾 `loop.stop()` → `run_until_complete` 抛 RuntimeError → 既有 `except RuntimeError: pass` 捕获后正常收尾。
- **关键决策与已否决方案**：否决复用 `core.tools.signal_handler` 加参数扩展——卡面明确服务端可不再使用它，且其「1 秒内两次」语义与无头需求相反。
- **下一步唯一动作**：托盘导入链惰性化。

## 里程碑 3：托盘导入链惰性化

- **当前阶段**：实现
- **本段结论**：`core/server/ui/__init__.py` 移除顶层 `from core.ui.tray import enable_min_to_tray`（该导入会连带 toast/tray→tkinter/pystray），改为仅说明惰性导入策略的文档字符串；唯一消费方 `tray_manager.py` 改为在 `enable_tray` 为真时于 `start()` 内 `from core.ui.tray import enable_min_to_tray` 惰性导入。`stop()` 原本已守卫+惰性，未动。
- **关键决策与已否决方案**：否决「模块级 `__getattr__` 惰性导出」——唯一消费方就一处，直接改导入点更简单（反熵：新增抽象说不出第二个消费者）。
- **下一步唯一动作**：logger 默认目录去客户端依赖 + CW_LOG_LEVEL。

## 里程碑 4：logger 去 config_client 依赖 + CW_LOG_LEVEL

- **当前阶段**：实现
- **本段结论**：`core/logger.py` 默认日志目录改为由 `logger.py` 自身 `__file__` 推导仓库根下 `logs/`（已核实 `config_server.BASE_DIR` 就是仓库根，行为等价）；`core/server/__init__.py` 在调用 `setup_logger` 处读 `CW_LOG_LEVEL`（未设置或为空沿用 `Config.log_level`），未动 `config_server.py`。初版注释里含 `config_client` 字样，卡面验收要求 grep 无输出，已改措辞。
- **关键决策与已否决方案**：无
- **下一步唯一动作**：提交无头化行为验收测试。
