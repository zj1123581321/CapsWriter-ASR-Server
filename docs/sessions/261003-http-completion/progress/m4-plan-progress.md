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