# E2 无状态分段规则迁移独立审查 verdict
<!-- delegate-outcome: succeeded -->
## 结论

审查结论：clean。冻结范围为 9e87c544070737b12269c76268f5d2376fad9f22..cd3a6e7ab43517e3c142d9d7f1601cdf3f23bc7a；被审 HEAD 为 cd3a6e7ab43517e3c142d9d7f1601cdf3f23bc7a。没有发现可溯源到 PCM 分段契约或 WS 不变式的 P1/P2 缺陷；本轮没有源码修复。 Dispatch-ID: dlg-20261001-180358-89d511；Task-Id 卡面未提供。

failure-visibility: clean

四问结论：

1. 旧规则只迁移位置。ws_recv.py 原先的 float 转换、有限值/上下界、引擎段预算与错误格式，完整移动到 core/server/segmenter.py 的 validate_segment_params；WS 调用顺序仍是先 float 转换、再规则校验、最后写入首帧参数缓存。
2. 没有新增 validator 对象、状态、配置、DTO、cache、fallback 或第二条实现路径。新加入的两个函数 engine_segment_limit 和 validate_segment_params 放在已有的 PCM 分段模块中；其 reader 是 WS 收包校验和 WS 分段配置。PCM 分段器本身早已存在并由 AudioCache 实际继承；后续 HTTP runner 仍是路线图中的候选调用方，当前未实现。
3. 没有观察到 WS 参数值、错误文案、默认值、锁定顺序、切点吸附、PCM 字节/偏移/overlap、最终段或 engine limit 改变。基底与迁移版本的 16 个输入结果逐字节相同；真实 WebSocket 输入和差异注入也符合预期。
4. 实际参数校验只有 shared module 入口；引擎上限由同一 Config/QwenArgs 来源提供，并传给既有 PcmSegmenter 段控。WS task id、首帧参数缓存、ValueError 到 bad_request 的映射和连接 cleanup 留在 WS。

## 规则与实际消费者

| 数据/规则 | producer 与来源 | reader 与证据 |
|---|---|---|
| 每帧 seg_duration、seg_overlap | WS 解析 AudioMessage；ws_recv.py:78 先执行 float() | ws_recv.py:79 调 shared_segmenter.validate_segment_params；错误仍由 ws_recv.py:599-606 映射到 bad_request 并清理当前 cache |
| 静态参数约束 | segmenter.py:35-63；duration 必须 finite 且 >=5；overlap 必须 finite 且 0<=overlap<duration/2 | WS 首帧校验及 shared segmenter tests；tests/test_segmentation_contract.py:50-88、175-225 |
| engine limit | segmenter.py:25-32 读取 Config.model_type；qwen_asr 用 Qwen3ASRGGUFArgs.chunk_size，qwen_asr_mlx 用 QwenASRMLXArgs.chunk_size，其余模型为 None | 两个 Qwen 配置的 chunk_size 均为 80 秒，config_server.py:194-227；无需加载模型。test_engine_segment_limits_follow_config_without_loading_models 覆盖两个 Qwen 分支和无上限分支 |
| snap 预算 | snap 开启时 max(max_cut, duration + search_after) + overlap；关闭时 duration + overlap；只有严格大于 limit 才拒绝 | segmenter.py:48-63；实际 WS 发送参数 70/8 在 snap 下因 83>80 收到 bad_request，70/4 合法且另一连接成功 |
| PCM 段及 byte producer | PcmSegmenter._cut 在 segmenter.py:245-260 按 round(seconds*16000) 量化 stride/overlap，再乘每采样 4 字节；offset 不含 overlap | ws_recv.py:124-146 配置真实 WS cache；_submit_pcm_segment 把段作为 Task 放入队列。tests/test_shared_segmenter.py:82-115 断言实际 Task.data、offset、overlap、owner_kind、task_id、is_final |
| 段预算消费者 | ws_recv.py:124-131 从 shared module 读取引擎上限并配置 AudioCache | segmenter.py:262-271 的 PcmSegmenter._assert_within_limit 在切段及最终段交付时检查字节长度；旧 WS 私有 _assert_segment_within_limit 在基底和当前提交里都只有定义、无调用，不是生产段控路径 |
| 任务参数锁 | AudioCache.task_id、segmentation_params、reset 留在 ws_recv.py:48-69 | ws_recv.py:81-95 继续在首帧锁定，后续冲突保持原错误；实际冲突和新连接隔离见 tests/test_segmentation_contract.py:175-225 |
| 最终结果消费者 | 实际 SDK 用 PCM 文件，经 WS 发送；SDK/E2E test 发送 90 秒音频，覆盖 flac、ogg_opus、s16le、f32le 和 proxy，见 tests/test_e2e_sdk_server.py:25-69 | tests/test_server_e2e_baseline.py:139-153 检查 200 秒首帧分段覆盖与单段上限；tests/test_file_result_contract_e2e.py:28-54 检查实际 final JSON 的 tokens、timestamps 与 text_accu |

参数校验函数输入矩阵逐项执行于基底和 H0（包含 float 字符串转换；此项只证明函数的 float 转换顺序，不声称字符串是合法 WS wire 输入）、4.999 下界、NaN/+Inf/-Inf、overlap 负数/0/半段内侧/半段精确边界、非 snap 的 80 秒预算边界与 epsilon、snap 的 80 秒边界与 epsilon、GGUF/MLX 上限以及无 Qwen 限制模型，共 16 组。两个 JSON 输出 SHA-256 均为 3a1e4b470d9bdc642130a8759b886084eaf3ccdb9c6c267a9144bb1e08782827。

case/nodeid 集合比较：基底收集 302 个 nodeid，H0 收集 308 个；旧 nodeid 删除数为 0，新增数为 6。修改过的既有 test_segmentation_contract nodeid 均保留。

## 发现、OCR 与 P1 判定

没有成立的 finding。OCR 前置扫描完整 JSON envelope 为 status=reviewed、reason=primary_selected、findings=2；没有 config_error。两条 severity=low 的维护建议均独立核实后不作为本轮 finding：

- ws_recv.py:99 的 _assert_segment_within_limit 在基底已是未调用定义；git grep 在基底和 H0 各只找到该定义。它不是当前生产路径，不按建议扩展 dead-code cleanup；实际段控在 PcmSegmenter._assert_within_limit。
- segmenter.py 的 engine_segment_limit 模块函数与 PcmSegmenter 属性/ configure 参数同名，但类代码只经 self.engine_segment_limit 访问配置值；当前没有导致错误的调用、输入或 spec 违例，OCR 提出的影响属于未来编辑风险。

P1 两问：对两条 OCR 候选，在实际 WS 消费路径中均找不到可触发的错误分支；第一条真实段控由共享 PcmSegmenter 执行，第二条没有当前调用形态。既无可触发故障，也无当前用户后果，均不满足 P1。没有来自 OCR 的 severity 可直接采信或照搬。

动态绑定红牙齿：scratch 中将 shared validate_segment_params 临时替换成对 duration=6 注入 ValueError sentinel，真实 WS 返回 bad_request 且包含 sentinel，另一个合法连接仍完成 final result，证明 WS 使用模块属性动态调用。随后分别把 minimum 从 5 放宽到 4、把 snap limit 改为 limit+3；每次在变异确认后，真实 WS 都接受原本非法输入并返回 final result，另一合法连接也完成。两次 mutation 均被真实 consumer 区分，scratch worktree 自动清理；不把这两点误报成分支覆盖的穷尽证明。test_final_file_cut_does_not_wait_for_more_audio 还通过 shared_segmenter.engine_segment_limit 的 monkeypatch 验证段控函数的模块属性入口。

## 验证结果与边界

- 无 aiohttp 隔离环境：Python 3.12.3、websockets==15.0.1；aiohttp 导入得到 ModuleNotFoundError 且退出码 1，CW_HTTP 相关环境变量未设置，仓库根没有 .env 文件。ManagedFakeServerHarness 使用真实子进程 worker、TCP/WebSocket 与 /health；健康状态 worker_alive=true。默认 WS 服务、参数正反例及另一连接、SIGTERM 两个零退出用例和分段回归共 28 passed、4 warnings。
- 当前 H0 本地全量：Python 3.12.3、websockets==15.0.1：305 passed、3 skipped、91 warnings；websockets==17.1：305 passed、3 skipped、91 warnings。命令均在 H0 官方 scratch worktree 中运行：python -m pytest tests/ -q。
- CI 快照：PR #46 在该 H0 SHA 的 run 36897025964；单元测试 (websockets==15.0.1) 与单元测试 (websockets) 均 SUCCESS。PR 当时为 draft；run 36897027220 的 gate / primary 是 SKIPPED，不能算主审完成。没有把 draft CI 绿当成完整 gate 结论，也没有将 PR 标 ready、合并或部署。
- 三个 skip 的完整原因在本地 -q 汇总及已核对的 CI job 状态字段中不可见；本地未装真实 ASR 模型，也未在 Windows/macOS/3.9/真实部署 systemd 环境运行，因此 skip 根因、真实模型与跨平台行为均记 unknown。卡面提供的主干基线 gh api request failed，继承红/新红无法由该主干对照判定；本次两套本地完整套件和该 H0 的 CI 两个测试 job 均无失败。
- diff 只涉及既有 WS、共享分段模块、两测试文件和一份新增进度文档（未读其内容）；没有 HTTP listener/store/runner、SDK、依赖、CI 或配置实现。E2 HTTP 与 R3 最终完成仍未实现，不在本 verdict 中宣称可用。

主要实际命令：

1. OCR：ocr-review --repo <repo> --from 9e87c544070737b12269c76268f5d2376fad9f22 --to cd3a6e7ab43517e3c142d9d7f1601cdf3f23bc7a --audience agent --concurrency 4 --background-file <private-temp>/ocr-background.md；检查 JSON 的 status/reason/findings，而非退出码。
2. 无 aiohttp：env -u CW_HTTP_PORT -u CW_HTTP_DATA_DIR <noaio-python-3.12> -m pytest -q tests/test_health.py::test_health_endpoint_tracks_work_and_preserves_http_and_websocket tests/test_server_headless.py::test_sigterm_triggers_stop_and_exits_0 tests/test_server_headless.py::test_sigterm_while_event_loop_is_idle_exits_0 tests/test_segmentation_contract.py tests/test_shared_segmenter.py。
3. 两套全量与基底矩阵均经官方 scratch-worktree.sh helper，以 9e87c54 或 H0 建立独立临时 worktree；矩阵脚本写入上述 16 组 producer/validator 结果后用 cmp 比较。变异通过同一 scratch-worktree 脚本运行。
4. PR 快照：gh pr view 46 --json number,title,state,isDraft,headRefName,headRefOid,baseRefName,statusCheckRollup；gh run list --commit cd3a6e7ab43517e3c142d9d7f1601cdf3f23bc7a；gh run view 36897025964 --json jobs,headSha,event,status,conclusion。

## 踩坑

pickup-brief.sh 首次误用 Python 启动，返回 SyntaxError；改由 bash 执行后确认本工作区无交接单。前三次临时网络探针分别暴露：harness 模块导入早于注入、ManagedFakeServerHarness 没有 port 属性、source=file 需要对齐器。这些均发生在外部临时 probe，修正为导入前注入、由 harness.url 取得端口并使用 mic 后，基底与 H0 的探针通过；不属于产品失败。

16 组参数矩阵首次从 /tmp 导入仓库模块失败，因为运行脚本目录替代了 cwd 的 sys.path；外层未设 fail-fast 曾打印无效的“相等”文本。该次结果作废。修复临时脚本加入当前 scratch 根目录，并用 set -euo pipefail、cmp 实际比较后，两端均生成 16 组记录且哈希一致。

## 绕过

按卡面使用无 aiohttp 新 venv、未读取/加载 .env、未触及模型或录音。所有 before/after、mutation 和 collect-only 都使用官方 scratch-worktree.sh；scratch 根在独立私有目录，临时脚本与日志留在 /tmp，未改被审 checkout 的源码和测试。最终仓库只新增本 verdict 文件。

## 偏差

有三个 pytest skip，本轮未从 CI job status 字段或本地 -q 汇总定位到具体 skip reason，按 unknown 记录；未推断为已验证模型兼容。没有运行真模型、跨平台、Python 3.9 或实际生产 systemd。CI 主审 job 由于 PR draft 为 SKIPPED；未做 ready/merge/deploy。独立 OCR 的两条低级维护建议记录为已核实但不成立的 finding，不改变 clean 结论。

## 最贵一步

H0 两个 WebSocket 版本的全量测试耗时 108.62 秒与 111.57 秒，合计约 220 秒；是本轮耗时最长且覆盖价值最高的验证。源码/参数矩阵没有引入额外依赖，OCR wrapper 状态已按完整 JSON envelope 核对。
