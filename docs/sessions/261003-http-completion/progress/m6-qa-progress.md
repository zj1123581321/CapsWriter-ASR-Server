阶段：implementing → 自测完成，等待主脑验收（C2 合并后需 retarget master 并重跑全量）
结论：base `5134720` 的 qa.md 十二组「未测」严重过期——第 1/2/4/5/6/8/9/10(16k 源)/12 组
已有真实 producer 断言闭合；真实缺口只有 5 条，已在
`docs/sessions/261003-http-completion/m6-qa-evidence.md` 覆盖表逐行写明。
基线：`451 passed, 3 skipped`（3 skip 全是模型/VAD：ForceAligner×2、silero-VAD×1），
无 HTTP 解码类 skip；ffmpeg 在本机 `/usr/bin/ffmpeg` 存在。
决策与否决：复用 `tests/test_http_file_runner.py` 已入库的真实服务骨架（真 HTTP listener +
真 runner + 真 ffmpeg + 真识别子进程），不自造 harness；缺口测试集中放
`tests/test_http_qa_e2e.py`，不改动既有测试的期待值、不放宽任何超时、不改业务代码。
否决：用 mock dict 充当跨进程证据；把「未确认重发字节」伪报为 0；只改文档说已测；
把组 10 的格式矩阵扩成任意编解码组合（本卡只补重采样/降混这一类真实缺口）。
下一步唯一动作：主脑合并 C2 后，把本分支 retarget `master`、合入最新主干并重跑两套 websockets
全量，然后走主审/ready/merge。

## 全量验证（本卡分支 `c346892`+docs，两套 websockets 各跑一遍）

- `websockets==15.0.1`：`457 passed, 3 skipped, 163 warnings in 242.46s (0:04:02)`
- `websockets`（本机解析到 17.2）：`457 passed, 3 skipped, 163 warnings in 236.82s (0:03:56)`
- base `5134720` 同命令是 `451 passed, 3 skipped`，净增 6 条（5 个测试函数，其中重采样矩阵 2 个参数）。
- 3 条 skip 的身份（`-rs`）：`test_aligner_integration.py:53`、`:62`（ForceAligner 后端/模型未安装）、
  `test_segmenter.py:208`（缺 silero-VAD 模型或 onnxruntime）。**没有 HTTP 解码类 skip**，
  本机 `/usr/bin/ffmpeg` 存在，新增解码用例没有 skip 路径（缺 ffmpeg 时显式 assert 失败而不是冒充通过）。
- 新增文件窄跑 5 轮稳定：`6 passed` ×5（websockets 15.0.1 与 17.2 各再跑 1 轮，均绿）。

## 历史 commit 读超时观察（不掩盖为未发生）

原主干 `2919…` 那次并发 commit 读超时的根因仍未知，公开证据见
`fix55-merged-acceptance-evidence.md`。本卡按卡面要求对对应窄 case 各重复 5 轮：

```
tests/test_http_file_tasks.py::test_concurrent_http_commit_respects_budget
tests/test_http_file_tasks.py::test_concurrent_mixed_admission_respects_shared_total
tests/test_http_capacity.py::test_two_real_tcp_commits_race_for_last_storage_capacity
```

实测 5 轮全部 `3 passed`（7.75–7.86s/轮），**未复现**该读超时。RPC/worker 锁时序在本机
没有留下可分析的现场，因此根因仍判定为未知；没有延长任何超时、没有加自动重试来藏红。

## 已提交单元

- `tests/test_http_qa_e2e.py::test_real_ws_and_http_share_one_worker_without_key_pollution`
  （组 7）：同一个真 worker 子进程同时收到 `owner_kind=ws`（带真实 socket_id）与
  `owner_kind=http`（socket_id 为空）的段；空 socket 期间 HTTP 照常 DONE；断开的 WS 任务
  从 `state.tasks`/`connection_tasks`/`pending_segments` 消失且 worker 不再收到它的段，
  同窗口 HTTP Job 仍 DONE。运行：`1 passed in 5.39s`。

## 反向红验

四条注入相反实现的 scratch 红验全部拿到目标 `AssertionError`（脚本保留在
`/tmp/m6-red/`、`tests/red_c_partial_publish.py`、`tests/red_d_auto_retry.py`，**均不入库**）：

| 编号 | 注入 | 目标 | 实测 |
|---|---|---|---|
| A | `ws_recv` 断连清理不 pop `state.tasks`/`pending_segments` | 组 7 | `AssertionError: 等待「断开的 WS 任务从 state.tasks 移除」超时 (30.0s)` |
| B | runner 的 ffmpeg argv 去掉 `-ar 16000` | 组 10 ×2 | `assert 882000 == 320000`（44.1k 立体声）、`assert 160000 == 320000`（8k 单声道） |
| C | `HttpFileRunner.fail_job` 先发布缺段结果再失败 | 组 11 | `assert 'DONE' == 'FAILED'` |
| D | SDK 在 `connection_lost` 上自动重发 | 组 3 | `assert 2 == 1`（commit 真发了两次） |

红验 A 顺带暴露了本卡自己写的一条**恒真断言**（`TaskKey` 下标用错，`key[1]` 是 socket_id），
已修正后重跑 A 才拿到红——这正是「恒真断言等于没写」的实例。

## 交付物

- draft PR：https://github.com/zlxlabs/CapsWriter-ASR-Server/pull/63 （draft，执行器不置 ready）
- 分支 `card/http-m6-qa-261003`，base `5134720`，远端 tip 已用 `git ls-remote` 核对。
- 提交序列：`8337998` 组7 → `611b273` 组1 → `2f9e3ff` 组3 → `6266273` 组10 → `fa5b861` 组11
  → `a112db7` 恒真断言修正 → `c346892` 在场窗口兼容新版 websockets → `86eb4c1` 覆盖表与红验记录。