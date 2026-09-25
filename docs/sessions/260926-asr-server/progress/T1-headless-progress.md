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
