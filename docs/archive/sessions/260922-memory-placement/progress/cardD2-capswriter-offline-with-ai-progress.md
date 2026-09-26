# CardD2 CapsWriter-Offline-with-AI 进展

## 里程碑 1：3 条仓专属记忆迁移与索引生成完成

- 当前阶段：实现完成，进入验收。
- 本段结论：3 条条目已从 agent-config 原样迁入本仓 `memory/`，并由 agent-config 主 clone 的真实 `build_index.py` 生成 `INDEX.md`。生成命令指向本仓 canonical 主 clone 路径，未指向执行器 worktree 或 runtime release。
- 关键决策与已否决方案：保留条目正文与 frontmatter 逐字节不变；未复制 `scripts/memory/`、未新增 `README.md`、未手写索引。因生成器按传入目录写入动态重建路径，使用同仓主 clone `memory/` 做临时真实生成后清理临时文件，再将生成物复制到执行器 worktree。
- 下一步唯一动作：运行全部行为验收与仓测试，随后提交本里程碑。

## 里程碑 2：行为验收与仓测试完成

- 当前阶段：验收完成，准备交付。
- 本段结论：3 条逐字节哈希、索引 `--check`、条目计数、pickup 读取条件和路径防悬空检查全部通过；仓测试为 `102 passed, 3 skipped`。全部产物已提交到执行器分支。
- 关键决策与已否决方案：3 个测试 skip 因本机缺少 ForceAligner/Silero 模型或后端，按任务要求如实保留为 skip，未伪造为 pass；探针异常与主 clone 临时 staging 清理情况写入交付报告。
- 下一步唯一动作：写入完整 `report.md` 并推送当前分支。
