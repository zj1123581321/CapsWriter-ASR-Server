# GitHub fork 本机归档摘要

- 采集时间：2026-09-26 UTC。
- 原仓库：`zj1123581321/CapsWriter-ASR-Server`（GitHub ID `971940657`，public fork，父仓库 `HaujetZhao/CapsWriter-Offline`，ID `646394328`，默认分支 `master`）。
- Pull requests：29/29 已合并；归档详情、issue comments、reviews、review comments、files、commits、timeline，以及每条 PR 的 patch/diff。分页条目合计：comments 0、reviews 0、review comments 0、files 436、commits 132、timeline 241。
- 仓库数据：labels 9；milestones 0；releases 0、release assets 0。
- 设置快照：摘要分支推送后重新核对，共 11 个分支均查询且未启用 branch protection；rulesets 0；webhooks 0；Actions variables 0、secret names 0。GitHub 不提供 secret 值读取，本归档不含且不能恢复 secret 值。
- Wiki 开关为启用；实际 `.wiki.git` 返回 Repository not found，`/wiki` 返回 HTTP 302 并跳回仓库首页，记录为没有 Wiki 页面；证据见私有归档的 `wiki-status.json` 与 `wiki-page-probe.json`。
- Git：摘要推送后的 mirror/bundle 包含全部 heads/tags（预计 11 个引用，含本摘要分支；逐名 tip 见 `git-remote-refs.tsv`）；已在新 bare 仓库恢复并运行 `git fsck`。
- 私有归档：从任一工作树运行 `git rev-parse --path-format=absolute --git-common-dir`，进入输出目录下的 `agent-worklog/fork-detach-20260926-01a0dbd4/`；其中 `index.md` 为文件索引，`manifest.json` 列出其余归档文件的 SHA-256、大小、PR 集合、分页计数和 refs 集合。
- 验证：全部 JSON 可解析；PR 集合、分页数和 29 份详情对齐；所有清单哈希匹配；bundle 在新 bare 仓库恢复后 heads/tags 名称与 tip 一致且 `git fsck` 通过；错误 hash 输入被验证器拒绝。
- 限制：这是供溯源的私有本机归档，不能原样恢复 GitHub PR 页面、讨论身份关系或平台元数据；原 PR URL 保存在私有 `pr-index.json`。

## r2 刷新快照（2026-09-26）

- 原仓库实时核对仍为 `zj1123581321/CapsWriter-ASR-Server`（ID `971940657`，父仓库 ID `646394328`）；PR 共 30 条，29 条已合并，PR #30 仍 open，head `229710c33b3da89bc560f00eab0c63de607ee1f4`，原 URL 在私有 `pr-index.json`。
- r2 私有归档定位：运行 `git rev-parse --path-format=absolute --git-common-dir`，在输出路径下进入 `agent-worklog/fork-detach-20260926-01a0dbd4-r2/`；索引为 `index.md`，UTC 时间、PR URL/集合、列表计数、refs 和逐文件 SHA-256/字节数见 `manifest.json`。
- PR 列表分页 30；各 PR 明细、issue comments 0、reviews 0、review comments 0、files 440、commits 137、timeline 247，均保留原始列表 JSON、patch 与 diff。
- 仓库数据：labels 9、milestones 0、releases/assets 0；rulesets、webhooks、Actions variables、secret names 均为 0。12 个分支均返回无 branch protection；secret 值不可读取，未归档也不能恢复。
- Wiki 开关启用，但实际无页面：`.wiki.git` 返回 Repository not found，`/wiki` 跳回仓库首页；证据保存在 `wiki-status.json`。
- Git mirror/bundle 含全部 heads/tags 与 30 条 PR head；摘要分支推送后刷新归档。最终 refs 与 bundle SHA-256 见私有 `index.md`，manifest 列出逐文件哈希；manifest 自身哈希见派发报告。摘要不自引所在 commit 的 bundle 哈希。
- 验证覆盖 JSON、PR 集合及明细、分页计数、文件 SHA-256、mirror/bundle 恢复与 `git fsck`；本机归档仅供溯源，不能原样恢复 GitHub PR 页面、讨论身份或平台元数据。r1 目录保持原样。
