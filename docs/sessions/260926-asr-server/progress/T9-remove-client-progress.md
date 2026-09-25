# T9 移除客户端进度

## 里程碑 1：静态可达性分析器
- 当前阶段：清理判定工具已建立。
- 本段结论：新增 `scripts/_reachability.py`，以服务端、proxy、SDK、测试和验证脚本为根遍历 AST 导入；以卡面基线统计删除前仓内 `.py` 数。基线输出为 358 个文件，当前新增分析器后为 359 个。
- 关键决策与已否决方案：`core/server/engines/**` 属卡面明示保护区，即使分析列出不可达文件也不删除，收尾会单列说明；不以名称猜测客户端归属。
- 下一步唯一动作：移除服务端托盘集成并验证无 tkinter 导入链。

## 里程碑 2：服务端彻底无 UI 依赖
- 当前阶段：服务端生命周期清理已完成。
- 本段结论：删除 `core/client/**`、`core/ui/**`、`core/server/ui/**`，从 `config_server.py` 和 `core/server/app.py` 移除托盘配置与调用；无头测试通过（`7 passed`），子进程先将 `sys.modules['tkinter'] = None` 再导入 server、proxy 和 app。
- 关键决策与已否决方案：`scripts/_verify_file_transcribe.py` 仍是卡面入口集合中的服务端验证脚本，故移除其对客户端 `ResultHandler` 的引用，保留脚本自身生成 SRT 的既有逻辑；此改动超出精确 `Scope-Globs`，会在报告列为偏差。
- 下一步唯一动作：删除余下客户端配置与打包资产，并按可达性复核遗漏模块。
