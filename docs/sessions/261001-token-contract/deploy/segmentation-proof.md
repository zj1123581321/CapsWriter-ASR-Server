# 生产 file 小末帧分段覆盖证明

## 结论

三处生产 server clone 均运行 `6b7a2b8`，`config_server.py` 干净；按实际服务解释器、工作目录和白名单部署变量导入的配置都是 `seg_cut_snap=True`、`search_before=5`、`search_after=5`、`max_cut=72`。真实 `PcmSegmenter` 重放 5 秒后追加 0.05 秒时，非 final 提交数为 0，final 段为 80,800 samples / 5.05 秒，`final_samples < 1600` 为 false。

## 源码边界

- `config_server.py:74-77` 定义 `ServerConfig` 的四个分段值；它们不是环境变量覆盖项。运行时模型类型来自部署环境。
- `core/server/connection/ws_recv.py:74-81` 按运行时模型类型取单段上限；`154-166` 将实际配置传给 `PcmSegmenter.configure` 和 `drain_ready`。
- `core/server/segmenter.py:135-196` 是切点吸附路径。file 非 final 段先检查 `duration < nominal + search_after + overlap`；本例为 `5 < 5 + 5 + 0`，直接返回，不调用切点查找。final flush 的 5.05 秒仍低于段上限，剩余数据由 `final_segment` 原样作为 final 段返回（`106-115`）。
- `core/server/worker/audio.py:25-30` 才是 worker 的短段判断：worker 分段少于 1600 samples 时跳过。上传帧边界不等同于这个 worker 分段。

## 实际消费配置

只读核对了三个分段服务；proxy 不自行分段。服务的解释器、工作目录、配置模块绝对路径和分段器模块绝对路径留在权限为 600 的受限白名单记录中，本公开文档不记录私人路径、账户或地址。

| 服务 | Python | 部署模型类型 | 端口 | HEAD | `config_server.py` |
|---|---|---|---|---|---|
| Studio-Paraformer | 3.11 | `paraformer` | 6016 | `6b7a2b8` | clean |
| Studio-MLX | 3.12 | `qwen_asr_mlx` | 6017 | `6b7a2b8` | clean |
| Windows-Qwen | 3.11 | `qwen_asr` | 6016 | `6b7a2b8` | clean |

Windows 活跃计划任务的启动脚本将 `CW_MODEL_TYPE` 设为 `qwen_asr`；账号级和机器级同名覆盖均未发现。Studio 的 `CW_MODEL_TYPE` 和 `CW_PORT` 从对应 PM2 实例按白名单读取。成功重跑只传入各自白名单变量；此前一次远程命令因引号错误曾将完整进程环境回显到工具记录，随后已停止该命令，回显未写入仓库文件。具体敏感内容仅记在私有处置报告中。

## 真实分段器重放

外部脚本以权限 600 保留。它使用各服务的 Python 解释器和 clone 工作目录，只带入白名单 `CW_MODEL_TYPE`、`CW_PORT`，导入 clone 中真实 `ServerConfig` 与 `PcmSegmenter`，生成 Float32 静音样本，不读取音频或加载模型。Studio 的隔离探针环境缺少 `Rich` 日志依赖，因此脚本用标准库 logger 绕过 `core.server` 包初始化，再导入真实 `core.server.segmenter` 源文件；分段逻辑本身未替换。切点吸附配置传入非空哨兵 finder；若 5 秒样本意外进入查点逻辑，脚本立即失败，实际重放未调用 finder。

| 重放形态 | 5 秒后非 final 提交数 | final samples | final 时长 | final 是否小于 1600 |
|---|---:|---:|---:|---|
| 服务实际配置 `cut_snap=True`，再追加 800 samples | 0 | 80,800 | 5.05 秒 | 否 |
| 私有控制 `cut_snap=False`，再追加 800 samples | 1 | 800 | 0.05 秒 | 是 |

正负两种实际 JSON 结果分别记录在三份服务白名单 JSON 内。控制形态只用于证明判据可以区分切段路径，没有修改生产配置或提交识别任务。

## 覆盖范围

- 生产 5.05 秒烟测证明 file final 契约和 800-sample 小末帧上传成功；在已核实的生产配置下，它没有覆盖 worker `<1600` 短段分支。
- `<1600` 分支此前已由本机真实 WebSocket / TaskPipeline 子进程与 base 红验锁定；本次重放没有替代或声称复现那条覆盖。
- 保留原生产健康、版本、普通 file、mic、proxy 和稳定窗口成功结果；保留历史阻塞与 Windows PID 归属复核。本次只读，未重启服务、调整配置或发送 ASR 任务，也不据此宣称代码故障。
