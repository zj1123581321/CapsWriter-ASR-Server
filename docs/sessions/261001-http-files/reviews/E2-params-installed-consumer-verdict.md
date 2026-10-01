# E2 安装包消费者反向复核

failure-visibility: clean

审查对象：`9e87c544070737b12269c76268f5d2376fad9f22..cd3a6e7ab43517e3c142d9d7f1601cdf3f23bc7a`（PR 46）。风险等级 internal。本轮不重扫 OCR（首轮已 `reviewed` / `primary_selected`）。本 head 无 HTTP 服务端代码，不把 R3 HTTP / M2 未接线判为缺陷。

## 结论

共享无状态分段规则从 WS 接收层搬到 `core/server/segmenter.py` 后，**安装后的 SDK wheel 文件入口**沿真实 health → 编码帧/`samples_total` → 服务端解码 PCM → `PcmSegmenter.configure`/`drain_ready` → 真 worker `Task.data` → WS final JSON 的交付物字段，与基底 9e87 同消费者对照一致。合法小数与引擎/吸附预算、非法早返回 `bad_request`、邻接连接成功、下一任务首帧不串参，均在真实 TCP 上看到。未发现 P1；无需要阻塞的 P2。

## 安装入口与包定位

- 客户端在私有 venv 安装 `capswriter-asr 0.1.0` 纯 Python wheel（`py3-none-any`），工作目录不是仓库 `sdk/`。
- 运行时 `capswriter_asr.__file__` 落在该 venv 的 `site-packages`，不经过仓库源码路径；`sys.path` 不含审查树 `sdk/`。
- `importlib.metadata` 读到 dist-info：`METADATA` / `WHEEL` / `RECORD`，包文件含 `client.py` 等，无 `core`。
- 同进程 `import core` 为 `ModuleNotFoundError`。下游 SDK 源码亦无 `import core` / `from core`。
- `/health` HTTP 200，`protocol_version=2`，`role=server`，`git_sha=cd3a6e7`，`worker_alive=true`，`encodings` 含 `f32le`/`s16le`/`flac`/`ogg_opus`。

## 反向取证（本轮新跑的真实进程）

探针先喂 **known-bad** 再 good。服务端为仓内 `ManagedFakeServerHarness`：真 WebSocket、真 worker 子进程、可编程假引擎；分段规则走生产 `ws_recv` → 共享 `validate_segment_params` / `engine_segment_limit` / `cache.configure`。客户端只跑已安装 SDK。音频为可再生正弦 float32，不是真实录音。

| 场景 | 交付物 |
|---|---|
| 4.999 / 0 | 错误帧 `code=bad_request`，文案 `seg_duration=4.999 不在允许范围 [5, +∞)` |
| 79 / 2（snap 关，qwen 上限 80） | `bad_request`：`单段最长 81s … ≤ 引擎上限 80s` |
| 随后另一连接 5.5 / 0.5、12s `f32le` | final `is_final=true`，`duration=12.0`，`samples_total=192000`（12×16000），wire 字节 768000=`n_f32×4` |
| 同连接首帧 6 / 1、次帧 5.5 / 0.5 | `bad_request`：`seg_duration=5.5 与该任务首帧值 6 不同` |
| 下一连接 6 / 1、12s | 成功，wire `seg_duration=6.0` `seg_overlap=1.0`，不沿用上一任务的 5.5 |
| 90s `f32le` 5.5 / 0.5 | 两帧：非末 3840000 B + 末帧 1920000 B；末帧 `samples_total=1440000`（90×16000，来自已发 PCM 字节而非容器时长）；final `duration=90.0`，`raw_is_final=true`，含 `text`/`text_accu`/`tokens`/`timestamps` |
| 90s `flac` 5.5 / 0.5 | 9 帧压缩字节合计 2140240（小于 raw 5760000）；`samples_total` 仍 1440000；final 90.0s，时间戳按 5.5s 步进覆盖到 88.0s |
| snap 开、70 / 8 | `bad_request`：`seg_max_cut=72, seg_duration=70, seg_search_after=5, seg_overlap=8` → 单段最长 83s > 80s |
| snap 开、70 / 5、12s | 成功（预算恰好 ≤80） |

`Task.data`（queue 实际 put，非日志子串）：`owner_kind=ws`，`samplerate=16000`，`len(data)%4==0`，可按 little-endian float32 整段解释且有限；12s 成功任务 `offset=0` `overlap=0.5` `is_final=true` `n_f32=192000`。90s 文件任务 offset 按 5.5s 步进；raw 两帧路径因引擎上限 80s、末帧一次性带剩余 PCM，末段从 offset 55s 起覆盖余下 35s（55+35=90），与 `_drain_fixed` 在 `is_final` 且 `duration≤80` 时不再切是同一条规则。Worker `sample_count` 与对应 `n_f32` 一致。

## 规则与 WS 行为（对照 spec / 不变式）

- 取值与引擎预算只在 `validate_segment_params` / `engine_segment_limit`；`ws_recv._validate_segmentation` 只保留首帧锁与缓冲任务匹配。`nominal < 5` 的字面副本在 `core/` 仅此一份。
- `_submit_segments` 每次 `configure(engine_segment_limit=engine_segment_limit(), cut_snap=Config.seg_cut_snap, …)`，不缓存另一份快照。
- 合法小数 5.5 / 0.5 与 5.00001 类（单测）不误拒；非 qwen `model_type` 时 `engine_segment_limit()` 为 `None`（单测 paraformer/sensevoice/fun_asr_nano）。
- 错误类仍是 `ValueError` → WS `bad_request` 后断开；非法连接不影响随后合法连接。
- 未新增 validator 对象、配置项、依赖、fallback、假 socket。SDK 未改（本 diff 不含 `sdk/`）。
- 本 head 无 HTTP server；R3 接口与 HTTP runner 复用仍属后续 M2，不能要求本提交完成。

## 基底 9e87 对照

同一安装 SDK、同一 12s `f32le` 5.5/0.5 与 4.999 负例，对 9e87 树再跑一遍：

- 负例文案逐字相同；
- good：`samples_total=192000`、wire 768000、`duration=12.0`、`is_final=true`；
- `Task.data` 字段（offset/overlap/is_final/n_f32/aligned4/owner_kind/samplerate）相同；
- harness 优雅 stop 退出码 0。

行为未因搬家改变。全量相对 `origin/master`（9e87c54）仍是这 97 行：`ws_recv.py` 净删副本、`segmenter.py` 增加两函数、测试改引用、进度文档。

## 变异牙齿（改完已还原，工作区实现文件干净）

注入前三组相关测试 7 passed。

1. `nominal < 5` 改为 `< 4.9`：`test_shared_segment_params_rule_is_connection_free[False-4.999-0.0-…]` **DID NOT RAISE**（1 failed / 6 passed）。
2. `max_segment > limit` 改为 `> limit + 1000`：无状态 79/2 与 snap 70/8 用例转红，且真实 WS `test_bad_parameters_fail_fast_and_other_connection_completes` 等终态超时（非法帧不再 `bad_request`）。3 failed / 4 passed。

还原后字节校验通过。模块属性与 configure 上限被改坏时现有测试有牙齿，无需新防御。

## SIGTERM、缺 HTTP 库、回归 skips

- 生产 SIGTERM 路径：`tests/test_server_headless.py` 7 passed（含一次 SIGTERM → 退出码 0）。Harness 裸 `Process` 直接 SIGTERM 得到信号退出码 -15，没有应用 `stop()` 处理器；不是本 diff 引入的生产路径，也不构成 P1。
- 隔离 venv 不装 `aiohttp`/`httpx`：`import aiohttp` / `import httpx` 非零（`ModuleNotFoundError`）。当前环境 **无任何 `CW_HTTP*` 变量**。WS health + 文件 final 不依赖这两库。本 head 无 HTTP 服务端，缺可选 HTTP 库不改变 WS 行为。
- 全量 `pytest tests/ -q -rs`：**305 passed, 3 skipped**，约 108s。skip 原文：
  - `tests/test_aligner_integration.py:53` ForceAligner 后端/模型未安装
  - `tests/test_aligner_integration.py:62` 同上
  - `tests/test_segmenter.py:208` 缺 silero-VAD 模型或 onnxruntime  
  与「既有 VAD/依赖条件」一致，不是 HTTP 业务 skip。

## 源核（本 diff）

`ws_recv` 删除 `_engine_segment_limit` 与范围内联校验，改为调用共享函数；`_assert_segment_within_limit` 与 `configure(engine_segment_limit=…)` 同步改引用。`segmenter.py` 增加两个无状态函数，文案与条件顺序与删前一致。测试把断言主语从 `ws_recv._engine_segment_limit` 改到共享函数。无第二规则引擎、无新状态。静默出错表在本 diff 新增路径上无 `except: pass` / 空降级。`ws_recv` 里 `CancelledError: pass` 为存量连接取消，不在本 97 行新增语义。

熵：抽出两个函数是 spec 写明的「当前 caller 是 WS、后续 HTTP 复用」；HTTP 第二消费者尚未接线。这不是无消费者的新 validator 对象，不升 P1。

## P1 两问

本轮无外部工具 major/P1 候选需落地。自检项「Harness SIGTERM 非 0」：

1. 真实使用方式会触发吗？生产入口是 `start_server` 的信号处理；该路径本机 POSIX 测到退出 0。审查用的 harness 子进程不是生产监督对象。
2. 触发后果能否接受？测试夹具被信号杀死不影响识别结果对错。两问不能同时成立 → 不是 P1，不派修。

## 未测（unknown，不外推）

真实识别模型、Windows、macOS、Python 3.9、systemd 生产单元、300s 墙钟、真实麦克风/真实音频文件：未测。Opus 本轮未单独作为压缩编码（压缩轴用了 FLAC；SDK/health 仍声明 `ogg_opus`）。不把 before/after 绿测写成全平台质量证明。

主干 CI 基线派发时不可用；继承红未能判定。本机全量 305 passed / 3 skipped 与卡面声称的 skip 形态一致。

## OCR

按卡面：首轮已真实 OCR `reviewed`/`primary_selected`，两 low 维护建议已不成立。本轮不在同一 head 重扫，完整消费者/源核未缩小。
