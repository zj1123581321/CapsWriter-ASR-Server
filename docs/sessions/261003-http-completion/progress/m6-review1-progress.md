# M6 review1 progress

固定范围：`5134720e058e0e9ae3d3feddebf4942f8bf7ed7a..52a748cc60cd73ecfe18a36f7dde0879e77d13f9`。

## 四项进度

1. **代码首读与初步结论：完成。** 先读固定 base 的 `qa.md`、`design.md` 和新增 E2E 测试及必要 SDK/runner/worker/WS producer 路径；新版 QA/evidence 尚未阅读前已提交首读产物 `87571e5`。
2. **新版 QA/evidence 索引独立核对：完成。** 12 组逐项对照代码和 test index，补充了每组证据及未知；发现组 10 不校验 worker 实收 PCM 样本内容。没有把实现方记录的红绿当成本轮执行证据。
3. **运行与约束力验证：完成。** 新增文件裸 shell 连跑 5 轮，每轮 `6 passed`、无 skip；必要旧 SDK/CLI/runner/supervision/cancel/cleanup 选择 `32 passed`。三项独立有效目标 AssertionError 逆变异为 SDK commit 自动重发、去掉重采样参数、owner recorder 记录错误；同长度全零 PCM 变异仍有 `2 passed`，确认 P2。脚本与日志留在 `/tmp/m6-review1-*`。
4. **定稿与远端核验：进行中。** 最终 verdict 为 `p2-only`，只有 M6R1-1；OCR 是 `skipped / no_reviewable_items`。真实 systemd 白名单单元也以 `6 passed` 完成，unit/PID/cgroup 和真实未知已记录。当前更新 verdict/progress 后将做指定 diff 检查、提交、push 并读取实际远端 ref。

## 现场与验证环境

- 独立 worktree，分支 `card/http-m6-review1-261003`；审查对象固定为派卡给定 H0，不追随后续提交。
- Pickup：无匹配交接单；open issues 没有本卡新测试的直接修复认领。孤儿摘要为 `orphan 0 owned 0 unattributable 0 too-new 0 recent-7d 0 stale-over-7d 0 missing_ledger_repos 0`。memory 探针报 `memory_dir_mismatch`，恢复脚本未在本轮执行。
- Python 3.12.3、ffmpeg `/usr/bin/ffmpeg`；测试用隔离 `uv` 环境及卡面固定依赖，没有污染 main venv。systemd 单元 `codex-m6review1-systemdprobe-261004-1056421.service`，probe PID `1056442`，cgroup `/user.slice/user-1000.slice/user@1000.service/app.slice/codex-m6review1-systemdprobe-261004-1056421.service`，unit 自然成功退出。第一次探针因 `uv` 前置环境 bin 目录而对 PATH 逐字相等检查失败；调整为检查原白名单 PATH 后缀仍存在后，最终探针通过。失败的是探针判据，不是 QA 用例。
- OCR 前置 JSON 状态为 `skipped / no_reviewable_items`，未视作 clean。基线 `gh api request failed`，继承红未能判定。
