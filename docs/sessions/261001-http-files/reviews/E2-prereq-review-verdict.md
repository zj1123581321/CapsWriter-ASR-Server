<!-- delegate-outcome: succeeded -->
# E2 prerequisite independent review verdict

failure-visibility: p2-only

审查对象：`9ecd70fb2a6e8f87b82db67b9c435209ddae9b43..b11659cc3afec1bee85b4ee82953a24b31dfe77b`，risk-tier internal。结论：未发现 P1；一项 P2，接受本卡不修。当前主干没有 aiohttp 服务端导入或 HTTP listener；本结论不表示 HTTP 上传功能已可用。

## Finding

- **P2：CI 不再持续覆盖 aiohttp 缺失时的默认服务导入路径。** `.github/workflows/ci.yml:30` 在两个 Python 3.12 矩阵都安装 aiohttp；`tests/test_server_headless.py:116-129` 虽会子进程导入真实服务端链路，但 CI 环境已提供 aiohttp，不能发现将来误加的顶层 aiohttp 导入。对应契约是 `docs/sessions/261001-http-files/design.md:34-36`。本次独立 OCR 标注 medium / confirmed；本仓判定为 P2。
  - P1 两问：①真实触发路径——在隔离 Python 3.12.3、确认未安装 aiohttp 且无 `CW_HTTP_*` 环境变量时，默认 WS、health、真实 fake-worker/TCP 与 SIGTERM 测试均通过；当前 `core/server` 无 aiohttp/CW_HTTP 引用。实际部署解释器未核实，不能外推。当前没有触发。②若未来无依赖部署误导入 aiohttp，服务导入/启动会失败，后果不可接受；但这只是未被 CI 锁住的未来回归，不是当前运行故障，故不升 P1。
  - 不变式实测命令：`env -u CW_HTTP_PORT -u CW_HTTP_DATA_DIR PYTHONNOUSERSITE=1 /tmp/cw-http-prereq-review-261001/ws-noaio-venv/bin/python /tmp/cw-http-prereq-review-261001/noaio_ws_probe.py`；结果 `42 passed`。保持本卡范围，不改 CI 或实现；建议后续为无 aiohttp 导入路径保留自动回归覆盖。

## 关键不变式

- **路线图仅为规划，状态与索引一致。** M1=已完成/merged_pr 37，M5=已完成/merged_pr 36，M2=进行中/merged_pr null，M3/M4/M6/M7=未开始/merged_pr null；`goals_index.py check --repo "$PWD"` 退出 0。PR36/37 已合入冻结基底（a7ad711、c1e8808）。原规划提交与继承提交成对 patch-id 相同（e20=f234，ce52=2548），配对 GOALS.md 与 goals/ 树相同。
- **依赖 marker 和真实消费者。** 三份 `requirements-server*.txt` 的原始 aiohttp 行字节相同：`aiohttp==3.14.3; python_version>='3.10'`；临时 3.12 venv 用三份真实行分别 pip 安装，均得到 aiohttp 3.14.3。packaging marker：3.9=False、3.12=True；去 marker 夹具 3.9=True。已安装 wheel 元数据为 `Requires-Python: >=3.10`。本机没有 Python 3.9，未声称 3.9 实装。
- **CI/测试入口。** CI 实际 pip argv 在两个矩阵都包含 `httpx==0.28.1` 与 `aiohttp==3.14.3`；同一 b116 run 两 job 均 SUCCESS（289 passed、3 skipped）。完整 uv 命令包含 aiohttp 和 HTTPX。PR44 仍为 draft；gate primary 与 OCR 为 SKIPPED，不计完整主审通过。
- **兼容和证据边界。** 无 aiohttp / 无 `CW_HTTP_*` 的隔离进程中，`tests/test_health.py`、`tests/test_error_contract.py`、`tests/test_port_restart.py`、`tests/test_server_headless.py`、`tests/test_file_result_contract_e2e.py`、`tests/test_pipeline_final_contract.py` 共 42 passed；真实 `ManagedFakeServerHarness`、health/WS TCP、SIGTERM 与 PR41 finalfile payload 消费均在当前 main 测试路径。PR41 finalfile 核心/测试及 PR42 `.github/workflows/gate.yml` caller 在冻结基底且本增量原样；`core`、配置、SDK、tests、协议、deploy、gate 文件相对 9ecd 未改。当前没有完整 HTTP 服务端应用代码/测试，未搬 PR38 成功证据。

## OCR

状态 `reviewed`，reason `primary_selected`，profile `minimax`；1 finding、1 confirmed、0 refuted。完整 JSON envelope 保存在 `/tmp/cw-http-prereq-review-261001-ocr-envelope.json`（3668 bytes）。CI workflow 源码 argv 与两矩阵实际 run 日志已对照。
