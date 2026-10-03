# M4 规划进度

## 当前阶段
planning 完成，产出 `docs/sessions/261003-http-completion/m4-plan.md`（补充设计，无应用代码）。

## 本段结论
- R7 请求侧限额已有真实 HTTP 证据（16 handler / body 2 / mailbox 32 / 32 未完成上传 / 共享预算与 WS 预留 2 / 413 / 408）；三处硬缺口：① 容量三闸（16 GiB source / DB+WAL+SHM 2 GiB / physical free 2 GiB）**零测试**；② **待处理 result 预留未实现**；③ **周期清理完全不存在**（上传 TTL 只有惰性过期，源音频零清理）。
- 拆 4 张卡：C1 容量三闸+result 预留 → C2 周期清理+终态源清理 → C3 端到端用户可见证据；C4 剩余单位反向红验可与 C2 并行。C1/C2 因同改 `http_store.py` 物理串行。
- M6/M7 环境缺口：CI 未装 ffmpeg 导致 `test_http_file_runner.py` 整体 skip（假绿风险）；无 Windows/macOS runner、无音频 fixture、无 systemd unit、无基线脚本。

## 关键决策与已否决方案
决策：保护面只落在 `uploads.state/expires_at`、`jobs.state/terminal_at`、源文件三者；容量闸在 store 层判并沿用既有 507/429 码；清理必须是 http_server 监督链内的真实周期任务；result 预留按 `Job 数 × 64 MiB` 保守计入 DB guard 且不加配置项。
已否决：全协议横向盘点、结构检查当行为主证据、单点内存计数、静默只数内存、自动 retry/重跑、删未登记残留、删 jobs/results、16 GiB 预留随删除释放、把反向红验推迟到 M6。

## 下一步唯一动作
主脑裁决 m4-plan.md §8 的 5 项待决（尤其 partial 源文件是否删、CI 装 ffmpeg 时机），然后据此开 C1 卡。

---

# M4 规划修正（feedback1，dlg-20261003-100700-af5c6b）

## 当前阶段
planning 修正完成。`m4-plan.md` 整体重写（121 行），本文档追加而非覆盖。上一版结论作废，权威版本为修正版。

## 本段结论
- **我的实错 8 处**（主脑 10 条中确认属实）：① 断言「CI 缺 ffmpeg、解码证据整体 skip」——`ci.yml` 安装步骤有 `sudo apt-get install -y ffmpeg`，我上次 grep 只看 pip 行把 apt 行滤掉了；master run `37113170289`（headSha=e066930）实测 425 passed / 3 skipped / 228.99s，解码链在跑，不需前置卡。② 把额度类等值侧统一成 `>=` 拒收——`declared + size > SOURCE_RESERVE_BYTES` 与 `free < 2GiB` 的等值都合法，与「最多 16 GiB」「至少留 2 GiB」一致。③ 把「删除源释放 16 GiB 预留」列进已否决——长期服务会静默永久满。④ T7 写 FAILED 删源后结果 200——`get_result` 对 FAILED 固定 409 `job_failed`。⑤ T8 用纯 RUNNING 构造活跃引用窗口是恒真（RUNNING 无 `terminal_at`，本就不是候选）。⑥ 三态表「测试覆盖」隐含「改坏必红已验证」。⑦ 称 C1/C2「物理必串行」是泛化一支笔。⑧ 称 M7 环境「完全不存在」——模型/音频不在 git 属正常现实。
- **口径修正**：source 预留必须按 source-presence（源是否仍在）计，现行 `WHERE state != 'EXPIRED'` 会漏记仍留在磁盘的 partial；到期终态源删除后必须释放预留，崩溃一致顺序为**先 unlink 后记释放**（反序会漏记）。partial 本轮不自动删物理文件，但必须计入 16 GiB 与物理容量。终态源按 `jobs.terminal_at` 起算 7 天（已裁决，不再列待拍板）。
- **C1 补齐**：预留上界含 R7 明写的 WAL 峰值（结果 payload × 2 作保守上界，常量不加配置，数据只查既有 `jobs`/`results`）；PATCH 与 commit 也要查物理余量（扣掉未写入尾与待处理预留），不足则 507 且不写不 ACK；幂等重放与结果查询在容量满时仍可用。
- **拆卡精简为两张**：C1 容量与结果/WAL 预留、C2 周期终态源清理；用户可见 E2E 证据随实现卡交付，不单开纯验收卡；剩余资源边界反向红验归 M6。C1/C2 无接口消费依赖，可并行；合入按依赖串行 C1→C2。
- **环境实测**：CI 全量 228.99s；本机有 ffmpeg 但**无 aiohttp**，`test_http_file_runner.py`/`test_http_file_tasks.py` 本机收集为 0（本地 361 项 vs CI 425+3）——C1/C2 的 HTTP 层验证须用 CI 或装了 aiohttp==3.14.3 的环境。3 skipped 的具体身份未取到（CI 未开 `-rs`），标未知。`app.py` 不改：`HttpServer.serve()/stop()` 已能消费周期任务。

## 关键决策与已否决方案
决策：额度/余量/槽位三类分别定等值侧（额度 `>`、余量 `<`、槽位 `>=`）；source 预留按 source-presence 且删除后释放；partial 不自动删但计入容量；终态源 `terminal_at` + 7 天无配置项；预留含 WAL 2 倍上界、常量不加配置、不开第二本账；PATCH/commit 也查余量；清理由 `HttpServer.serve/stop` 内真实周期任务消费、不改 app.py；C1/C2 实现并行、合入串行。
已否决：结构检查当行为主证据；把额度等值改成 `>=`；先记释放再 unlink；新增资源表或通用资源账本；用 fixture/grep 宣称改坏必红已验证；把并行说成物理必串行；以「CI 缺 ffmpeg」为前置；以 systemd unit 缺失阻塞 M7；单开纯验收实现卡；为拿三个绿而生产部署。

## 下一步唯一动作
主脑按修正版开 C1 卡（容量与结果/WAL 预留），卡面直接引用 `m4-plan.md` §2 等值侧、§4 预留与余量口径、§7 本机缺 aiohttp 的验证环境约束。