# coding: utf-8
"""
冒烟验证：MLX Qwen3-ASR 能否在 multiprocessing spawn 子进程里正常初始化 Metal 并推理。

背景（见 docs/archive/research/mlx-qwen-integration-plan.md 评审报告 架构问题#2）：
  独立版 mlx_ws_server 跑在主进程(asyncio+线程池)，而主线把 ASR 跑在
  multiprocessing 子进程里(process_manager.py)。macOS 默认 spawn(干净子进程)，
  Metal 理论上能在子进程全新初始化，但这条路径从未真机验证过 —— 本脚本就是那个 gate。

只依赖 mlx-qwen3-asr，不依赖主线 server 全套依赖，可在装了 mlx 的任意 venv 跑：
    .venv-mlx/bin/python scripts/_smoke_mlx_subprocess.py
首次运行会从 HuggingFace 下载 Qwen3-ASR-0.6B 权重；如需离线，设 CW_MLX_MODEL 指向本地目录。
"""
import os
import sys
import time
import multiprocessing as mp


def _worker(model: str, q):
    """子进程入口：import + Session + transcribe 一段静音，把结果回传父进程。"""
    try:
        import numpy as np
        import platform
        info = {"machine": platform.machine(), "system": platform.system(),
                "start_method": mp.get_start_method()}

        t0 = time.time()
        from mlx_qwen3_asr import Session
        sess = Session(model=model)
        info["load_sec"] = round(time.time() - t0, 2)

        # 1 秒静音，仅验证子进程内推理管线不崩
        audio = np.zeros(16000, dtype=np.float32)
        t1 = time.time()
        result = sess.transcribe(audio, return_timestamps=False)
        info["infer_sec"] = round(time.time() - t1, 3)
        info["text"] = getattr(result, "text", None)
        info["ok"] = True
        q.put(info)
    except Exception as e:  # noqa: BLE001
        import traceback
        q.put({"ok": False, "error": repr(e), "trace": traceback.format_exc()})


def main():
    model = os.environ.get("CW_MLX_MODEL", "Qwen/Qwen3-ASR-0.6B")
    # 显式用 spawn，复现主线 macOS 子进程行为（process_manager.py 未设 start_method，
    # 而 macOS 默认即 spawn）
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    p = ctx.Process(target=_worker, args=(model, q))
    print(f"[smoke] 拉起 spawn 子进程，model={model} …（首次会联网下载权重）")
    p.start()
    p.join()

    if not q.empty():
        info = q.get()
    else:
        info = {"ok": False, "error": "子进程未回传任何结果", "exitcode": p.exitcode}

    print(f"[smoke] 子进程 exitcode={p.exitcode}")
    for k, v in info.items():
        if k != "trace":
            print(f"[smoke]   {k}: {v}")

    if not info.get("ok"):
        print("[smoke] ❌ 失败")
        if info.get("trace"):
            print(info["trace"])
        sys.exit(1)
    print("[smoke] ✅ spawn 子进程内 MLX/Metal 初始化 + 推理通过")


if __name__ == "__main__":
    main()
