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

## 接下来

1. 先写 SDK 实际请求/帧计量、隐私约束、失败状态和归一化 fixture 测试，完成两条独立红验后逐步实现。
2. 已提交从空环境复现说明；15.0.1 与当前最新 17.2 的全量命令均通过。
3. 运行隔离的 `127.0.0.1` 服务；先合成 smoke，再用 WAV 与 MP4 各自串行测 HTTP 与 SDK WS v2。
4. 记录真实指标、未知项和 SHA；更新 draft PR，不标 ready。

## 已知限制

- 主干 CI 基线派发时不可用，继承红暂时无法判定。
- 本卡公开记录只写匿名 fixture ID、字节数和指标；原文、素材路径/哈希、字幕与 SDK 结果正文留在派发私有目录。
