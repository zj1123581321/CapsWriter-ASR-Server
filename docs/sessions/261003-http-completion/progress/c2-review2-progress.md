阶段：独立证据完成，正在整理最终 verdict/report 并提交推送。
结论：新增 `tests/test_http_cleanup.py` 全文件 5 passed；竞态窄测独立重复五轮为 5/5 passed。scratch 红验确认失效 `active` guard 后，指定周期集成 case 以原断言 `AssertionError: 周期清理在 decoder.close 释放前删除了仍被 runner 引用的源` 转红（1 failed），故断言能拦住已识别回归。真实 systemd probes 证实正常清理路径与本卡发现的运行时存储错误可见性/进程存活风险。
关键决策与否决：红验只在固定 H0 的临时 scratch worktree 改动一行，注入前后已核对；scratch 已由 helper 清理，审查 worktree 未改源码。OCR 状态 skipped，不算通过；完整仓库测试与 C1 precheck 不在本卡范围。审查结论含一项 P1：cleanup unlink 的存储异常会令 HTTP/WS listener 关闭并可见记录错误，但短进程/worker 仍存活、systemd unit 仍 active，进程级监督未被触发。
唯一下一步：写入逐不变式 verdict 与四问完整报告，按允许路径提交；push 固定分支后核实远端 tip 和 worktree clean。
