# 当前文本拼接算法

本文描述当前服务端文本输出使用的 `merge_by_text`，以代码为准。旧版详细说明仍保存在[历史算法文档](../../archive/legacy-client/text_merge_algorithm.md)，其中的 20 字窗口、`ERROR_TOLERANCE` 和逐字模糊容错不适用于当前实现。

## 调用位置

`TaskPipeline._process_simple_merge`（`core/server/worker/pipeline.py`）清理本段识别文本后调用 `merge_by_text`，更新主要输出 `Result.text`。用于字幕的 token 与时间戳走另一条路径：`merge_tokens_by_sequence_matcher` 位于 `core/server/merger/token_merger.py`，不要把它和文本拼接混为一谈。

## 匹配与拼接

实现位于 `core/server/merger/text_merger.py`：

1. 任一输入为空时，直接返回另一个输入。
2. 去掉 `prev_text` 末尾标点，并跳过 `new_text` 开头标点；若清理后任一侧为空，则返回原始两段直接拼接。
3. 只比较清理后前文末尾最多 100 个字符和新文本开头最多 100 个字符。`SequenceMatcher` 使用 `autojunk=False`，取其匹配块。
4. 候选匹配长度至少为 2；匹配块终点必须严格超过前文窗口的四分之三位置，且起点必须位于新文本窗口的前四分之一位置（含边界）。
5. 候选按 `size * size + a - b` 评分，最高分胜出；其中 `a`、`b` 是匹配在两个窗口中的起点，`size` 是匹配长度。
6. 命中时，前文保留到匹配块末尾，新文本跳过其开头至匹配块末尾的部分，再拼接剩余文本。未命中时，直接拼接原始两段。

窗口长度按 Python 字符串长度计算。算法使用 `SequenceMatcher` 给出的相同字符块，不做同音字、编辑距离或逐字错误容忍；窗口之外的重复文本不会参与匹配。结果是面向流式回显的文本去重策略，不承诺纠正识别错误。

## 代码位置

- 文本路径：`core/server/merger/text_merger.py`，由 `TaskPipeline._process_simple_merge` 调用。
- Token 与时间戳路径：`core/server/merger/token_merger.py`，由同一流水线单独调用。
