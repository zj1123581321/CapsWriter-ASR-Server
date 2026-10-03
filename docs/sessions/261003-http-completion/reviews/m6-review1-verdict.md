<!-- delegate-outcome: succeeded -->
# M6 QA 独立首审 verdict

failure-visibility: skipped

## 固定范围与阶段

- 固定对象：`5134720e058e0e9ae3d3feddebf4942f8bf7ed7a..52a748cc60cd73ecfe18a36f7dde0879e77d13f9`，不随分支后续提交移动。
- 风险等级：internal。只审新增 `tests/test_http_qa_e2e.py` 与本范围文档增量；不修改生产代码。
- 阶段：代码首读结论已提交；新版 `qa.md` / evidence 独立交叉核对、测试运行与变异验证仍待完成。本文件当前结论不是最终 verdict。

## 初步代码结论（先于新版 QA/evidence 阅读）

暂未确认代码 finding。新增文件的 5 个测试函数经 2 例采样率矩阵展开为 6 个用例。组 1 通过真实 SDK、TCP 记录代理、服务端源文件和 SQLite 行核对同一次上传；组 3 让服务端真实返回 202 后在 SDK 传输出口丢弃响应，再检查恢复请求序列、Job/Result 行与 worker 收到的段；组 7 真实启动 `ws_recv`、HTTP listener、共享 multiprocessing Queue 和识别子进程，检查两种 owner 同时在场及断线收尾；组 10 用真实 ffmpeg 解码 44.1 kHz 立体声及 8 kHz 单声道输入并检查 worker 收到的分段元数据；组 11 在此前段已有结果后让最后一次识别失败，检查 FAILED、无持久结果且无 final 成功段。

worker 使用可编程假引擎，测试证明传输、序列化、调度和持久化契约，不证明真实 ASR 质量。跨进程 Task 摘要来自 `tests/harness/worker.py` 中 multiprocessing 消费侧实际收到的对象；ffmpeg argv/env 记录由 PATH 首位的真实子进程包装器产生。各条断言的约束力与实际红验尚未验证。

## 待完成阶段

1. 独立核对更新后的 `docs/sessions/261001-http-files/qa.md` 与 `docs/sessions/261003-http-completion/m6-qa-evidence.md` 的 12 组索引和证据主张。
2. 运行新增 6 个用例及必要旧 SDK/CLI/runner 用例；新增文件至少 5 轮；执行 3 个最小反向变异，其中至少 2 个覆盖字节/owner/丢响应，至少 1 个覆盖重采样/末段失败。
3. 在裸 shell 与有环境白名单的真实 `systemd --user` unit 中运行；核对 subprocess env/argv/payload 生产边界，记录范围与未知。
4. 固定范围的最终 OCR 三态、 finding 分诊与未知补全后，更新 verdict/progress，提交、推送并核验实际远端 tip 与工作区。

## OCR 前置

- status：`skipped`
- reason：`no_reviewable_items`
- profile/model：`minimax / MiniMax-M3.1-Flash-Preview`
- envelope：`findings=[]`、`coverage=none`、`verify_status=skipped`。此结果不是“审过且干净”。
