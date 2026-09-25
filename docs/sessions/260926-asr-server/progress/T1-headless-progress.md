# T1 服务端无头化——进度存档

## 里程碑 1：端口占用 / 模型缺失 fail fast

- **当前阶段**：实现
- **本段结论**：`server_manager.py` 端口自检失败由 `input()+return` 改为 `raise SystemExit(1)`（错误日志已由 `_check_port` 打印）；`check_model.py` 两处 `input('按回车退出')` 删除，保留 `sys.exit(1)`。SystemExit 不被 `app.start()` 的 `except RuntimeError` 捕获，可直通解释器以退出码 1 结束。
- **关键决策与已否决方案**：否决「改成可配置的 input/直接退出双模式」——无头守护是硬需求，交互路径一律删除（反过度设计）。
- **下一步唯一动作**：app.py 注册 SIGTERM/SIGINT 一次触发 stop()。
