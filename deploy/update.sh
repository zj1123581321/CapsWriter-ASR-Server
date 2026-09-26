#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
    printf '用法: %s <git-ref>\n' "$0" >&2
    exit 2
fi

ref="$1"
deploy_python="${DEPLOY_PYTHON:?请设置 DEPLOY_PYTHON}"
process_name="${DEPLOY_PROCESS_NAME:?请设置 DEPLOY_PROCESS_NAME}"
port="${DEPLOY_PORT:?请设置 DEPLOY_PORT}"
model_type="${CW_MODEL_TYPE:?请设置 CW_MODEL_TYPE}"
health_timeout="${DEPLOY_HEALTH_TIMEOUT:-300}"
health_interval="${DEPLOY_HEALTH_INTERVAL:-2}"

if [[ ! "$port" =~ ^[0-9]+$ ]] || (( port < 1 || port > 65535 )); then
    printf 'DEPLOY_PORT 必须是 1 到 65535 之间的整数\n' >&2
    exit 2
fi
if [[ ! "$health_timeout" =~ ^[1-9][0-9]*$ || ! "$health_interval" =~ ^[1-9][0-9]*$ ]]; then
    printf '健康检查超时和间隔必须是正整数\n' >&2
    exit 2
fi

case "$model_type" in
    qwen_asr|qwen_asr_mlx|fun_asr_nano|sensevoice|paraformer|proxy) ;;
    *) printf '不支持的 CW_MODEL_TYPE: %s\n' "$model_type" >&2; exit 2 ;;
esac

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd -- "$script_dir/.." && pwd)"
if [[ "$deploy_python" == */* && "$deploy_python" != /* ]]; then
    deploy_python="$repo_dir/$deploy_python"
fi
if [[ ! -f "$deploy_python" || ! -x "$deploy_python" ]] && ! command -v -- "$deploy_python" >/dev/null 2>&1; then
    printf 'DEPLOY_PYTHON 不存在或不可执行: %s\n' "$deploy_python" >&2
    exit 2
fi
platform="$(uname -s)"
case "$platform:$model_type" in
    Darwin:qwen_asr|Darwin:qwen_asr_mlx|Darwin:fun_asr_nano|Darwin:sensevoice|Darwin:paraformer|Darwin:proxy)
        requirements_file="requirements-server-macos.txt"
        ;;
    Linux:qwen_asr|Linux:fun_asr_nano|Linux:sensevoice|Linux:paraformer|Linux:proxy)
        requirements_file="requirements-server-linux.txt"
        ;;
    Linux:qwen_asr_mlx)
        printf 'qwen_asr_mlx 只能部署在 macOS\n' >&2
        exit 2
        ;;
    *) printf 'update.sh 不支持当前系统和模型: %s / %s\n' "$platform" "$model_type" >&2; exit 2 ;;
esac

cd "$repo_dir"
git fetch --tags origin
git checkout --detach "$ref"
expected_git_sha="$(git rev-parse --short HEAD)"

case "$model_type" in
    qwen_asr|qwen_asr_mlx|fun_asr_nano)
        llama_build_info="$repo_dir/core/server/engines/llama_build_info.py"
        if [[ ! -f "$llama_build_info" ]]; then
            printf 'llama 预检跳过：%s 无 llama_build_info.py\n' "$ref"
        else
            llama_build="$(sed -nE 's/^[[:space:]]*LLAMA_BUILD[[:space:]]*=[[:space:]]*"([^"]+)"[[:space:]]*$/\1/p' "$llama_build_info")"
            if [[ -z "$llama_build" ]]; then
                printf '无法从 %s 解析 LLAMA_BUILD\n' "$llama_build_info" >&2
                exit 1
            fi
            llama_lib_dir="$repo_dir/core/server/engines/llama/bin/$llama_build"
            case "$platform" in
                Darwin)
                    llama_files=(libggml.dylib libggml-base.dylib libllama.dylib)
                    llama_asset="llama-${llama_build}-bin-macos-arm64.tar.gz"
                    ;;
                Linux)
                    llama_files=(libggml.so libggml-base.so libllama.so)
                    llama_asset="llama-${llama_build}-bin-ubuntu-vulkan-x64.tar.gz"
                    ;;
                *)
                    printf 'llama 预检不支持当前系统: %s\n' "$platform" >&2
                    exit 2
                    ;;
            esac
            missing_llama_files=()
            for llama_file in "${llama_files[@]}"; do
                if [[ ! -f "$llama_lib_dir/$llama_file" ]]; then
                    missing_llama_files+=("$llama_file")
                fi
            done
            if (( ${#missing_llama_files[@]} > 0 )); then
                printf 'llama 预检失败：%s 缺少文件：%s；请从 llama.cpp release 获取资产：%s\n' \
                    "$llama_lib_dir" "${missing_llama_files[*]}" "$llama_asset" >&2
                exit 1
            fi
        fi
        ;;
esac

"$deploy_python" -m pip install -r "$requirements_file"

pm2 restart "$process_name"

health_file="$(mktemp)"
trap 'rm -f -- "$health_file"' EXIT
health_url="http://127.0.0.1:${port}/health"
health_status="000"
health_deadline=$((SECONDS + health_timeout))
while (( SECONDS < health_deadline )); do
    request_timeout=$((health_deadline - SECONDS))
    if (( request_timeout > 5 )); then
        request_timeout=5
    fi
    if health_status="$(curl --silent --show-error --connect-timeout "$request_timeout" --max-time "$request_timeout" \
        --output "$health_file" --write-out '%{http_code}' "$health_url")"; then
        if [[ "$health_status" == 200 ]]; then
            break
        fi
    else
        health_status="000"
    fi
    remaining=$((health_deadline - SECONDS))
    if (( remaining <= 0 )); then
        break
    fi
    sleep_seconds="$health_interval"
    if (( sleep_seconds > remaining )); then
        sleep_seconds="$remaining"
    fi
    sleep "$sleep_seconds"
done

health_summary() {
    python3 - "$health_file" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as response_file:
    payload = json.load(response_file)
fields = ("status", "git_sha", "model", "worker_alive")
print(json.dumps({name: payload.get(name) for name in fields}, ensure_ascii=False))
PY
}

if [[ "$health_status" != 200 ]]; then
    printf '健康检查超时：期望 git_sha=%s，最后 HTTP 状态=%s\n' "$expected_git_sha" "$health_status" >&2
    if [[ -s "$health_file" ]]; then
        printf '/health 白名单字段: %s\n' "$(health_summary)" >&2
    else
        printf '/health 白名单字段: {"status":null,"git_sha":null,"model":null,"worker_alive":null}\n' >&2
    fi
    exit 1
fi

actual_git_sha="$(python3 - "$health_file" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as response_file:
    print(json.load(response_file).get("git_sha") or "")
PY
)"
if [[ "$actual_git_sha" != "$expected_git_sha" ]]; then
    printf '健康检查 git_sha 不一致：期望=%s 实际=%s\n' "$expected_git_sha" "$actual_git_sha" >&2
    printf '/health 白名单字段: %s\n' "$(health_summary)" >&2
    exit 1
fi

printf '更新完成：process=%s model=%s port=%s git_sha=%s\n' \
    "$process_name" "$model_type" "$port" "$actual_git_sha"
