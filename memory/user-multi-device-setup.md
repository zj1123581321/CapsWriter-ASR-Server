---
name: user-multi-device-setup
description: 用户的多设备计算环境 -- Mac Studio、Mac mini、AMD 6800H、RTX 3060，用于 ASR 批量转录
kind: fact
valid_as_of: 2026-09-03
origin: a3633351-baa7-454c-a369-3cc51c014995
---

这条事实是 2026-09-03 在 CapsWriter-Offline-with-AI 上得到的（origin 会话 `a3633351-baa7-454c-a369-3cc51c014995`）。

用户有 4 台计算设备用于 ASR 转录：
- M1 Max Mac Studio, 64GB 内存（家里，跑 MLX Qwen3-ASR，端口 6017）
- M2 Pro Mac mini, 16GB 内存（家里，跑 MLX Qwen3-ASR，端口 6017）
- AMD Ryzen 7 6800H + Radeon 680M 核显 Windows（家里，跑 GGUF Qwen3-ASR，DML+Vulkan，端口 6016）
- RTX 3060 12GB 显存 PC（公司）

约 1 万个音频文件需要批量转录。用户关注充分利用所有设备算力。
