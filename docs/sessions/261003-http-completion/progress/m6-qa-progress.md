阶段：implementing（12 组覆盖表已核对，新增测试按组 TDD 补齐中）
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
下一步唯一动作：补完组 3/10/11 三条新增测试并逐条提交，之后跑两套 websockets 全量。

## 已提交单元

- `tests/test_http_qa_e2e.py::test_real_ws_and_http_share_one_worker_without_key_pollution`
  （组 7）：同一个真 worker 子进程同时收到 `owner_kind=ws`（带真实 socket_id）与
  `owner_kind=http`（socket_id 为空）的段；空 socket 期间 HTTP 照常 DONE；断开的 WS 任务
  从 `state.tasks`/`connection_tasks`/`pending_segments` 消失且 worker 不再收到它的段，
  同窗口 HTTP Job 仍 DONE。运行：`1 passed in 5.39s`。

## 反向红验

见本文件末尾小节（每条关键新断言一次注入相反实现的 scratch 红验）。