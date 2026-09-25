# 上游关系与同步记录

本仓库面向局域网 ASR 服务端；上游 `HaujetZhao/CapsWriter-Offline` 面向语音输入法。本仓库只摘取引擎层和服务端推理相关提交，客户端、界面和整仓打包/依赖管理不随上游同步。

本次共同祖先（merge-base）：`7d7fac3541a998be10ebf15102f7884a7dd36edb`。

同步流程：先运行 `git fetch upstream`，再对选定提交逐个运行 `git cherry-pick -x <sha>`。`-x` 会把上游原始提交号写入本地提交说明；同步后在本表记录提交标题、取舍和原因。

| 上游提交 | 标题 | 决定 | 理由 |
| --- | --- | --- | --- |
| `84912d5218ee5e51e216c54dc15a1cb0f466eb76` | 修复跨分片 token 拼接吞字符：切点落在 token 内部时按字符拆开 | 摘取 | 不同分片的 token 粒度可能不同；切点落在多字符 token 中间时按字符切分，避免吞掉空格或字符。 |
| `39c33180711209e0bf6c23c302b52dbb8a1e2049` | 增加 aligner 上下文大小 | 摘取 | GGUF aligner 的 `n_ctx` 从 3072 增至 4096；保留本仓独立的环境变量配置。 |
| `47df96a056ebf3baefa61b9ae64b35eed6a3992b` | 更新 llama.py 适配 b10621 | 摘取 | ctypes 结构体和加载失败行为需要匹配 b10621；同时更新对应说明。 |
| `29a0c8ba6f379e394a15b25bd4c45f4b004aa3a1` | 修复 llama_sampler_init_penalties 签名：补上 n_vocab 参数适配新版 llama.cpp | 摘取 | b10621 的 penalties 函数签名需要显式传入词表大小。 |
| `1a332b416322a22ce89a6966bc7ba913b45e6e7d` | 改用 uv 管理环境，Python 3.14 | 不摘取 | 整仓 lock 包含客户端依赖；服务端专用依赖管理放到 T15 统一处理，避免本次把客户端和打包变更一起带入。 |

## llama.cpp 动态库版本

`core/server/engines/llama/llama.py` 将 `LLAMA_BUILD` 固定为 `b10621`。运行时只从 `core/server/engines/llama/bin/b10621/` 加载动态库，不会回退到 `bin/` 根目录。该目录必须有当前平台的 `ggml`、`ggml-base` 和 `llama` 三个核心库；缺目录或缺文件时启动直接报错。发行包中的其它后端动态库也应解压到同一版本目录，供 `ggml_backend_load_all()` 发现。

b10621 官方发行包下载地址格式为 `https://github.com/ggml-org/llama.cpp/releases/download/b10621/<资产文件名>`。当前目标资产：

- Mac Studio / Mac mini（Apple Silicon）：`llama-b10621-bin-macos-arm64.tar.gz`；核心文件为 `libggml.dylib`、`libggml-base.dylib`、`libllama.dylib`。
- AMD Windows x64（Vulkan）：`llama-b10621-bin-win-vulkan-x64.zip`；核心文件为 `ggml.dll`、`ggml-base.dll`、`llama.dll`。CPU 构建的替代资产是 `llama-b10621-bin-win-cpu-x64.zip`，同样使用上述三个核心文件；一个版本目录只放同一种构建的文件，不混合 Vulkan 与 CPU 包。

### 升级与回滚

1. 下载与目标平台匹配的发行包，把包内动态库解压到 `core/server/engines/llama/bin/b10621/`。不要覆盖或删除旧版本文件夹；新代码只会加载 `LLAMA_BUILD` 指定的目录。
2. 启动新代码。日志先出现 `初始化 llama.cpp，跳转至：.../bin/b10621`，绑定成功后才出现 `恢复至目录：...`；两行都出现才表示本次绑定完成。
3. 回滚时切回上一版本代码并重启，保留旧动态库目录即可。首次从本仓 `8a4ebee` 升级时，旧代码仍从 `bin/` 根目录加载 b7798，因此首次升级要把现有根目录库留在原处；b10621 放入独立的 `bin/b10621/`。之后当上一版代码也使用版本目录时，回滚所需的旧版本目录继续保留即可。
