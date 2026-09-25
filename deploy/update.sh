#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
    printf '用法: %s <git-ref>\n' "$0" >&2
    exit 2
fi

ref="$1"
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

if [[ ! -x .venv/bin/python ]]; then
    python3 -m venv .venv
fi
.venv/bin/python -m pip install -r "$requirements_file"

pm2 restart "$process_name"

health_file="$(mktemp)"
trap 'rm -f -- "$health_file"' EXIT
health_url="http://127.0.0.1:${port}/health"
health_status="000"
elapsed=0
while (( elapsed <= health_timeout )); do
    if health_status="$(curl --silent --show-error --connect-timeout 2 --max-time 5 \
        --output "$health_file" --write-out '%{http_code}' "$health_url")"; then
        if [[ "$health_status" == 200 ]]; then
            break
        fi
    else
        health_status="000"
    fi
    if (( elapsed >= health_timeout )); then
        break
    fi
    sleep "$health_interval"
    elapsed=$((elapsed + health_interval))
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
