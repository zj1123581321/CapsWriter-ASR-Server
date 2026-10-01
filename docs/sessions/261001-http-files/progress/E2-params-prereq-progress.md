# E2 参数前置（复用既有无状态分段参数规则）进度

- **当前阶段**：implementing；本增量只把既有 WS 无状态分段参数规则迁到共享模块，HTTP 业务入口仍不存在（本 main 无 HTTP 实现，行为无变化）。
- **本段结论**：`core/server/segmenter.py` 新增两个无状态函数作为唯一权威规则：`engine_segment_limit()`（按 Config.model_type 读 Qwen3ASRGGUFArgs/QwenASRMLXArgs 的 chunk_size，不加载模型）与 `validate_segment_params(nominal, overlap)`（finite 下界 5、`0 <= overlap < nominal/2`、按 `seg_cut_snap` 用 `seg_max_cut/seg_search_after` 算单段最长并与引擎上限比较）。WS 接收层 `_validate_segmentation` 净删这两段副本，只保留首帧参数锁定、缓冲任务不匹配；`_submit_segments` 的段控上限与参数校验现在读同一个 `segmenter.engine_segment_limit()`。
- **锁定与否决**：条件顺序、`:g`/`.6f` 文案、默认值、`float()` 转换与小数合法语义逐字不变；错误类仍是 `ValueError`（WS 侧映射 `bad_request` 未动）。保留首帧参数锁定、小数分段、切点吸附单段上限、final 分段与背压。未新增 validator 对象/DTO/Cache/配置/依赖/重试/fallback，未搬 HTTP 模块或候选证明。
- **验证结论**：全量 Verify-Command 305 passed、3 skipped、退出码 0（skip 为既有 VAD/依赖条件）。反向最小变异两类均在共享函数上注入且立刻红、非 ImportError：把 nominal 下界 5 改 4.9 → `test_shared_segment_params_rule_is_connection_free` DID NOT RAISE；改 5.5 → 真实 WS 连接负态 `test_bad_parameters_fail_fast_and_other_connection_completes` 红；把 `max_segment > limit` 放宽 → snap 引擎上限用例 DID NOT RAISE。注入前后文件字节已还原。
- **下一步**：本卡草稿 PR 仅声明「复用既有无状态分段参数规则」，不得声称 HTTP 可用；HTTP 后继需实际接线并自行计算段数上限。
- **未完成**：R3 复用规则的后续 HTTP 段、M2 全部；HTTP 识别完成度未推进。