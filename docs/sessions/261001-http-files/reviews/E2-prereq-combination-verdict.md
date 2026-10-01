<!-- delegate-outcome: succeeded -->
# E2 前置依赖与最新 SDK 主干组合消费复核

failure-visibility: p2-only

## 踩坑

- **本轮组合消费 review verdict：pass；无新增 finding。** `failure-visibility: p2-only` 来自锁定的既有 P2，接受不加机制。派发卡说明首轮真实 OCR 已 reviewed，留有一条 P2；本轮不是 H1 OCR，不重复调用 OCR。该 P2 是 CI 永久矩阵都安装 `aiohttp`（`.github/workflows/ci.yml:30`），因此 CI 不会长期覆盖可选依赖实际缺席的场景。两问：①真实路径会触发吗？不安装 aiohttp 的默认 WS 部署确实存在，本轮在该环境跑过 SDK 与真实服务端消费者；当前代码通过。若未来默认 WS 路径新增 aio 导入会受影响。②当前后果是否不可接受？本轮没有运行故障，剩余是回归覆盖盲区，按锁定决策为 P2 并接受；不新增 CI matrix、状态或配置。没有 P1。
- 冻结对象为 `b11659cc3afec1bee85b4ee82953a24b31dfe77b..e57411d345d2405bd4fcbbfe22ac59c72ab18e4c`。`e57411d` 是实际 merge commit，父提交恰为 `b11659c` 与 `79c39363af9c58c6404589f5905079cd46e1ef7f`，无第三父或额外实现。
- 按全组合视角 `79c3936..e57411d` 是 13 个路径、87 增 34 删（121 行变更）。服务端实现、配置、SDK、测试、部署、协议与 gate 相对 `79c3936` 保持逐树/逐 blob 相同：`core=40612f4e10ef4bd29758513e34447b8d871d1f5b`、`sdk=90a77b7daf7800e31e21c13283f571efa274d666`、`tests=e64499a9a1dd4825d7b5da8d8261a6ee6dbc839d`、`deploy=f8db60158a942ceed55247ee4a37b923e0f61be0`、`docs/reference/protocol.md=ab00792e2246ae19f01577a4d03fe6189e4fb7ba`、`.github/workflows/gate.yml=6343d1f6fecf7f590c54cdf4cf68b1710860b1f3`。三份 `requirements-server*.txt`、`ci.yml`、首条正式 testing 命令、`GOALS.md` 与 `goals/` 均与 `b11659c` 对象相同；只读 goals 索引校验退出 0。

## 绕过

- **四问。** ①合入内容仅是登记的前置依赖/目标索引变化，最新主干只作为第二父继承；全组合差异未多出服务端或 SDK 产品实现。②组合没有引入抽象、状态、fallback、配置或双路径。③SDK 的 v2 health gate 明确将 HTTP 426、protocol v1/畸形 health、服务端不支持的 encoding 在开 WebSocket 前返回错误；`tests/test_sdk_client.py:265` 覆盖此契约。有效 v2 health 下的 SDK→WS 实测通过。没有证据表明本次依赖合并造成旧服务误拒；也未把单测伪装成旧版生产服务实机验证。④全组合没有新增 HTTP handler/API/应用证明；`GOALS.md` 与 `goals/` 沿用 `M1/M5 已完成、M2 进行中`，没有把 M2 写成完成。
- **最新 SDK 生产者与实际 wire。** `sdk/capswriter_asr/client.py:398` 先检查 health；`401/403/405` 根据 SDK 实际发出的 raw bytes 计数，压缩编码在 `405` 从该字节流解码计数，再由 `413` 进入 final WebSocket 帧。`tests/test_sdk_samples_total.py:183` 的真实 SDK/TCP WebSocket 接收端以 ffmpeg 解码输出样本数对比 FLAC/Opus final 帧；`:206` 从收到的 Base64 payload 字节长度计算 raw 样本数。该判据读 producer 实际发送的数据，不用 `sf.info` 容器时长推算。
- **真实消费者。** `tests/test_e2e_sdk_server.py:25` 经 `ManagedFakeServerHarness` 启动标准跨进程 worker 与 TCP WebSocket，SDK 上传 90 秒文件覆盖 FLAC、Opus、s16le、f32le 与 proxy；health 检查、final 消费、队列停服都在该路径上。`tests/test_file_result_contract_e2e.py:28` 与 `tests/test_pipeline_final_contract.py:99` 锁定真实 WS file final 和 worker finalizer 不变式。`tests/test_server_headless.py:133` 的 SIGTERM 探针断言服务退出码 0。
- **无 aiohttp / 默认关闭路径。** 在 Python 3.12 的隔离 uv 环境中移除 `CW_HTTP`，依赖清单未安装 aiohttp。独立 `import aiohttp` 探针退出 1，原因为 `ModuleNotFoundError: No module named 'aiohttp'`；同一依赖集合运行以下消费者，结果 `54 passed, 8 warnings in 29.09s`。这证明当前真实消费环境中 aio 不存在，且默认 WS 路径没有依赖它。
  ```sh
  env -u CW_HTTP UV_CACHE_DIR=/tmp/capswriter-e2-prereq-combo-261001-noaio-cache uv run --no-project --python 3.12 --with numpy --with rich --with websockets --with colorama --with pytest==9.1.1 --with soundfile --with pytest-asyncio==1.4.0 --with httpx==0.28.1 python -m pytest tests/test_sdk_samples_total.py tests/test_sdk_client.py tests/test_file_result_contract_e2e.py tests/test_pipeline_final_contract.py tests/test_e2e_sdk_server.py::test_real_sdk_transcribes_90_seconds_with_all_encodings_and_proxy tests/test_server_headless.py -q -p no:cacheprovider
  ```

## 偏差

- 组合 CI 快照：run `36878415590`（head `e57411d`）的两个 WebSocket 矩阵 job（最低 `15.0.1` 与未 pin 最新）及 pytest 步骤均成功。gate run `36878416144` 的质量、测试与 draft aggregator 成功，但 `gate / primary`、`gate / ocr`、`gate / resolve_advisory` 是 **skipped**；这是 draft 的绿，不能记作完整主审成功。主干 `79c3936` 的 push run `36877299693` 两个矩阵也都成功。
- 派发给出的主干基线查询为 `gh api request failed`，故历史红无法判定继承或新增；本轮可见的上述 CI job 没有新红。Windows、macOS、Python 3.9 与真实部署机未实测，均保留 unknown；本地结论仅覆盖 Python 3.12。
- 按任务卡不重跑 OCR。repo 收件箱仍有 open issue #43（旧容器时长估算 `samples_total`）；PR45 当前 producer 的字节计数由上述真实 wire 测试覆盖，本卡只记录、不操作 issue。pickup 未发现交接单；欠账巡检为 `orphan 0 / owned 0 / unattributable 0 / stale-over-7d 0`。memory 报告探针返回 `memory_dir_mismatch`，因此不据此宣称记忆巡检通过。

## 最贵一步

- 正式全量命令按 `docs/development/testing.md`、Python 3.12、当前 CI 依赖在隔离 uv cache 执行：`uv run --no-project --python 3.12 --with numpy --with rich --with websockets --with colorama --with pytest==9.1.1 --with soundfile --with pytest-asyncio==1.4.0 --with aiohttp==3.14.3 --with httpx==0.28.1 python -m pytest tests/ -q -p no:cacheprovider`。结果：`299 passed, 3 skipped, 91 warnings in 116.98s`。三项 skip 已复核：缺 Silero VAD/onnxruntime 一项，缺 ForceAligner 后端/模型两项；均是既有模型资源 skip。
- 机器目标索引检查：执行 agent-config 提供的只读 `goals_index.py check --repo "$PWD" --base HEAD`，退出 0。任务卡验收命令为 `test -s docs/sessions/261001-http-files/reviews/E2-prereq-combination-verdict.md`。
