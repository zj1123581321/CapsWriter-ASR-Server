# 开发环境与测试

根目录 `pyproject.toml` 只描述 Python 开发环境，不是服务发行包或部署依赖清单；服务端和平台依赖仍按 [部署说明](../deploy/README.md) 安装。开发环境要求 Python 3.12 及以上、uv 和 ffmpeg。

```sh
uv sync
uv run python -m pytest tests/ -q
```

`uv sync` 安装锁定的 `dev` 测试依赖；根项目不打包，开发依赖不包含模型或平台服务组件。门禁入口仍由 `bash scripts/gate-quality` 运行，它使用 `uv run --no-project` 和 CI 对齐的测试依赖，因此不会消费根 `pyproject.toml` 或 `uv.lock`。

Dev Container 使用 Python 3.12 和固定版本 uv feature；创建时仅在镜像缺少 ffmpeg 时安装它，再运行 `uv sync`。容器配置未定义额外凭据挂载；本次验证使用不含 `.env`、Git 凭据和 SSH 材料的隔离副本。
