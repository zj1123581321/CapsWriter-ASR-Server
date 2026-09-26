# quality-entry-progress

## 2026-09-26 契约绿

- 当前阶段：implementing；有界契约（替身 uv argv/非零传播）已绿。
- 本段结论：`scripts/gate-quality` 用 uv 跑完整 `tests/`，依赖对齐 CI 的 websockets==15.0.1 腿；不改 ci.yml。
- 关键决策及否决：否决根 pyproject、step env 当隔离、本机 sudo 装 ffmpeg、新增 broker。平台未落地，不宣称接入。
- 下一步：完整 `bash scripts/gate-quality` 一次 + `git diff --check`；失败则对照 base，不修范围外。

## 2026-09-26 完整入口

- 当前阶段：implementing 收尾；完整 `bash scripts/gate-quality` 退出 0。
- 本段结论：228 passed, 3 skipped, 81 warnings, 161.94s；`git diff --check` 0。既有 skip 未改。
- 关键决策及否决：不修范围外 skip；不宣称平台接入。ffmpeg 本机已有，未 sudo 安装。
- 下一步：draft PR；caller/secret/迁仓仍待后续卡。

## 2026-09-26 P2

- 当前阶段：三项 P2 定点契约 4 passed。
- 本段结论：受控最小 env；替身只记 argv/PATH；缺 ffmpeg/uv 字面错误；设计链接 gate-hub#1136、gate#249、gate-hub#1134。完整入口 230 passed, 3 skipped, 81 warnings, 98.90s；`git diff --check` 0。
- 关键决策及否决：不序列化完整 environ；路径隔离替身，不依赖系统缺包；不改 ci.yml。
- 下一步：更新 draft PR30，交主脑复审；caller/secret/迁仓仍待后续卡。

## 2026-09-26 工作目录契约修正

- 当前阶段：定点契约 4 passed；注入错误 cwd 后仓根断言变红。
- 本段结论：受控 PATH 提供真实 `dirname`；替身 uv 记录 cwd，成功与非零传播用例断言仓根；同入口依赖运行 4 passed。仅 pytest 依赖命令被 conftest 缺 `numpy` 挡住（退出 4）。
- 关键决策及否决：此前默认 `git diff --check` 面对 clean 树的空 diff，不能证明已提交范围；本轮检查 base..HEAD，不改历史测试数字。
- 下一步：检查 base..HEAD whitespace，commit/push 并读回 draft PR30 与远端 tip。
