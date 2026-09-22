---
name: capswriter-asr-deployment
description: ASR Server 部署信息 -- Mac Studio、Mac mini、AMD 6800H Windows 的进程管理、路径、Tailscale IP
kind: reference
valid_as_of: 2026-09-04
origin: a3633351-baa7-454c-a369-3cc51c014995
---

这条参考来自 CapsWriter-Offline-with-AI（origin 会话 `a3633351-baa7-454c-a369-3cc51c014995`）。

| 机器 | Tailscale IP | SSH 用户 | 进程管理 | 本地路径 | 引擎 | GPU 加速 | 端口 |
|------|-------------|---------|---------|---------|------|---------|------|
| Mac Studio | 100.121.0.34 | zhanglixing | pm2 (qwen-asr-server) | /Users/zhanglixing/Production/qwen_asr_server（git clone） | Qwen3-ASR-1.7B (MLX) | Metal | 6017 |
| Mac Studio | 100.121.0.34 | zhanglixing | pm2 (capswriter-server) | /Users/zhanglixing/Production/capswriter_server_main（git clone，独立目录） | Paraformer | — | 6016 |
| Mac mini | 100.66.238.15 | zlx | pm2 (qwen-asr-server) | /Users/zlx/Production/qwen_asr_server（**非 git**，文件覆盖部署） | Qwen3-ASR-1.7B (MLX) | Metal | 6017 |
| AMD 6800H (Win) | 100.119.132.40 | zlx | Windows 计划任务 (CapsWriter-Server) | D:\MyFolders\Prod\CapsWriter-Offline-with-AI（git clone） | Qwen3-ASR-1.7B (GGUF) | DML+Vulkan (Radeon 680M) | 6016 |

2026-07-05：MLX 默认模型 0.6B→1.7B（commit ed8ab58），三台全部跑 1.7B。内存：mini 16GB / Studio 64GB。大文件互传走局域网直连（临时 `python3 -m http.server` + curl，40~70MB/s），勿经开发机 Tailscale 中转（0.3~1.4MB/s）。6800H 重启务必用新版 `run_server.bat`（启动前自带 taskkill 清残留，详见 [[project-seg-cut-snap]]）。

2026-07-06：keepalive 修复（commit f61a1a7，server/client 加 `ping_interval=None`，根治超长上传被 1011 误杀）已部署 **Mac Studio capswriter-server (6016)** 并验证通过；**其余实例待同步**：Studio qwen_asr_server (6017)、mini qwen_asr_server (6017，非 git 需文件覆盖)、6800H (6016) 仍跑旧代码，同样存在此 bug。Studio SSH 直连 22 端口从开发机不通（Tailscale 数据层被挡），需经 mini (100.66.238.15) 跳板：`ssh -J zlx@100.66.238.15 zhanglixing@192.168.31.222`。

### 进程管理备注
- **Mac (pm2)**: 非交互 SSH 需要扩展 PATH（`/usr/local/bin` 或 `$HOME/.nvm/versions/node/*/bin`）。
- **AMD 6800H (Windows 计划任务)**: 任务名 `CapsWriter-Server`，AtLogon 触发，失败自动重试 3 次（间隔 1 分钟）。启动脚本 `run_server.bat`（设 CW_ONNX_PROVIDER=DML, CW_LLM_USE_GPU=1）。llama.cpp 版本锁 b7798 (Vulkan)。

### MLX 1.7B 实测 (77s 真实嘈杂音频，2026-07-05，footprint 口径)
- **Mac mini (16GB)**: 听写 RTF 0.214；文件转录(含 CPU 对齐器) RTF 0.282；worker 常驻 ~4.7GB、文件转录峰值 **10GB**（16GB 机器偏紧，注意别同时跑重活）
- **Mac Studio (64GB)**: 听写 RTF 0.127；文件转录 RTF 0.163；worker 常驻 ~5.2GB、峰值 12GB
- 对比 0.6B：常驻 3.3GB/峰值 7GB，mini 文件转录 RTF 0.122~0.131
- 重启后首个任务 RTF 偏高（Metal 编译+对齐器冷加载），第二个起为热值
- 两台 Mac 的 pm2（ecosystem.config.cjs）均已设 `CW_ALIGNER_LLM_USE_GPU=1`，上述 RTF 已是对齐器走 Metal 的成绩，无进一步现成优化空间。（教训：查 pm2 环境变量要 `grep "^CW_"` 全量看，别用部分前缀漏项）

### RTF 基准 (合成音频，2026-06-30 实测)
AMD 6800H + Radeon 680M 核显，DML+Vulkan 模式：
- 10s 音频: RTF 0.106~0.143 (延迟 1.0~1.4s)
- 20s 音频: RTF 0.289 (延迟 5.8s)
- 60s 音频: RTF 0.265 (延迟 15.9s)
- 纯 CPU 对比: RTF 约 0.27~0.31，GPU 短音频有 ~50% 加速

### Proxy 部署
- **Mac Studio** pm2 进程名 `capswriter-proxy`，端口 6020，路径 `/Users/zhanglixing/Production/capswriter_proxy`（注意：与 ASR Server 的 `qwen_asr_server` 是独立目录，同一个 git repo 的不同 clone）
- 状态查看：`curl http://100.121.0.34:6020/status` 或浏览器打开 `http://100.121.0.34:6020/status?html`
- 更新部署：SSH 到 Mac Studio → `cd /Users/zhanglixing/Production/capswriter_proxy && git pull && pm2 restart capswriter-proxy`

代理只负载均衡 Qwen 引擎（Mac 走 6017，Windows 走 6016），不代理 Paraformer。

关联 [[user-multi-device-setup]]、[[project-proxy-architecture]]。
