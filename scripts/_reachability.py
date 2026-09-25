#!/usr/bin/env python3
"""统计服务端、代理、SDK、测试与验证脚本可达的仓内 Python 模块。"""

from __future__ import annotations

import ast
import subprocess
from collections import deque
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE_REV = "b82534c107c1c2c0c55c966b937de8aca9ec8d6f"


def module_name(path: Path) -> str:
    relative = path.relative_to(ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def python_files() -> list[Path]:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout.decode().split("\0")
    paths = {ROOT / name for name in tracked if name.endswith(".py") and name}
    paths.add(ROOT / "scripts/_reachability.py")
    return sorted(path for path in paths if path.is_file())


def module_index(files: list[Path]) -> dict[str, Path]:
    return {module_name(path): path for path in files}


def local_modules(name: str, index: dict[str, Path]) -> set[str]:
    found = set()
    parts = name.split(".") if name else []
    for end in range(1, len(parts) + 1):
        candidate = ".".join(parts[:end])
        if candidate in index:
            found.add(candidate)
    return found


def imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    imported = set()
    current = module_name(path).split(".")
    if path.name != "__init__.py":
        current.pop()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                prefix = current[: len(current) - node.level + 1]
                suffix = node.module.split(".") if node.module else []
                base = ".".join(prefix + suffix)
            else:
                base = node.module or ""
            if base:
                imported.add(base)
                imported.update(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
        elif isinstance(node, ast.Call) and node.args:
            function = node.func
            is_dynamic_import = (
                isinstance(function, ast.Name) and function.id in {"__import__", "import_module"}
            ) or (isinstance(function, ast.Attribute) and function.attr == "import_module")
            if is_dynamic_import and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                imported.add(node.args[0].value)
    return imported


def roots(files: list[Path]) -> set[str]:
    entrypoints = {"start_server.py", "start_proxy.py"}
    return {
        module_name(path)
        for path in files
        if path.relative_to(ROOT).as_posix() in entrypoints
        or path.relative_to(ROOT).parts[0] in {"sdk", "tests"}
        or (path.relative_to(ROOT).parts[0] == "scripts" and path.name.startswith("_") and path.suffix == ".py")
    }


def reachable_modules(files: list[Path]) -> set[str]:
    index = module_index(files)
    queue = deque(sorted(roots(files)))
    reached = set()
    while queue:
        name = queue.popleft()
        for module in local_modules(name, index):
            if module in reached:
                continue
            reached.add(module)
            queue.extend(sorted(imports(index[module]) - reached))
    return reached


def cleanup_candidates(files: list[Path]) -> set[Path]:
    candidates = set()
    for path in files:
        relative = path.relative_to(ROOT)
        if relative.parts[0] in {"core", "LLM"} or len(relative.parts) == 1:
            if relative.parts[:2] == ("core", "server") and "export" in relative.parts:
                continue
            candidates.add(path)
    return candidates


def main() -> None:
    files = python_files()
    reached = reachable_modules(files)
    base_files = subprocess.run(
            ["git", "ls-tree", "-r", "-z", "--name-only", BASE_REV],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout.decode().split("\0")
    base_count = sum(path.endswith(".py") for path in base_files)
    unreachable = sorted(
        path.relative_to(ROOT).as_posix()
        for path in cleanup_candidates(files)
        if module_name(path) not in reached
    )
    print(f"入口可达模块数：{len(reached)}")
    print(f"删除前后仓内 .py 文件数：{base_count} -> {len(files)}")
    print("仓内（引擎 export 目录除外）不可达的 .py 文件：")
    print("\n".join(unreachable) if unreachable else "无")


if __name__ == "__main__":
    main()
