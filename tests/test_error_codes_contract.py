"""服务端、SDK 与协议文档的错误码契约。"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _assignment_set(path: Path, name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            call = node.value
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                call = call.args[0]
            if not isinstance(call, (ast.Set, ast.Tuple, ast.List)):
                raise AssertionError(f"{path}: {name} 必须是字面量集合")
            return {ast.literal_eval(item) for item in call.elts}
    raise AssertionError(f"{path}: 未找到 {name}")


def _markdown_error_codes(path: Path) -> set[str]:
    document = path.read_text(encoding="utf-8")
    section = document.split("### 4.2 error", maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
    return {
        match.group(1)
        for line in section.splitlines()
        if (match := re.match(r"\| ([a-z][a-z0-9_]*) \|", line))
        and match.group(1) != "code"
    }


def _assert_error_code_sets_match(*sets: set[str]) -> None:
    assert sets and all(codes == sets[0] for codes in sets[1:]), sets


def test_protocol_error_codes_match_sdk_and_documentation():
    core = _assignment_set(ROOT / "core/protocol.py", "ERROR_CODES")
    sdk = _assignment_set(ROOT / "sdk/capswriter_asr/client.py", "PROTOCOL_ERROR_CODES")
    documented = _markdown_error_codes(ROOT / "docs/reference/protocol.md")
    _assert_error_code_sets_match(core, sdk, documented)


def test_error_code_contract_rejects_an_extra_code():
    expected = {"bad_request", "internal"}
    with pytest.raises(AssertionError) as caught:
        _assert_error_code_sets_match(expected, expected | {"made_up"})
    print(f"红验捕获预期断言失败：{caught.value}")
