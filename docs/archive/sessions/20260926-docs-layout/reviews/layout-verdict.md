# 文档目录迁移独立审查结论

## 审查范围

- Risk tier：internal。
- Base：`cbbbdb22c03298d91f7ebaae2faa9ef678d230c4`。
- 固定审查 H0：`2fd7fe0789d72bf6e304e8b5f8b9e2cb355f88ba`。
- 对象：仅 `base..H0`；审查规格为该 H0 中的 `docs/development/designs/documentation-layout.md`。
- 结论：1 项 P2、0 项 P1。发现属于新算法说明的边界文字与现行整数阈值不完全一致。

failure-visibility: p2-only

## Findings

### P2-1：文本拼接窗口边界的比例表述与实现不一致

**违反规格：** 用户验收不变式 4（`merge_by_text` 新说明准确反映 `text_merger.py`）。

**位置与证据：** [`docs/development/algorithms/text-merge.md:16`] 描述匹配块终点必须严格超过前文窗口的四分之三位置。实现 [`core/server/merger/text_merger.py:90`] 计算 `tail_end_threshold = len(tail) // 4 * 3`，并在第 95 行检查终点 `a + size > tail_end_threshold`。窗口长度为 99 时，实现阈值是 72，因此终点 73 可成为候选；数学上的四分之三位置是 74.25。两种表述在非 4 的倍数窗口下不同。

**影响与定级：** 这是开发者依据说明推导边界时会遇到的事实偏差；运行时行为没有被本次改动改变。定为 P2，不构成静默运行故障。

## 已核验的不变式

- 迁移表映射了 56/56 篇基线 tracked 文档，32/32 篇 sessions 均位于归档目标路径。旧 sessions、继承的 `CHANGELOG.md` 和旧文本拼接算法文档共 34 对的 Git blob 完全一致。
- 根 README 和 `docs/README.md` 直接提供首次部署、SDK 与 WebSocket 协议入口；SDK 和部署权威页面仍留在原目录。导航、guides、reference、development、maintainers、archive 分类与设计规格一致。
- H0 中 26 篇活动 Markdown 的 103 个相对链接目标均存在。代码/测试差异仅更新文档路径注释或文档读取路径，没有改动应用执行逻辑、协议字段或依赖。
- 下游接入说明将字段、末帧规则和错误码指向协议权威页；GPU 参数、默认值、Paraformer CPU 行为、MLX 例外和 GPU 预加速默认值与 `config_server.py` 及引擎读取位置一致。
- `CHANGELOG.md` 未添加发布版本或日期；其 proxy 路由、状态页和诊断说明与当前 `core/proxy/router.py`、`core/proxy/proxy_server.py` 相符。旧混合更新日志完整归档。
- 旧算法说明在归档页明确标为历史实现；现行说明区分文本合并与 token/时间戳合并，并移除了旧 20 字窗口及 `ERROR_TOLERANCE` 对当前行为的暗示。

## 验证与盲区

- 迁移核验脚本：`/tmp/capswriter-docs-layout-migration-audit.py`；结果为 56 篇全映射、0 个缺失目标、0 个未映射源文件、0 个被改写的历史 blob。
- 相对链接核验脚本：`/tmp/capswriter-docs-layout-link-audit.py`；结果为 103 个目标全部存在。该静态核验检查文件/目录目标，不验证 Markdown 标题锚点。
- `git diff --check base H0` 通过。没有运行应用测试套件；本卡只要求文档审查，`Verify-Command` 仅检查本 verdict 文件存在。
- `ocr-review` 状态为 `reviewed_fallback`，主模型额度耗尽后由 DeepSeek 备链完成，`coverage=complete`、`findings=[]`；该工具自身的 verifier 标记为 `skipped`，不视作额外验证，也未据此缩小人工审查范围。
- 当前 review worktree 中 `git ls-files --others --ignored --exclude-standard` 返回 0 项，因此卡片所述 9 份本机 ignored 研究文件不在此 worktree，无法从本树独立检查其本地状态；它们不在冻结 diff 中，本次没有读取或改动它们。
