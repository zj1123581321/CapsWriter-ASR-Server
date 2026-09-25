# coding: utf-8
"""
ASR WebSocket 负载均衡代理配置。

部署时通过环境变量覆盖，无需改源码：
  CW_PROXY_BACKENDS    逗号分隔的后端列表，如 "ws://192.168.1.10:6017,ws://192.168.1.20:6017"
                       可选权重格式为 "url|weight"，如 "ws://192.168.1.10:6017|2.0"
  CW_PROXY_PORT        代理监听端口，默认 6020
  CW_PROXY_ADDR        代理监听地址，默认 0.0.0.0
"""

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def _env_str(key, default):
    return os.environ.get(key, default)

def _env_backends(key, default):
    raw = os.environ.get(key)
    if raw:
        backends = []
        for item in raw.split(','):
            item = item.strip()
            if not item:
                continue
            if '|' in item:
                url, weight = item.rsplit('|', 1)
                backends.append((url.strip(), float(weight.strip())))
            else:
                backends.append(item)
        return backends
    return default


class ProxyConfig:
    """CapsWriter ASR proxy runtime config."""

    listen_addr = _env_str('CW_PROXY_ADDR', '0.0.0.0')
    listen_port = int(_env_str('CW_PROXY_PORT', '6020'))

    backends = _env_backends('CW_PROXY_BACKENDS', [
        ("ws://127.0.0.1:6016", 1.0),
        ("ws://127.0.0.1:6017", 1.0),
        # ("ws://remote:6016", 0.3),    # Tailscale 3Mbps - 降权
    ])

    max_connect_failures = 3
    cooldown_seconds = 60
    probe_interval = 30.0
    log_level = "DEBUG"
