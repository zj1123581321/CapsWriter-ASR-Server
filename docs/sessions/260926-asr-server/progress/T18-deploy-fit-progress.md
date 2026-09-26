## 2026-09-26：部署脚本适配与行为测试

- 当前阶段：脚本实现
- 本段结论：`update.sh` 与 `update.ps1` 改为使用指定解释器，不再建虚拟环境；checkout 后按模型检查 llama 库，并为旧 ref 保留跳过路径。`tests/test_deploy_scripts.py -v` 的 9 项测试通过；当前环境没有 `pwsh`，PowerShell 契约采用文本断言。
- 关键决策与已否决方案：保留 llama_build_info.py 的文本正则解析，不 import 仓库代码；paraformer 等不需 llama 的模型不做预检。
- 下一步唯一动作：完成部署文档、主机模板与待办更新。

## 2026-09-26：生产布局文档与验收

- 当前阶段：文档与验收
- 本段结论：README、主机模板和 TODO 已补齐三机实际解释器、llama 库、旧 ref 回滚及 GGUF 复读现场。定向用例 9 项通过，整仓 218 项通过、3 项跳过；关闭 llama 缺库判断后对应测试以 `AssertionError` 转红。
- 关键决策与已否决方案：update.sh 也按 llama 加载器在 Linux 的 `.so` 文件规则预检；Windows 无 pwsh，按卡面做脚本文本断言。
- 下一步唯一动作：写交付报告并创建 draft PR。

## 2026-09-26：T18 r2 反馈修正
- 当前阶段：实现与本地验证
- 本段结论：llama 预检改为读取已校验 ref 的 Git 对象并在 checkout 前执行；缺库用例确认 HEAD 不变。定向 9 项和整仓 218 项通过，另有 3 项跳过。
- 关键决策与已否决方案：先 rev-parse 校验 ref，再用 cat-file 判断目标版本文件是否存在；预检所需动态库仍从忽略的工作树目录读取。
- 下一步唯一动作：提交本轮脚本、测试、README 和进度修正。
