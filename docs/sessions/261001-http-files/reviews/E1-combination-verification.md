<!-- delegate-outcome succeeded -->

# E1 组合验证报告（Core cfc32fc + SDK a7ad711 合入 850d41b）

本卡不是第三轮扩审，只记录组合树的消费侧验证证据。未修改任何实现、测试、CI、goals。

## 1. 冻结身份与来源相等

冻结 SHA：`850d41b440c53b2a8902bb8ae42bfe25479cc5a6`

```
$ git show -s --format='%H %P' 850d41b
850d41b440c53b2a8902bb8ae42bfe25479cc5a6 cfc32fc5a815bca17998a48d0f55acfecc81217f a7ad7118287d77557ef78b3fe8f6ec93319c4de4
```

两个父提交精确等于已终审的 Core（cfc32fc）与已 merge master 的 SDK（a7ad711），无第三个来源。

### 1.1 Core 生产内容 + 已审测试 + contract 与 cfc 一致

```
$ git diff --exit-code cfc32fc..850d41b -- core/server tests/test_owner_ipc.py \
    tests/test_shared_segmenter.py tests/test_segmentation_contract.py \
    tests/test_backpressure.py docs/sessions/261001-http-files/design.md
DIFF1=0
```

### 1.2 SDK + CI + HTTP 测试与 a7ad 一致

```
$ git diff --exit-code a7ad711..850d41b -- sdk .github/workflows/ci.yml tests/test_http_client.py
DIFF2=0
```

### 1.3 全量 name-status：没有把新增逻辑藏到别的路径

`git diff --name-status cfc32fc..850d41b` 只有 SDK 侧 9 项（`.github/workflows/ci.yml`、`sdk/README.md`、`sdk/capswriter_asr/{__init__,cli,client,http_client}.py`、`sdk/pyproject.toml`、`tests/test_http_client.py`、一份 SDK 进度文档）。

`git diff --name-status a7ad711..850d41b` 只有 Core 侧 22 项，全部落在 `core/server/**`、Core 四份已审测试、`docs/development/{architecture-workflows,data-flow}.md`、本 session 的 design/qa/进度文档与 `goals/http-*`。

两个方向的文件集合是同一棵树的互补，没有任何一条路径同时被两侧改动 → merge 未产生冲突解法，也不存在第三种实现。

## 2. CI 同款全量测试（两个 websockets 版本，各一次）

两组都用 `uv run --no-project` 隔离 venv，`httpx==0.28.1` 显式钉住（新 SDK 的真实依赖），`pytest==9.1.1`、`pytest-asyncio==1.4.0` 显式钉住，不依赖系统偶然环境。全量发现 `tests/`，无 `-k`。

```
组 A（websockets==15.0.1）：
263 passed, 3 skipped, 85 warnings in 101.49s

组 B（websockets 最新，实测 17.1，httpx 0.28.1）：
263 passed, 3 skipped, 85 warnings in 99.88s
```

3 个 skip 的原因（`-rs` 原文）：

- `tests/test_aligner_integration.py:53` ForceAligner 后端/模型未安装（macOS 需下载 b7798 dylib + Qwen3-ForcedAligner 模型 + onnxruntime）
- `tests/test_aligner_integration.py:62` 同上
- `tests/test_segmenter.py:208` 缺 silero-VAD 模型或 onnxruntime

这 3 个是**资源/环境 skip**，不是本批功能通过，也不计为 feature 覆盖。

## 3. 实际 producer 证据（非仅存在性）

新 HTTP 服务端尚未实现——`rg -ln "aiohttp|fastapi|uvicorn|http.server" core/` 无命中。因此本卡**不宣称真正的 server-side HTTP 已通**，只验证旧 WS 入口与新 HTTP 客户端 producer 真实执行且互不污染。

- 旧 SDK WS 全链路：`tests/test_e2e_sdk_server.py::test_real_sdk_transcribes_90_seconds_with_all_encodings_and_proxy`（2 个参数化用例）在真实服务端上跑 90 秒音频，断言不是存在性而是内容：`assert isinstance(transcript, Transcript)` 加 `assert_gapless(decode_spans(transcript.text), 90_000, backend.calls)`——即返回的分段必须无缝覆盖 90s 且与后端实际调用一致。
- 背压/断线：`tests/test_backpressure.py` 7 个用例真实起服务端，含 `test_inflight_limit_stops_reads_and_returns_all_results`（并发上限下仍返回全部结果）、`test_disconnect_while_waiting_for_slot_cleans_task`（断线清理）、`test_upload_idle_returns_bad_request`、`test_backpressure_pauses_upload_idle_timer`。
- HTTP 客户端真实 TCP producer：`tests/test_http_client.py` 自带 `TcpCapture`（第 40 行起，基于真实 socket 监听），20 个用例全部走真实字节收发。**证明不是仅存在性的关键断言**：
  - `test_source_mismatch_and_existing_recovery_make_zero_requests`：`assert server.requests == []` —— 负态下必须发出**零个** HTTP 请求（server 端 handler 若被触达就返回 500 `must-not-request`，测试即红）。这是"没污染"的可证伪判据，不是"函数在不在"。
  - `test_cli_http_result_has_nonzero_failure_without_token_in_stderr`：CLI 负态必须非零退出且 stderr 不含令牌。
  - `test_invalid_http_responses_are_visible[302/…]`：非法响应必须显式报错。
- Owner IPC：`tests/test_owner_ipc.py` 7 个用例，含 `test_http_owner_gate_crosses_real_queue[True/False]` 与 `test_unknown_owner_kind_fails_fast`（fail-fast，不 silent）。

### 3.1 并发/网络关键整文件重复 5 次（不重启生产模型）

```
$ for i in 1 2 3 4 5; do ... pytest tests/test_backpressure.py tests/test_http_client.py -q; done
27 passed in 31.17s
27 passed in 34.44s
27 passed in 30.11s
27 passed in 29.90s
27 passed in 30.13s
```

5/5 全绿，无 flake。

## 4. 已接受的 P2（本卡不扩修，也不当作 0 finding）

以下均为前序终审已明确接受、本卡**原样保留**的取舍：

1. 官方 SDK 小帧下资源有界；自定义最大 64MiB WS 帧可在申请 semaphore **之前**批量准备约 95MiB 段 payload。未证 OOM、未证错结果，接受当前性能取舍，**不修改分段接口**。
2. HTTP 共享集合 / sink 负态测试待 E3/E6 用同真实接线补；现有代码 fail-fast，不静默吞错。
3. SDK 可选 uploadID 缺字段时，显式 resume 会用原 URL/key/token 做幂等找回，接受由此产生的额外请求。
4. 旧文件名 / URL / traceback 的既有接受项保持不动。

本卡未修改任何代码，也未把上述取舍包装成"零发现"。

## 5. 未实现领域（明确边界）

- 服务端 HTTP 入口：未实现，`core/` 无任何 HTTP server 依赖。本卡的 HTTP 证据全部来自客户端侧 producer（TcpCapture / CLI），不代表服务端 HTTP 通路已通。
- 真实模型相关的 3 个 skip 属环境缺失，不计覆盖。

## 6. 结论

- **组合测试是否通过**：通过。冻结 SHA 的生产内容与两个独立终审对象逐路径相等，merge 只含两个已审来源、零冲突解法、零第三实现；两个 websockets 版本（15.0.1 与 17.1）全量 263 passed / 3 skipped；并发网络关键文件 5 次重复全绿。
- **验证执行 outcome**：succeeded（本报告已写入 Git，commit + push 完成）。

## 7. 踩坑 / 绕过 / 偏差 / 最贵一步

- **踩坑**：`git diff --name-status` 两个方向必须都跑，只跑一侧无法排除"一侧把新增逻辑塞进对方路径"的情况；两侧集合互补且零交集是本卡的核心判据。
- **绕过**：未绕过。全程 `-p no:cacheprovider` 禁缓存、`uv run --no-project` 隔离 venv、不用 `-k`、不重试实现、不 force / no-verify。
- **偏差**：无。
- **最贵一步**：两次全量测试（各约 100s）与 5 次重复（约 150s），合计约 6 分钟，是本卡耗时主体。

## 8. 状态快照

- `git log -1` / `git show -s --format='%H %P'`：见第 1 节。
- `git status --short`：干净（仅本报告新增文件）。
- `git diff --stat`：见提交。
- `git ls-remote` 非空校验：见提交后回复正文（判存在看 stdout 非空，不拿本地副本当结果）。

## 9. Git 记录

提交见提交正文；`git log --stat -1` 与 `git ls-remote origin card/http-e1-combination-261001` 的 stdout 非空作为推上去的判据。
