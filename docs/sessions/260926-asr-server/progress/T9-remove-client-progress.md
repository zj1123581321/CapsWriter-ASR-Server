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

## 里程碑 3：仓库文档与剩余客户端文件清理
- 当前阶段：服务端定位改写完成，进入全量验证。
- 本段结论：删除 LLM 角色文件、客户端配置/入口、客户端依赖清单、双用途 PyInstaller 与 zip 打包文件、客户端专属工具和文档/图片；保留服务端部署与模型文档。可跟踪 `.py` 数为 358 → 254，AST 入口可达 179 个模块；README 为 35 行且三条要求链接均存在。无 tkinter 的 server/proxy 导入命令输出 `ok`。
- 关键决策与已否决方案：AST 仍列出 5 个 `core/server/engines/**` 非 export 文件不可达，但卡面禁止改动整个引擎目录，故原样保留并在报告列出；未达 Python 模块对应的 `core/tools/zhconv/zhcdict.json` 保留，因为该非代码文件不在 AST 删除规则内，删除它会使总代码行差超过 20,000 硬预算。`models/Ollama-Polish.py` 位于卡面清理范围之外，保留并报告。更新 `requirements-server*.txt` 去掉已无调用方的托盘依赖，属 Scope-Globs 外的必要偏差。
- 下一步唯一动作：运行卡面指定的完整 pytest 命令并完成末次静态/grep 验收。

## 里程碑 4：完整测试通过
- 当前阶段：实现与主验证完成。
- 本段结论：卡面 Verify-Command 输出 `213 passed, 3 skipped, 81 warnings in 57.81s`；无 tkinter 导入冒烟输出 `ok`。现存跳过项是需平台/模型资源的集成用例。
- 关键决策与已否决方案：不删除测试套件中的服务端无头测试与 SDK 测试；仓库内没有独立 Windows 桌面客户端测试文件。
- 下一步唯一动作：完成最终 grep/预算/状态取证，生成报告并推送 draft PR。

## 里程碑 5：最终残留扫描
- 当前阶段：收尾取证。
- 本段结论：grep 发现并删除了 `core/logger.py` 中一行引用旧客户端入口的注释；其余命中仅来自历史 CHANGELOG 和旧文档迁移台账。
- 关键决策与已否决方案：历史记录保留其当时的客户端词汇，不改写成当前仓库结构。
- 下一步唯一动作：提交这条清理并复核最终状态后推送 draft PR。
