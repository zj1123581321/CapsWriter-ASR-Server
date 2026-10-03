# #55 配置探针与 M3 完成记录独立审查

failure-visibility: clean

- **审查结论**：PASS，无 finding。
- **风险等级**：internal。
- **固定对象**：`e066930ef38aabe8e5051c9256463646f62a7186..6adeba5b39409964ca11634ed2ba1760a4fde56c`；本轮不追分支后续提交。
- **覆盖范围**：固定 diff 的三个文件：`goals/http-core/M3-runner.md`、`goals/http-core/M4-resources.md`、`tests/test_http_config.py`；对照设计/QA 规格、必要调用方及 GitHub 实际 CI 状态。

## 审查结论与不变式

未发现违反本卡不变式的改动。配置探针仍通过真实子进程执行，父进程只将 `PATH`、隔离 `HOME`、`LANG`、动态构造的 `PYTHONPATH` 与显式覆盖项交给子进程；不继承整个环境。新增环境断言在消费端检查子进程实际观察到的 argv、解释器、cwd、HOME、PYTHONPATH、导入位置和环境键。实际 user-site 试跑中，HOME 改为临时目录后，子进程仍导入与父进程同一 `websockets` 文件；依赖路径由父解释器 `sys.path` 推导，没有固化机器路径。

`_probe` 与 `_idle_probe` 均调用共享的 `_probe_env`，该 helper 有两个现存消费者。`HttpServer` 仍被真实导入，默认 300 秒和显式 12 秒均到达 `HttpServer._read_idle_seconds()`。在隔离 scratch worktree 将 reader 分别改成错误的 999 秒、以及只对显式值返回 999 秒后，测试分别在 `tests/test_http_config.py:143` 与 `:148` 以 `AssertionError` 失败；没有以 ImportError 或缺依赖作为红验结果。

M3 记录把 `status: 已完成`、`merged_pr: 51` 与 PR #51 合并提交 `67b8b65247c9f5310d5c8346cf579416e19851fc` 对齐，并在 `goals/http-core/M3-runner.md:30` 记载 R7 随 PR #58 / `e066930ef38aabe8e5051c9256463646f62a7186` 合入的容量口径。M4 在 `goals/http-core/M4-resources.md:5,10,18` 只标为进行中、`merged_pr: null`；文字明确激活只解除前置阻塞，不代表资源实现完成。该边界符合规格。`GOALS.md` 的派生索引更新属主脑消费方待办，按本卡排除，不记为应用缺陷。

## Findings 与 P1 两问

- **Findings：无。** 因无 finding，不存在需要登记的位置、触发场景或规格违反项。
- **P1 问一（真实使用方式下是否触发）：** 没有已证实的缺陷触发路径；真实 user-site 子进程与测试消费者均按预期运行。
- **P1 问二（触发后果能否接受）：** 问一未成立，没有 P1 候选后果需要分级。
- **Backlog：无新增项。**

## 熵增检查

新增 `_dependency_paths` 与 `_probe_env` 服务于本次已发生的子进程依赖不可见问题；`_probe_env` 被 `_probe`、`_idle_probe` 两处实际调用。依赖目录按当前解释器布局推导，解决临时 HOME 下 user-site 不可见问题，不新增生产抽象、状态、配置、固定机器路径、整环境继承、重试或 fallback。环境子进程只输出键名和白名单观察值，不输出父环境内容。没有发现需要减掉的新增机制。

## 验证与 CI 证据

- OCR 前置：`ocr-review` 存在并按固定 SHA 范围、本 worktree、`audience=agent`、并发 4、870 字节中性摘要调用；JSON `status=skipped`、`reason=no_reviewable_items`、`coverage=none`。本轮标记为**未扫过**，没有把 skipped 当作干净扫描；随后仍完成全量人工审查。
- 指定全文件测试：7 项通过。实际命令和 user-site 子进程核验记录在同目录 progress 文件及本次派发报告中。
- 反向 reader 注入：默认 300 与显式 12 两种错误值分别导致各自 reader 断言 `AssertionError`。
- PR #59 的固定 head `6adeba5`：两个 websockets 单元测试 job 均 `SUCCESS`；`gate / primary` 与 `gate / ocr` 为 `SKIPPED`（PR 仍为 draft），因此不能称为完整主审门禁通过。`gate / quality`、`gate / classify_pr_paths`、`gate / gate (draft)`、`gate / ledger` 为 `SUCCESS`。
- 历史事实核对：PR #51 已合并至 `67b8b65`，两个 websockets 单元 job、primary 与 OCR 均成功；PR #58 已合并至 `e066930`，两个单元 job 成功、primary 为 `FAILURE`、OCR 成功。派发卡记载主干基线查询不可用，故该历史红与基线的同作业/首失败步骤关系**未能判定**；PR #59 的 primary/OCR 为 skipped，不据此声称全门禁绿。远程 CI 最终处置交主脑消费。
- 真实系统 Python 3.12 user-site 布局：`site.ENABLE_USER_SITE=True`，子进程 HOME 为隔离 tmp，且父子 `websockets` 导入文件一致；裸解释器未安装 `aiohttp`，不据此判实现失败。指定 uv 环境装有 `aiohttp==3.14.3` 并完成整文件测试；没有移除真实 `HttpServer` 导入或用缺依赖 skip。

## 现场四问

- **踩到的坑：** pickup 简报脚本是 Bash，误用 `python3` 会报语法错误；改用 Bash 后确认当前工作区无交接单。OCR 虽可运行但返回 `no_reviewable_items`，按 skipped 处理。memory 巡检报告提示 `memory_dir_mismatch`，未用旧结果替代。
- **闸与绕过：** reader 变异只在 scratch-worktree 的 H0 临时树执行，并核实删除；固定工作树未改被审代码。未将 PR #59 draft 的绿色结果说成完整 gate。
- **与卡面的偏差：** 无范围偏差；未改生产、测试、goals 或 `GOALS.md`。user-site 裸解释器缺少 aiohttp，按卡面要求作为环境缺项记录，不判实现失败。
- **最贵的一步：** 在 uv 固定依赖环境跑完整测试，并在独立 H0 scratch worktree 对默认/显式 reader 分别做反向注入。
