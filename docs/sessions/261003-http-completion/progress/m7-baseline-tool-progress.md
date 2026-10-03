# M7 基线工具进度

更新时间：2026-10-04（UTC+8）

## 当前阶段

- 工具实现前的现场核查已完成；工作树为独立派发分支，起始 HEAD `820c3a2ee4fccc1b44187bd99c40cf99c16e1ca2`，无在途改动。
- 既有 `_baseline_asr.py` 是 WS v1 Base64 JSON 客户端；本卡保留旧入口，新增工具调用 SDK 默认 WS v2 与 HTTP SDK producer。
- 同一私有素材的 MP4 音轨与 16 kHz WAV 解码后均为 1,234,944 samples（77.184 秒）；PCM 相关系数约 0.99999997，但逐样本不相等。字幕末尾 77.040 秒，落在视频 77.184 秒内。
- 字幕没有可核实的人工标注或生成来源记录；仅可标记 `unverified`，不作为正确率真值。
- 当前 Linux checkout 没有模型权重。只读检查确认生产 Mac 上运行的 Paraformer 服务健康、代码版本为 `6b7a2b8`，权重文件非空；这个运行实例只作为权重来源，本卡不会在生产机启动/改动服务。

## 已完成的工具单元

- 完成 `scripts/_baseline_http_ws.py` 初版：固定 loopback、server health/模型/SHA 校验、真实 SDK 默认 WS v2 与 HTTP producer 计量、私有 JSON 输出、无自动重试和 `BASELINE_FAILED`。
- 新增 6 个定向测试及 `tests/fixtures/http_baseline_producer.json`。两条独立红验分别抓到 SRT 未归一化、WS SDK 实际帧遗漏 `model` 字段；修复后定向测试 `6 passed`。
- HTTP CLI 测试在真实本机 TCP 假服务前运行子进程，断言 SDK 实际 POST/PATCH/commit 请求、上传源 body、选项、重发计数、退出码、stdout 隐私和落盘 JSON 字节。假 ASR 返回与正式 Linux 模型测试严格分开。
- 已建立早 draft PR #64；PR 保持 draft。
- 按授权从正在运行的 Paraformer 主机只读复制四个模型/词表文件到私有 Linux cache；四个文件均与之前探测到的路径和非零字节数对应。未对来源主机启动服务、写文件或重启。
- 卡面全量命令使用 Python 3.12.3 与 `websockets==15.0.1` 退出码 0：`452 passed, 3 skipped`，约 4 分钟。3 个 skip 全部是既有 ForceAligner/VAD 模型依赖缺失；没有 HTTP decode skip。
- 同一全量命令将 websockets 约束改为未 pin，实际解析 `17.2`，退出码 0：`452 passed, 3 skipped`，约 3 分 46 秒；skip 与 15.0.1 完全相同，没有 HTTP decode skip。
- Linux 独立服务 venv 已按 `requirements-server-linux.txt` 安装并通过 `uv pip check`；Python 3.12.3、sherpa-onnx 1.13.8、ONNX Runtime 1.30.0，`CPUExecutionProvider` 可用。基线 SHA 的 detached server worktree 已建立，模型目录软链只存在于该隔离树且未进入主分支。
- 隔离服务固定在完整 SHA `820c3a2ee4fccc1b44187bd99c40cf99c16e1ca2` 并只监听 loopback；health 的模型、协议版本、worker 状态和短 SHA 前缀与预期匹配。先后完成 3 秒无语音 WAV 的 WS v2/HTTP smoke，再逐个完成 77.184 秒 WAV 与 MP4 的 WS v2/HTTP 测量；4 项均以 `DONE` 结束，详细匿名数字见 `docs/sessions/261003-http-completion/m7-linux-baseline-evidence.md`。
- WAV：WS v2 1,972,515 JSON UTF-8 字节、HTTP 2,469,966 PATCH body 字节；MP4：WS v2 3,619,954 JSON UTF-8 字节、HTTP 18,974,961 PATCH body 字节。HTTP 重复 body 均为 0。各协议耗时、DONE/token/timestamp 与未核实参考稿差异均已落匿名 evidence；完整正文、路径、音频哈希、字幕和模型响应只留私有目录。

## 收尾状态

- 工具、测试、复现指南、匿名 Linux 实测 evidence 与阶段进度均已落盘；15.0.1 与最新 17.2 全量测试均通过（各 `452 passed, 3 skipped`）。
- Linux 首测全部完成，服务版本与工作目录 SHA 已核对，字幕保持 `unverified`。本次隔离服务已通过自有监控器停止，退出码 0 且无服务进程残留；私有模型、素材与识别结果不进入仓库。
- PR #64 的正文已补上匿名实测证据，保持 open draft；本卡不执行 ready、merge 或 deploy。下一步由 Pi 主脑独立 review，后续三平台正式基线另按最终 merged SHA 执行。

## 已知限制

- 主干 CI 基线派发时不可用，继承红暂时无法判定。
- 本卡公开记录只写匿名 fixture ID、字节数和指标；原文、素材路径/哈希、字幕与 SDK 结果正文留在派发私有目录。
