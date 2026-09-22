# CardD2 CapsWriter-Offline-with-AI 进展

## 里程碑 1：3 条仓专属记忆迁移与索引生成完成

- 当前阶段：实现完成，进入验收。
- 本段结论：3 条条目已从 agent-config 原样迁入本仓 `memory/`，并由 agent-config 主 clone 的真实 `build_index.py` 生成 `INDEX.md`。生成命令指向本仓 canonical 主 clone 路径，未指向执行器 worktree 或 runtime release。
- 关键决策与已否决方案：保留条目正文与 frontmatter 逐字节不变；未复制 `scripts/memory/`、未新增 `README.md`、未手写索引。因生成器按传入目录写入动态重建路径，使用同仓主 clone `memory/` 做临时真实生成后清理临时文件，再将生成物复制到执行器 worktree。
- 下一步唯一动作：运行全部行为验收与仓测试，随后提交本里程碑。
