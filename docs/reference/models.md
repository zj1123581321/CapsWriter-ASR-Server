# 模型支持

服务端通过 `CW_MODEL_TYPE` 选择 ASR 引擎。模型文件由使用者自行下载并放在仓库的 `models/` 目录中；服务首次启动会检查所选引擎的必要文件。

| `CW_MODEL_TYPE` | 后端 | 内建能力 | 说明 |
| --- | --- | --- | --- |
| `paraformer` | sherpa-onnx / ONNX | ASR、时间戳 | 推荐用于首次部署；CPU 推理；标点由 Punct-CT-Transformer 补足，不支持热词 |
| `sensevoice` | ONNX | ASR、标点、热词、时间戳 | 包含多语言 SenseVoice ONNX 模型 |
| `fun_asr_nano` | ONNX + GGUF | ASR、标点、热词、时间戳 | GGUF 解码器，需要 llama.cpp 动态库 |
| `qwen_asr` | ONNX + GGUF | ASR、标点 | Qwen3-ASR 1.7B；时间戳需额外的 Qwen3 ForcedAligner |
| `qwen_asr_mlx` | Apple MLX | ASR、标点 | 只适用于 Apple Silicon；首次运行可从 Hugging Face 下载模型，或用 `CW_MLX_MODEL` 指向本地模型目录；时间戳需额外的 ForcedAligner |

## 首次选择

首次部署推荐 `paraformer`：仓库已有 sherpa-onnx 依赖路径，模型只需准备 Paraformer 与标点模型，不需要 llama.cpp，也不要求 GPU。依赖安装与目录结构见[首次部署指南](../guides/getting-started.md)。

如需原生标点和热词支持，可选择 `sensevoice`。Fun-ASR-Nano 和 Qwen3-ASR 使用 GGUF 解码器或对齐器，除模型文件外还要准备与当前代码版本匹配的 llama.cpp 动态库。MLX 后端仅适用于 Apple Silicon。

| 引擎 | 模型目录 |
| --- | --- |
| Paraformer | `models/Paraformer/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-onnx/` |
| Punct-CT-Transformer | `models/Punct-CT-Transformer/sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12/` |
| SenseVoice | `models/SenseVoice-Small/Sensevoice-Small-ONNX/` |
| Fun-ASR-Nano | `models/Fun-ASR-Nano/Fun-ASR-Nano-GGUF/` |
| Qwen3-ASR GGUF | `models/Qwen3-ASR/Qwen3-ASR-1.7B/` |
| Qwen3 ForcedAligner | `models/Qwen3-ForcedAligner/Qwen3-ForcedAligner-0.6B/` |

所有模型下载入口和必须保留的解压目录层次见[模型下载说明](../guides/模型下载的若干问题.md)。引擎能力列表对应各引擎实现的 `capabilities` 声明；没有内建标点或时间戳时，服务会尝试加载配置的辅助模型。
