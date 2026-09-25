# coding: utf-8
"""Build metadata shared by HTTP health endpoints."""

from __future__ import annotations

import subprocess
from pathlib import Path

from core.logger import get_logger


logger = get_logger("build_info")


def get_git_sha() -> str:
    """Return the repository's short commit SHA or ``unknown``."""
    repository_root = Path(__file__).resolve().parents[2]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        logger.warning("git_sha 不可用: %s", exc)
        return "unknown"

    git_sha = result.stdout.strip()
    if not git_sha:
        logger.warning("git_sha 不可用: git rev-parse 未返回内容")
        return "unknown"
    return git_sha
