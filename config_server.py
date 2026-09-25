import os
from pathlib import Path

# 版本信息
__version__ = '2.6'

# 项目根目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# 部署环境覆盖 (Deployment Override)
# master 主线一份代码同时支持 Win/Linux/Mac 多平台、多部署实例：部署差异（引擎、
# 端口、GPU 开关）一律通过环境变量切换，无需改动源码，避免代码漂移。可用变量：
#   --- 部署标识（区分同机多实例）---
#   CW_MODEL_TYPE             ASR 引擎：qwen_asr(默认)/qwen_asr_mlx/fun_asr_nano/sensevoice/paraformer
#   CW_PORT                   WebSocket 监听端口：6016(默认)
#   CW_ADDR                   WebSocket 监听地址：0.0.0.0(默认)
#   --- GPU/后端加速 ---
#   CW_ONNX_PROVIDER          ONNX 后端：CPU(默认)/CUDA/DML/TRT   —— SenseVoice/FunASR/Qwen
#   CW_LLM_USE_GPU            GGUF LLM 是否用 GPU：0(默认)/1       —— FunASR/Qwen
#   CW_VULKAN_FORCE_FP32      Vulkan 强制 FP32：0(默认)/1          —— FunASR
#   CW_ALIGNER_ONNX_PROVIDER  对齐器 ONNX 后端：CPU(默认)/...      —— ForceAligner
#   CW_ALIGNER_LLM_USE_GPU    对齐器 GGUF 是否用 GPU：0(默认)/1    —— ForceAligner
# 示例（Linux GPU 部署）：export CW_ONNX_PROVIDER=CUDA CW_LLM_USE_GPU=1
# 示例（Mac MLX 部署到 6017）：export CW_MODEL_TYPE=qwen_asr_mlx CW_PORT=6017 CW_ALIGNER_LLM_USE_GPU=1
# ---------------------------------------------------------------------------

def _env_str(key: str, default: str) -> str:
    """读取字符串型环境变量，未设置或为空时返回默认值。"""
    val = os.environ.get(key)
    return val if val else default


def _env_bool(key: str, default: bool = False) -> bool:
    """读取布尔型环境变量，接受 1/true/yes/on（不区分大小写）。"""
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in ('1', 'true', 'yes', 'on')


# 服务端配置
class ServerConfig:
    addr = _env_str('CW_ADDR', '0.0.0.0')   # 监听地址，环境变量可覆盖
    port = _env_str('CW_PORT', '6016')       # 监听端口，环境变量可覆盖（同机多实例靠它区分）

    # 语音模型选择：'qwen_asr', 'qwen_asr_mlx', 'fun_asr_nano', 'sensevoice', 'paraformer'
    #   'qwen_asr_mlx' 为 Apple MLX 版 Qwen3-ASR，仅 Apple Silicon (arm64 macOS) 可用
    #   部署时由 CW_MODEL_TYPE 覆盖，无需改源码
    model_type = _env_str('CW_MODEL_TYPE', 'qwen_asr')

    format_num = True       # 输出时是否将中文数字转为阿拉伯数字
    format_spell = True     # 输出时是否调整中英之间的空格

    enable_tray = False        # 是否启用托盘图标功能
    hotwords_path = Path(BASE_DIR) / 'hot-server.txt' # 全局热词配置文件路径

    # 日志配置
    log_level = 'DEBUG'        # 日志级别：'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'
    aligner_idle_timeout = 10  # 对齐引擎空闲多少秒后自动释放显存 (0 表示不释放)

    # 分段切点吸附（避免固定时长盲切剁在连续语音中间，导致对齐器时间戳畸变）
    # 详见 core/server/connection/segmenter.py 模块说明
    seg_cut_snap = True         # 总开关；关闭则回到固定时长盲切
    seg_search_before = 5.0     # 名义切点向前搜索窗口（秒）
    seg_search_after = 5.0      # 名义切点向后搜索窗口（秒）
    seg_max_cut = 72.0          # file 任务弹性延长上限（秒）；段长=切点+overlap，须 ≤ 引擎 chunk_size(80)
    seg_vad_threshold = 0.15    # VAD 人声概率低于该值视为静音帧（嘈杂多人声下 0.2~0.4 仍可能是弱语音，须收紧）
    seg_min_quiet = 0.4         # 可信断点要求的静音持续时长下限（秒），与 ffmpeg silencedetect 惯例一致

    # GPU 预加速配置（有识别任务时，提前调高显存频率，降低延迟，需管理员权限运行）
    gpu_boost_enabled = False                   # 总开关，默认关闭
    gpu_boost_cmd = 'nvidia-smi -lmc 9000'      # GPU 预加速命令，锁定显存频率到9000MHz（根据实际 GPU 调整）
    gpu_unboost_cmd = 'nvidia-smi -rmc'         # GPU 取消预加速命令，恢复显存到默认频率
    gpu_unboost_timeout = 1                     # 空闲多少秒后取消加速

    # 集成显卡兼容性补丁
    # os.environ["GGML_VK_DISABLE_COOPMAT"] = "1"   # AMD集显无法加载 GGUF 模型时尝试
    # os.environ["GGML_VK_DISABLE_F16"] = "1"       # 集成显卡解码有误，强制熔断时尝试




class ModelDownloadLinks:
    """模型下载链接配置"""
    # 统一导向 GitHub Release 模型页面
    models_page = "https://github.com/HaujetZhao/CapsWriter-Offline/releases/tag/models"


class ModelPaths:
    """模型文件路径配置"""

    # 基础目录（锚定到 config_server.py 所在目录，避免子进程 cwd 漂移导致 ONNX 找不到）
    model_dir = Path(BASE_DIR) / 'models'

    # Paraformer 模型路径
    paraformer_dir = model_dir / 'Paraformer' / "speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-onnx"
    paraformer_model = paraformer_dir / 'model.onnx'
    paraformer_tokens = paraformer_dir / 'tokens.txt'

    # 标点模型路径
    punc_model_dir = model_dir / 'Punct-CT-Transformer' / 'sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12' / 'model.onnx'

    # SenseVoice 模型路径，自带标点
    sensevoice_dir = model_dir / 'SenseVoice-Small' / 'Sensevoice-Small-ONNX'
    sensevoice_encoder = sensevoice_dir / 'SenseVoice-Encoder.fp16.onnx'
    sensevoice_decoder = sensevoice_dir / 'SenseVoice-CTC.fp16.onnx'
    sensevoice_tokenizer = sensevoice_dir / 'tokenizer.bpe.model'


    # Fun-ASR-Nano 模型路径，自带标点
    fun_asr_nano_gguf_dir = model_dir / 'Fun-ASR-Nano' / 'Fun-ASR-Nano-GGUF'
    fun_asr_nano_gguf_encoder_adaptor = fun_asr_nano_gguf_dir / 'Fun-ASR-Nano-Encoder-Adaptor.fp16.onnx'
    fun_asr_nano_gguf_ctc = fun_asr_nano_gguf_dir / 'Fun-ASR-Nano-CTC.fp16.onnx'
    fun_asr_nano_gguf_llm_decode = fun_asr_nano_gguf_dir / 'Fun-ASR-Nano-Decoder.q5_k.gguf'
    fun_asr_nano_gguf_token = fun_asr_nano_gguf_dir / 'tokens.txt'
    fun_asr_nano_gguf_hotwords = Path(BASE_DIR) / 'hot-server.txt'

    # Qwen3-ASR 模型路径，自带标点
    qwen3_asr_gguf_dir = model_dir / 'Qwen3-ASR' / 'Qwen3-ASR-1.7B'
    qwen3_asr_gguf_encoder_frontend = qwen3_asr_gguf_dir / 'qwen3_asr_encoder_frontend.onnx'
    qwen3_asr_gguf_encoder_backend = qwen3_asr_gguf_dir / 'qwen3_asr_encoder_backend.onnx'
    qwen3_asr_gguf_llm_decode = qwen3_asr_gguf_dir / 'qwen3_asr_llm.gguf'

    # silero-VAD 模型路径（分段切点吸附用，~2.3MB，缺失时自动降级 RMS 能量检测）
    silero_vad_model = model_dir / 'silero-vad' / 'silero_vad.onnx'

    # Force-Aligner 模型路径
    force_aligner_gguf_dir = model_dir / 'Qwen3-ForcedAligner' / 'Qwen3-ForcedAligner-0.6B'
    force_aligner_gguf_encoder_frontend = force_aligner_gguf_dir / 'qwen3_aligner_encoder_frontend.int4.onnx'
    force_aligner_gguf_encoder_backend = force_aligner_gguf_dir / 'qwen3_aligner_encoder_backend.int4.onnx'
    force_aligner_gguf_llm_decode = force_aligner_gguf_dir / 'qwen3_aligner_llm.q5_k.gguf'



class ParaformerArgs:
    """Paraformer 模型参数配置"""

    paraformer = ModelPaths.paraformer_model.as_posix()
    tokens = ModelPaths.paraformer_tokens.as_posix()
    num_threads = 4
    sample_rate = 16000
    feature_dim = 80
    decoding_method = 'greedy_search'
    provider = 'cpu'            # Paraformer 不支持 GPU 加速，固定 CPU（不随 CW_ONNX_PROVIDER 变化）
    debug = False


class SenseVoiceArgs:
    """SenseVoice 模型参数配置"""

    encoder_path = ModelPaths.sensevoice_encoder.as_posix()
    decoder_path = ModelPaths.sensevoice_decoder.as_posix()
    tokenizer_path = ModelPaths.sensevoice_tokenizer.as_posix()
    itn = True                  # 原生输出阿拉伯数字
    onnx_provider = _env_str('CW_ONNX_PROVIDER', 'CPU')  # ONNX 推理后端 (CPU/CUDA/DML)，环境变量可覆盖
    top_k = 8                   # 热词检索的 CTC 空间大小
    dml_pad_to = 30             # 开启 DirectML 加速时，短音频统一填充到指定长度，有加速效果


class FunASRNanoGGUFArgs:
    """Fun-ASR-Nano-GGUF 模型参数配置"""

    # 模型路径
    encoder_onnx_path = ModelPaths.fun_asr_nano_gguf_encoder_adaptor.as_posix()
    ctc_onnx_path = ModelPaths.fun_asr_nano_gguf_ctc.as_posix()
    decoder_gguf_path = ModelPaths.fun_asr_nano_gguf_llm_decode.as_posix()
    tokens_path = ModelPaths.fun_asr_nano_gguf_token.as_posix()

    # 显卡加速（默认 CPU，部署时由环境变量覆盖）
    onnx_provider = _env_str('CW_ONNX_PROVIDER', 'CPU')   # ONNX 推理后端 (CPU/CUDA/DML)
    llm_use_gpu = _env_bool('CW_LLM_USE_GPU', False)      # 是否启用 GPU 加速 GGUF 模型
    vulkan_force_fp32 = _env_bool('CW_VULKAN_FORCE_FP32', False)  # 强制 FP32（Intel 集显精度溢出时设 1）
    
    # 模型细节
    enable_ctc = True           # 是否启用 CTC 热词检索
    n_predict = 512             # LLM 最大生成 token 数
    n_threads = None            # 线程数，None 表示自动
    similar_threshold = 0.6     # 热词相似度阈值，超过阈值的热词会被传入 llm decoder 的上下文
    max_hotwords = 20           # 传入上下文的热词数量上限
    dml_pad_to = 30             # 开启 DirectML 加速时，短音频统一填充到指定长度，有加速效果
    verbose = False

class Qwen3ASRGGUFArgs:
    """Qwen3-ASR-GGUF 模型参数配置"""

    # 模型路径
    model_dir = ModelPaths.qwen3_asr_gguf_dir.as_posix()
    encoder_frontend_fn = ModelPaths.qwen3_asr_gguf_encoder_frontend.name
    encoder_backend_fn = ModelPaths.qwen3_asr_gguf_encoder_backend.name
    llm_fn = ModelPaths.qwen3_asr_gguf_llm_decode.name

    # 显卡加速（默认 CPU，部署时由环境变量覆盖）
    onnx_provider = _env_str('CW_ONNX_PROVIDER', 'CPU')  # ONNX 推理后端 (CPU/CUDA/DML)
    llm_use_gpu = _env_bool('CW_LLM_USE_GPU', False)     # 是否启用 GPU 加速 GGUF 模型

    # 模型细节
    n_ctx = 2048                # 上下文窗口大小
    chunk_size = 80.0           # 分段长度（秒）
    memory_num = 1              # 记忆段数
    dml_pad_to = 30             # 开启 DirectML 加速时，短音频统一填充到指定长度，有加速效果
    verbose = False


class QwenASRMLXArgs:
    """Qwen3-ASR MLX (Apple Silicon) 模型参数配置。

    公开类属性会被 EngineFactory 原样展开成 MLXEngineConfig(**...) 的 kwargs，
    字段名必须与 MLXEngineConfig 的 dataclass 字段严格对应。
    """
    # 模型来源：HF repo id（首次运行联网下载）或本地模型目录绝对路径
    # 默认 1.7B 与 GGUF 部署对齐（嘈杂/多人声素材文本质量明显优于 0.6B）；
    # 内存紧张的机器可用 CW_MLX_MODEL=Qwen/Qwen3-ASR-0.6B 降回
    model = _env_str('CW_MLX_MODEL', 'Qwen/Qwen3-ASR-1.7B')
    dtype = _env_str('CW_MLX_DTYPE', '') or None   # 'float16' / 'bfloat16' / 空=库默认
    chunk_size = 80.0                              # 单段最大音频时长（秒），超出截断
    verbose = False


class ForceAlignerGGUFArgs:
    """Force-Aligner-GGUF 模型参数配置"""

    # 模型路径
    model_dir = ModelPaths.force_aligner_gguf_dir.as_posix()
    encoder_frontend_fn = ModelPaths.force_aligner_gguf_encoder_frontend.name
    encoder_backend_fn = ModelPaths.force_aligner_gguf_encoder_backend.name
    llm_fn = ModelPaths.force_aligner_gguf_llm_decode.name

    # 显卡加速（对齐器独立开关，默认 CPU；对齐器调用稀疏，GPU 收益有限）
    onnx_provider = _env_str('CW_ALIGNER_ONNX_PROVIDER', 'CPU')  # ONNX 推理后端 (CPU/CUDA/DML)
    llm_use_gpu = _env_bool('CW_ALIGNER_LLM_USE_GPU', False)     # 是否启用 GPU 加速 GGUF 模型

    # 对齐细节
    n_ctx = 4096                # 上下文窗口大小
    dml_pad_to = 30             # 开启 DirectML 加速时，短音频统一填充到指定长度，有加速效果
