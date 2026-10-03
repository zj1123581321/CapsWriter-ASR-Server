# M6 review1 progress

固定范围：`5134720e058e0e9ae3d3feddebf4942f8bf7ed7a..52a748cc60cd73ecfe18a36f7dde0879e77d13f9`。

## 四项进度

1. **代码首读与初步结论：完成。** 先读 base 版本 `qa.md`、`design.md` 和新增 E2E 测试及必要 SDK/runner/worker/WS producer 路径；未先读新版 QA/evidence。代码初步未确认 finding，详细边界见 verdict。本阶段首个 review 产物提交在前。
2. **新版 QA/evidence 索引独立核对：进行中。** 12 组逐项映射 producer 路径、实际测试与不足；不采用作者报告或红绿结论作为证据。
3. **运行与约束力验证：未开始。** 待执行 6 个新增用例至少 5 轮、必要旧 SDK/CLI/runner 用例、3 个最小反向变异、裸 shell 与 systemd --user 白名单环境。
4. **定稿与远端核验：未开始。** 待判 finding severity 与 failure-visibility，提交最终 verdict/progress，推送后读取实际远端 ref 并核验 clean。

## 现场与前置

- worktree：独立树，分支 `card/http-m6-review1-261003`，固定 HEAD `52a748cc60cd73ecfe18a36f7dde0879e77d13f9`；起始工作区干净。当前 dispatch id 的 unit 属于本卡执行现场。
- pickup：无匹配交接单；仓库 open issues 列表中未发现与本卡新测试直接相关的修复认领项。巡检行：`summary: orphan 0 owned 0 unattributable 0 too-new 0 recent-7d 0 stale-over-7d 0 missing_ledger_repos 0`。memory 探针原文：`memory 巡检报告不可用：memory_dir_mismatch（/home/zlx/.local/state/memory-doctor/latest.json）`；恢复入口为 `/home/zlx/projects/personal/agent-config/scripts/memory/memory_doctor_run.sh /home/zlx/projects/personal/agent-config`。
- OCR 前置 envelope 为 `skipped / no_reviewable_items`，未计为已审干净。
- review-discipline 指定环境依赖探针尚未执行；review-discipline 来源为 `/home/zlx/.local/lib/agent-config-runtime/releases/1850ecbba91d7065c1b82380993d19b8163bdcb1`。
