<!-- delegate-outcome: succeeded -->
# M6 十二组 HTTP QA 独立首审结论

failure-visibility: p2-only

## 结论与固定范围

- 审查范围固定为 `5134720e058e0e9ae3d3feddebf4942f8bf7ed7a..52a748cc60cd73ecfe18a36f7dde0879e77d13f9`；不跟随后续分支提交。
- 结论：发现 1 项 P2（M6R1-1），没有确认 P1。新增测试覆盖了原列出的 5 条缺口，但第 10 组未证明 worker 收到的 PCM 样本值正确；同长度静音变异仍通过。第 1 组相反实现红验、真实容量级压测和真实部署/识别质量依旧未知。
- 此结论针对 internal QA 测试增量，不代表真实 ASR 质量、三平台部署或生产可用性验收。实现源码没有生产改动，也不消除测试的假绿缺口。
- 已先提交代码首读的初步结论，再独立对照更新版 `qa.md` 与 evidence 索引；作者所报红绿结果未被当作本轮运行证据。

## 四个审查问题

1. **新用例是否走到了真实 producer 边界？** 是。组 1 走 SDK、实际 HTTP 请求、真实服务端源文件和 SQLite；组 3 经过真实 HTTP response 丢弃与显式恢复；组 7 同时运行真 WS/HTTP、共享 multiprocessing Queue 和 worker 子进程；组 10 使用真实 ffmpeg；组 11 检查末段失败后的 Job、结果表和 HTTP 结果端点。worker 的记录夹具来自子进程实际收到的 Task/发出的 Result，不是测试手造的消费侧字典。
2. **跨进程 payload 与持久化边界是否留下可核验证据？** 大部分是。组 1 比较本次 PATCH body 与源字节，并比对服务端落盘 SHA-256/长度；组 3 检查真实请求序列、Job 行及 worker 实收段数；组 7 检查两类 owner 实收及结果隔离；组 10 有真实 argv/env 包装器、分段长度与样本总数。但组 10 没有把实际 PCM 内容与独立解码字节对应，因此仍有本 verdict 的 P2。
3. **关键断言经反向变异后是否能在目标位置报错？** 是，完成了三个独立有效 AssertionError 红验，分别覆盖丢响应自动重发、owner 记录断言和去掉重采样参数；另做同长度零 PCM 变异，明确复现组 10 假绿。owner 变异只改测试 recorder 记录的 WS `owner_kind`，没有改 worker 运行逻辑，故只作为 recorder 断言有约束力的证据。
4. **测试结果能支持多大范围的结论？** 仅支持这里列出的 synthetic 输入、假识别引擎、真实服务/子进程/ffmpeg 路径。新增文件裸 shell 五轮和真实 `systemd --user` 白名单环境均 6/6 通过；必要旧用例选择 32/32 通过。它们不能推导真实识别质量或平台部署已验收。OCR 前置为 `skipped`，不能算作审过且干净。

## Finding

### M6R1-1 — P2：第 10 组没有锁住 worker 收到的 PCM 样本内容

- **违反的不变式**：base `docs/sessions/261001-http-files/qa.md` 第 10 组要求 producer 输出有界 16 kHz mono f32 PCM 段；任务卡明确要求跨进程断言 producer 实际发出的 payload 字节，消费侧大小计算不够。
- **代码位置**：`tests/test_http_qa_e2e.py:621-652`；跨进程记录器 `tests/harness/worker.py:43-64` 只保存 `data_bytes`、样本数与前缀摘要，没有保留/校验 `Task.data` 的 PCM 内容摘要。
- **实际反证**：scratch 中只把 `core/server/http_file_runner.py` 的 `yield block` 变为 `yield bytes(len(block))`，也就是同长度全零 PCM。目标两种重采样参数用例仍为 `2 passed`，日志 `/tmp/m6-review1-mutation-zero-pcm.log`。因此长度、offset、采样率标签、样本总数和真 ffmpeg 对照总长度可以全绿，实际送入 worker 的音频内容仍可能错误。
- **分级**：P2。44.1 kHz 立体声与 8 kHz 单声道属于本测试实际构造的输入类别，但没有真实用户输入分布数据；静音会破坏识别结果，但本轮没有证明生产 decoder 正在静音化。依 P1 两问，真实使用触发率未知，生产后果尚未由真实路径观测，不能升 P1。
- **建议验证契约**：让 producer fixture 保存 worker 实收 PCM 的真实字节摘要，并与独立真 ffmpeg 产生的 16 kHz mono f32 输出建立可核对关系；当前此建议是缺口描述，不在本审查卡修改实现。

## 十二组独立证据索引

| 组 | 生产边界及关键测试 | 本轮判定与剩余未知 |
|---|---|---|
| 1 二进制上传 | `tests/test_http_qa_e2e.py::test_real_sdk_upload_bytes_match_server_disk_sha`：真实 SDK PATCH body 与源字节逐字节一致，服务端 source 文件长度/SHA 与源一致；既有 TCP 抓包与服务端落盘用例作为两端契约 | 新端到端链闭合。该链的相反实现红验未跑，标未知；真实生产服务/录音未测。 |
| 2 脱离连接 | `tests/test_http_file_runner.py:341-380`，上传连接关闭后另一连接取得完整 worker Result；另有 `tests/test_http_file_tasks.py:451` 重启领取 | 既有断言覆盖，未发现本轮新增缺口。 |
| 3 幂等提交 | `tests/test_http_qa_e2e.py::test_lost_commit_response_recovers_with_exactly_one_recognition`；响应实际发出后丢弃，显式恢复，核请求序列、SQLite job 数和 worker 实收段数 | 新端到端恢复链闭合。SDK 自动重发变异命中目标断言 `assert 2 == 1`。 |
| 4 续传 offset | `tests/test_http_file_tasks.py:272-310` 实际 PATCH 重发字节；`tests/test_http_supervision.py:503-530` 崩溃后核对确认 offset 与未确认磁盘尾；`tests/test_http_store.py:161` | 有真实文件/请求与 crash 窗口证据；未发现本轮新增缺口。 |
| 5 上传跨重启 | `tests/test_http_supervision.py:503`；`tests/test_http_file_runner.py:747/784/775` SIGTERM/SIGKILL 后新进程核状态，确认不自动重跑 | 独立服务进程和 SQLite 路径有证据；未发现本轮新增缺口。 |
| 6 结果跨重启 | `tests/test_http_supervision.py:577` 新进程读取旧进程写入的真实 Result；`tests/test_http_cleanup.py:276/488` 源清理后仍可领结果 | 结果 producer、持久化和读取路径有证据；未发现本轮新增缺口。 |
| 7 双 owner 并发 | `tests/test_http_qa_e2e.py::test_real_ws_and_http_share_one_worker_without_key_pollution`，真 WS 与 HTTP 经同一 worker Queue/Result Queue；核 key 隔离、空 socket 并行、WS 断开收尾 | 新增真实共享 worker 用例覆盖。owner recorder 字段逆变异在目标断言报 AssertionError；这条变异不等于改写实际路由逻辑。 |
| 8 取消与 I/O | `tests/test_http_file_tasks.py:619-731` 两种完成顺序各多轮核磁盘字节、DB offset 与 mailbox 锁占用 | 真实 I/O worker 与取消交错断言；未发现本轮新增缺口。 |
| 9 final/offset/额度/准入 | `tests/test_http_file_tasks.py:504/568/742/1347/1427/1464/1538/1565`、`tests/test_http_capacity.py`、`tests/test_http_release_invariant.py` | 软件类别/等值边界与共享准入有覆盖；1 GiB、16 GiB、2 GiB 等物理容量只按缩小常量测，真实体量压测未知。 |
| 10 解码与 PCM | `tests/test_http_qa_e2e.py::test_resampled_sources_produce_bounded_16k_mono_f32_segments`（2 参数）；既有 `tests/test_http_file_runner.py:341/405/415-426` 核 ffmpeg argv/env、段数、并发与临时文件 | 真 ffmpeg 输入矩阵和有界格式长度被测；同长度静音变异仍 2 passed，故 PCM 样本内容不变量缺失（M6R1-1）。 |
| 11 失败与监督 | `tests/test_http_qa_e2e.py::test_final_segment_failure_fails_job_without_publishing_partial`；既有 `tests/test_http_file_runner.py:491/521/539/817/1127/1429/747` 覆盖其他失败与停机 | 新末段失败用例检查已发前段之后仍 FAILED、无结果行、GET 409。去掉重采样之外的独立有效最终失败变异未额外重跑，本轮采用新用例实测与既有索引。 |
| 12 清理与兼容 | `tests/test_http_cleanup.py:142/276/553/778`、`tests/test_http_capacity.py:311/912`；旧 WS 基线、背压、health、错误码、协议与切段用例 | 清理持久结果及兼容入口有既有覆盖；真实三平台和真实 ASR 字节/质量仍未知。 |

## 运行、变异与环境证据

- Python 3.12.3；`/usr/bin/ffmpeg`。全部 Python 测试使用 `uv run --no-project --no-config --isolated --python 3.12` 与 numpy、rich、websockets 15.0.1、colorama、pytest 9.1.1、soundfile、pytest-asyncio 1.4.0、aiohttp 3.14.3、httpx 0.28.1；没有写入 main venv。
- 裸 shell：`tests/test_http_qa_e2e.py` 连跑五轮，每轮 `6 passed`、无 skip；`tests/test_http_client.py` 与选定 runner、tasks、supervision、cleanup 用例合计 `32 passed`。日志分别在 `/tmp/m6-review1-round-{1..5}.log` 与 `/tmp/m6-review1-existing-targeted.log`。
- systemd：真实临时单元 `codex-m6review1-systemdprobe-261004-1056421.service` 在 `env -i` 白名单环境运行同一新增文件，`6 passed`、无 skip；子进程 PID `1056442`，MainPID `1056430`，cgroup `/user.slice/user-1000.slice/user@1000.service/app.slice/codex-m6review1-systemdprobe-261004-1056421.service`，`RuntimeMaxSec=300s`、`KillMode=control-group`、`TasksMax=64`。单元自然退出成功，没有停止 delegate 或其他 unit。`uv` 会为子进程前置隔离环境 bin 目录；检查确认允许的 PATH 后缀完整保留，且 HTTP/HTTPS/ALL proxy 变量与 `CW_HTTP_PORT` 不存在。
- 有效逆变异：SDK 在 commit `TransportError` 后自动重发，目标用例 AssertionError `2 == 1`（`/tmp/m6-review1-mutation-retry-commit.log`）；去掉 ffmpeg `-ar 16000`，两个参数化用例在样本数断言 AssertionError（`/tmp/m6-review1-mutation-no-resample.log`）；仅将 worker recorder 对 WS `owner_kind` 的记录改错，组 7 owner 断言 AssertionError（`/tmp/m6-review1-mutation-owner-observer-target2.log`）。三者均是目标断言红，不是导入/收集错误。
- 同长度全零 PCM 变异仍绿：`/tmp/m6-review1-mutation-zero-pcm.log`，作为 M6R1-1 的直接证据。两次改变实际 owner/socket 的尝试导致 worker 异常退出并卡住 fixture teardown，已按各自 pytest PID 终止；不计作有效红验，也未影响其他 unit。
- OCR 前置完整 JSON envelope：`status=skipped`、`reason=no_reviewable_items`、`findings=[]`、`coverage=none`、`verify_status=skipped`，profile/model 为 `minimax / MiniMax-M3.1-Flash-Preview`。按三态规则这是未扫描，不是 clean。
- 基线 `gh api request failed`，因此 inherited-red 状态未能判定。本轮无生产源码/配置/部署修改。
