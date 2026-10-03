# coding: utf-8
"""R4 结构约束：HTTP owner 释放路径的机械检查（AST，非文本 grep）。

把「终态可靠落库之后，才释放 HTTP owner/闸门」固化成可机械核对的约束，
让将来新增的释放路径在 CI 里直接失败，而不是再靠人工复审发现：

1. 位置表清单：``core/server`` 内所有 ``transition_terminal`` 调用点按
   (文件, 函数) 精确钉住；任何新增/删除/搬移的释放路径都会让清单失配。
2. 违规路径形状：``fail_active_tasks`` 与 ``ProcessManager.monitor`` 不得
   再以 http key 直接调用 ``transition_terminal``，必须走
   ``finalize_http_job``。
3. 顺序约束：每个可能携带 http key 的调用点，源码顺序上必须先有持久化
   await（且被 ``wait_for`` 卡住上限）才有 ``transition_terminal``。
4. 终态责任唯一：HTTP 结果分支（ws_send 的 http 分支）不得自行终态转换；
   成功路径由 ``HttpResultSink.__call__`` 落库→转换→释放一体完成，失败路径
   由 ``runner.fail_job`` 完成，运行时由 ``test_http_double_terminal_eliminated``
   直数计数（每条路径恰好一次）。
5. ws-only 形状：其余调用点的 key 参数必须是 ws 字面量，或源自
   ``connection_tasks``——而 ``connection_tasks`` 只装 ws key 由
   ``begin_task`` 的形状约束钉死。

静态检查的诚实边界：跨函数的 key 数据流不做全程序追踪（如
``_acquire_segment_slot`` 的 key 参数），这些点位由位置表钉死 + 运行时
探针（tests/test_http_file_runner.py 的 R4 用例）兜底。
"""
from __future__ import annotations

import ast
import shutil
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1] / "core" / "server"

# 位置表：每个文件里各函数允许出现的 transition_terminal 调用数（修复后基线）。
# 新增释放路径 = 清单失配 = CI 红；改之前必须先把该行写进主脑位置表并补对应
# 的顺序/形状约束。
EXPECTED_TERMINAL_CALLS = {
    "connection/ws_recv.py": {"_acquire_segment_slot": 1, "ws_recv": 5},
    "connection/ws_send.py": {
        "schedule_error_close": 1,
        # 1 = ws 兜底（socket 已消失的 ws key）；http key 由 http 守卫 continue 掉，
        # 必须走 finalize_http_job（见 test_violating_paths_go_through_finalize）
        "fail_active_tasks": 1,
        # ws_send 只剩 WS 路径的 2 处；HTTP 结果分支的转换责任完全移交 sink，
        # 分支内一行转换都不许有（见 test_ws_send_http_branch_has_no_transition）
        "ws_send": 2,
    },
    "worker/process_manager.py": {"monitor": 1},
    # __call__ = HttpResultSink 成功路径的唯一转换（落库→转换→释放一体）；
    # finalize_http_job 与失败路径都经 runner.fail_job 落库+释放（机制只有一份，
    # 见 test_finalize_entry_persists_before_release_with_bounded_wait 的链式断言）
    "http_file_runner.py": {"__call__": 1, "fail_job": 1},
}


def _load(relpath: str) -> ast.AST:
    # utf-8-sig：connection/__init__.py 等文件带 BOM，ast.parse 不接受 U+FEFF
    return ast.parse(
        (SERVER_DIR / relpath).read_text(encoding="utf-8-sig")
    )


def _functions(tree: ast.AST) -> dict:
    """{函数名: 节点}；本仓约定同名函数在同一文件内不重复定义。"""
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _call_name(func: ast.expr):
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _terminal_calls(node: ast.AST) -> list:
    return sorted(
        (
            item
            for item in ast.walk(node)
            if isinstance(item, ast.Call)
            and _call_name(item.func) == "transition_terminal"
        ),
        key=lambda item: item.lineno,
    )


def _awaits_of(node: ast.AST, name: str) -> list:
    return [
        item
        for item in ast.walk(node)
        if isinstance(item, ast.Await)
        and isinstance(item.value, ast.Call)
        and _call_name(item.value.func) == name
    ]


def _parents(tree: ast.AST) -> dict:
    return {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }


def _key_arg(call: ast.Call) -> ast.expr:
    # 统一签名 transition_terminal(state, key, status, ...)，key 是第 2 个位置参数
    assert len(call.args) >= 2, f"transition_terminal 调用缺少 key 参数: {call.lineno}"
    return call.args[1]


def _is_ws_literal(key: ast.expr) -> bool:
    return (
        isinstance(key, ast.Call)
        and _call_name(key.func) == "make_task_key"
        and len(key.args) >= 2
        and isinstance(key.args[0], ast.Constant)
        and key.args[0].value == "ws"
    )


def _ws_name_bindings(func: ast.AST) -> set:
    """函数内被赋值为 make_task_key('ws', ...) 调用的名字集合（含条件表达式）。"""
    bound = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if isinstance(value, ast.IfExp):
            value = value.body
        if not _is_ws_literal(value):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                bound.add(target.id)
    return bound


def _is_key_kind_check(test: ast.expr, kind: str) -> bool:
    """形如 key[0] == '<kind>' 的判断。"""
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Subscript)
        and isinstance(test.left.value, ast.Name)
        and test.left.value.id == "key"
        and any(isinstance(op, ast.Eq) for op in test.ops)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == kind
    )


def _is_owner_kind_check(test: ast.expr, kind: str) -> bool:
    """形如 result.owner_kind == '<kind>' 的判断。"""
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Attribute)
        and test.left.attr == "owner_kind"
        and any(isinstance(op, ast.Eq) for op in test.ops)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == kind
    )


def test_release_position_table_is_exact():
    """约束 1：释放原语的全部调用点与位置表逐行一致（新增路径必失败）。"""
    actual = {}
    for path in sorted(SERVER_DIR.rglob("*.py")):
        tree = _load(str(path.relative_to(SERVER_DIR)))
        per_file = {
            name: len(_terminal_calls(node))
            for name, node in _functions(tree).items()
            if _terminal_calls(node)
        }
        if per_file:
            actual[str(path.relative_to(SERVER_DIR))] = per_file
    assert actual == EXPECTED_TERMINAL_CALLS, (
        f"transition_terminal 调用点清单失配（新增/删除/搬移了释放路径？）：\n"
        f"actual={actual!r}\nexpected={EXPECTED_TERMINAL_CALLS!r}"
    )


def test_finalize_entry_persists_before_release_with_bounded_wait():
    """约束 3a：finalize_http_job 只经 wait_for 包裹的 runner.fail_job 收尾。

    机制只有一份：finalize 内不得直接调用 transition_terminal（释放动作在
    runner.fail_job 内部完成，其先落库后释放的顺序由
    test_runner_fail_job_persists_before_release 钉住——两处断言合起来把
    「先落库、后释放、wait_for 有上限」的完整链条锁死）。
    """
    finalize = _functions(_load("http_file_runner.py"))["finalize_http_job"]
    assert _terminal_calls(finalize) == [], (
        "finalize_http_job 不得直接调用 transition_terminal；释放必须经由 "
        "runner.fail_job 的既有正确形态"
    )
    persists = [
        item
        for item in ast.walk(finalize)
        if isinstance(item, ast.Await)
        and isinstance(item.value, ast.Call)
        and _call_name(item.value.func) == "wait_for"
        and item.value.args
        and isinstance(item.value.args[0], ast.Call)
        and _call_name(item.value.args[0].func) == "fail_job"
    ]
    assert len(persists) == 1, "finalize_http_job 必须只有一处受监督的持久化写"
    wait_for_call = persists[0].value
    assert any(keyword.arg == "timeout" for keyword in wait_for_call.keywords), (
        "持久化写必须用 wait_for 卡住超时上限，否则错因没写进去会升级成进程挂死"
    )


def test_runner_fail_job_persists_before_release():
    """约束 3b：runner.fail_job（正确样板）保持先落库后释放。"""
    fail_job = _functions(_load("http_file_runner.py"))["fail_job"]
    terminals = _terminal_calls(fail_job)
    assert len(terminals) == 1
    persists = _awaits_of(fail_job, "fail_job")
    assert len(persists) == 1, "runner.fail_job 必须只有一处持久化写"
    assert persists[0].lineno < terminals[0].lineno


def test_violating_paths_go_through_finalize():
    """约束 2：两条违规路径不再直接 transition_terminal，改走 finalize_http_job。"""
    ws_send_tree = _load("connection/ws_send.py")
    ws_send_funcs = _functions(ws_send_tree)
    fail_active_tasks = ws_send_funcs["fail_active_tasks"]
    terminals = _terminal_calls(fail_active_tasks)
    assert len(terminals) == 1, (
        "fail_active_tasks 只允许保留 ws 兜底的一处直接释放"
    )
    finalize_awaits = _awaits_of(fail_active_tasks, "finalize_http_job")
    assert len(finalize_awaits) == 1
    parents = _parents(ws_send_tree)
    # finalize 调用必须嵌在 http 判断里（http 分支）
    ancestor = parents.get(finalize_awaits[0])
    finalize_in_http = False
    while ancestor is not None:
        if isinstance(ancestor, ast.If) and _is_key_kind_check(ancestor.test, "http"):
            finalize_in_http = True
            break
        ancestor = parents.get(ancestor)
    assert finalize_in_http, "finalize_http_job 必须位于 fail_active_tasks 的 http 分支内"
    # 残留的 ws 兜底 transition_terminal 必须在 http 守卫之后：
    # 守卫体以 continue 收尾，http key 到不了它
    guard = None
    for node in ast.walk(fail_active_tasks):
        if isinstance(node, ast.If) and _is_key_kind_check(node.test, "http"):
            guard = node
            break
    assert guard is not None and isinstance(guard.body[-1], ast.Continue), (
        "fail_active_tasks 的 http 守卫必须以 continue 收尾"
    )
    assert terminals[0].lineno > guard.end_lineno, (
        "ws 兜底释放点出现在 http 守卫之前，http key 可能直接到达 transition_terminal"
    )

    # 同一棵语法树取函数与父指针，节点身份必须一致
    process_tree = _load("worker/process_manager.py")
    monitor = _functions(process_tree)["monitor"]
    parents = _parents(process_tree)
    terminals = _terminal_calls(monitor)
    assert len(terminals) == 1, "monitor 只允许保留 ws 兜底的一处直接释放"
    finalize_awaits = _awaits_of(monitor, "finalize_http_job")
    assert len(finalize_awaits) == 1
    # 残留的 transition_terminal 不得嵌在 http 判断的「真分支」里（orelse 是
    # else 兜底，http key 由守卫 continue/elif 排除，到不了它）
    ancestor = parents.get(terminals[0])
    child = terminals[0]
    while ancestor is not None:
        if isinstance(ancestor, ast.If) and _is_key_kind_check(ancestor.test, "http"):
            assert child not in ancestor.body, (
                "monitor 的 http 分支仍在直接 transition_terminal"
            )
        child, ancestor = ancestor, parents.get(ancestor)
    # finalize 调用必须真的嵌在 http 判断里（而不是写错分支）
    ancestor = parents.get(finalize_awaits[0])
    nested_in_http = False
    while ancestor is not None:
        if isinstance(ancestor, ast.If) and _is_key_kind_check(ancestor.test, "http"):
            nested_in_http = True
            break
        ancestor = parents.get(ancestor)
    assert nested_in_http, "finalize_http_job 必须位于 monitor 的 http 分支内"


def test_ws_send_http_branch_has_no_transition():
    """HTTP 结果分支不得自行终态转换：终态责任唯一属于结果 sink。

    旧形态（缺陷）：sink 失败路径已落库 FAILED + 转换 + 释放运行态记录，
    ws_send 回到自己的分支后又对同一 key 转一次（重复终态）；成功路径 sink
    只落库不转换，转换由 ws_send 补——两套终态责任并存，与
    state.py release_terminal_task 的文档声明矛盾。本断言锁定修复形态：
    ws_send HTTP 分支只有 sink + acknowledge，一行转换都不剩。
    """
    ws_send = _functions(_load("connection/ws_send.py"))["ws_send"]
    http_branches = [
        node
        for node in ast.walk(ws_send)
        if isinstance(node, ast.If) and _is_owner_kind_check(node.test, "http")
    ]
    assert len(http_branches) == 1, "ws_send 只允许一个 HTTP 结果分支"
    branch = http_branches[0]
    assert _terminal_calls(branch) == [], (
        "ws_send 的 HTTP 分支仍在自行终态转换；HTTP 终态唯一收尾人是结果 sink"
    )
    sinks = _awaits_of(branch, "sink")
    assert len(sinks) == 1, "HTTP 结果分支必须经 sink 收尾（落库→转换→释放一体）"
    assert isinstance(branch.body[-1], ast.Continue), (
        "HTTP 分支必须以 continue 收尾，之后的调用点才天然是 WS 专属"
    )
    after = [t for t in _terminal_calls(ws_send) if t.lineno > branch.end_lineno]
    assert len(after) == 2, "HTTP 分支之后只允许 WS 路径的两处释放"


def test_ack_before_terminal_order():
    """F1：HTTP 分支先段确认、后 sink 终态收尾（与 WS 分支段确认在前的语义一致）。

    终态收尾会 pop pending_segments 并把 record.segment_slots 置 None，事后的
    段确认无事可做；顺序反了只是让人误以为它还在起作用。
    """
    ws_send = _functions(_load("connection/ws_send.py"))["ws_send"]
    http_branches = [
        node
        for node in ast.walk(ws_send)
        if isinstance(node, ast.If) and _is_owner_kind_check(node.test, "http")
    ]
    assert len(http_branches) == 1, "ws_send 只允许一个 HTTP 结果分支"
    branch = http_branches[0]
    acks = [
        node
        for node in ast.walk(branch)
        if isinstance(node, ast.Call)
        and _call_name(node.func) == "acknowledge_segment_result"
    ]
    assert len(acks) == 1, "HTTP 分支必须做段确认（与 WS 分支一致）"
    sinks = _awaits_of(branch, "sink")
    assert len(sinks) == 1, "HTTP 分支必须经 sink 终态收尾"
    assert acks[0].lineno < sinks[0].lineno, (
        "F1：段确认必须在终态收尾之前；终态释放后 pending_segments 与段名额"
        "已被清，事后的段确认无事可做"
    )


def test_sink_success_path_persists_transitions_releases_in_order():
    """sink 成功路径补完：record_result 落库 → transition_terminal → 释放运行态记录。"""
    http_tree = _load("http_file_runner.py")
    sink_call = _functions(http_tree)["__call__"]  # HttpResultSink.__call__
    terminals = _terminal_calls(sink_call)
    assert len(terminals) == 1, (
        "sink 成功路径必须恰好做一次终态转换（status 必须是 DONE）"
    )
    done_args = terminals[0].args
    assert any(
        isinstance(arg, ast.Constant) and arg.value == "DONE" for arg in done_args
    ), "sink 成功路径的转换终态必须是 DONE"
    persists = _awaits_of(sink_call, "record_result")
    assert len(persists) == 1, "成功路径只有 record_result 一处持久化写"
    releases = [
        item
        for item in ast.walk(sink_call)
        if isinstance(item, ast.Call)
        and _call_name(item.func) == "release_terminal_task"
    ]
    assert len(releases) == 1, "成功路径必须释放运行态记录（与失败路径同形态）"
    assert persists[0].lineno < terminals[0].lineno < releases[0].lineno, (
        "R4 顺序被破坏：成功路径必须先落库 DONE，再转换，最后释放记录"
    )
    # 失败路径仍经 runner.fail_job（其内部顺序由 test_runner_fail_job_persists_before_release 钉住）
    assert len(_awaits_of(sink_call, "_fail")) == 4, (
        "sink 的失败出口（error_code/结果校验 HttpStoreError/未知异常/落库失败）"
        "必须都走 _fail → fail_job，且未知异常出口继续上抛"
    )


def test_ws_only_sites_use_ws_shaped_keys():
    """约束 4：非 http 白名单调用点的 key 形状必须是 ws 字面量或 connection_tasks 派生。"""
    ws_send_funcs = _functions(_load("connection/ws_send.py"))
    terminal = _terminal_calls(ws_send_funcs["schedule_error_close"])[0]
    assert _is_ws_literal(_key_arg(terminal)), (
        "schedule_error_close 的 key 必须是 make_task_key('ws', ...) 字面量"
    )

    ws_recv_funcs = _functions(_load("connection/ws_recv.py"))
    ws_recv = ws_recv_funcs["ws_recv"]
    ws_bound_names = _ws_name_bindings(ws_recv)
    has_connection_tasks_lookup = any(
        isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "active"
            for target in node.targets
        )
        and isinstance(node.value, ast.Call)
        and _call_name(node.value.func) == "get"
        for node in ast.walk(ws_recv)
    )
    assert has_connection_tasks_lookup, "ws_recv 里必须有 active = connection_tasks.get(...)"
    for terminal in _terminal_calls(ws_recv):
        key = _key_arg(terminal)
        if isinstance(key, ast.Name) and key.id == "active":
            continue  # connection_tasks 只装 ws key（见 test_connection_tasks_only_holds_ws_keys）
        if isinstance(key, ast.Name) and key.id in ws_bound_names:
            continue  # 名字在函数内被绑定为 make_task_key('ws', ...) 字面量
        assert _is_ws_literal(key), (
            f"ws_recv 第 {terminal.lineno} 行的释放点使用了非 ws 字面量 key："
            f"{ast.dump(key)[:120]}"
        )
    helper_terminal = _terminal_calls(ws_recv_funcs["_acquire_segment_slot"])[0]
    key = _key_arg(helper_terminal)
    assert isinstance(key, ast.Name) and key.id == "key", (
        "_acquire_segment_slot 的 key 参数由 ws_recv 调用链以 ws key 传入"
        "（位置表钉死，运行时由 R4 探针兜底）"
    )


def test_connection_tasks_only_holds_ws_keys():
    """约束 4 的根基：begin_task 里 connection_tasks 的赋值必须在 ws 守卫之下。"""
    # 同一棵语法树取函数与父指针，节点身份必须一致
    state_tree = _load("state.py")
    begin_task = _functions(state_tree)["begin_task"]
    parents = _parents(state_tree)
    assigns = [
        node
        for node in ast.walk(begin_task)
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Subscript)
        and getattr(node.targets[0].value, "attr", "") == "connection_tasks"
    ]
    assert len(assigns) == 1, "connection_tasks 只允许在 begin_task 里赋值一次"
    ancestor = parents.get(assigns[0])
    guarded = False
    while ancestor is not None:
        test = getattr(ancestor, "test", None)
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "owner_kind"
            and any(isinstance(op, ast.Eq) for op in test.ops)
            and any(
                isinstance(comp, ast.Constant) and comp.value == "ws"
                for comp in test.comparators
            )
        ):
            guarded = True
            break
        ancestor = parents.get(ancestor)
    assert guarded, "connection_tasks 赋值不在 ws 守卫之下，ws-only key 推导失效"


# ---------------------------------------------------------------- 运行时计数探针


@pytest.mark.asyncio
@pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="本机 PATH 中没有 ffmpeg，HTTP 文件解码链无法验证",
)
async def test_http_double_terminal_eliminated(tmp_path, monkeypatch):
    """重复终态直数计数：成功/失败两条路径各自恰好转换一次。

    不依赖「测试通过」间接推断——用计数包装器包住全部持有
    transition_terminal 私有引用的模块（state/ws_send/http_file_runner），
    对 (job_id, status) 逐次计数：旧缺陷形态下失败路径会被 sink 与 ws_send
    各转一次（计数=2），修复后两条路径都必须恰好=1。
    """
    pytest.importorskip("aiohttp", reason="未安装 aiohttp==3.14.3；HTTP 入口默认关闭")
    import importlib

    import core.server.state as state_module
    from tests.test_http_file_runner import (
        FAKE_ENGINE,
        make_container,
        running_runner_server,
        submit,
        wait_terminal,
    )

    counts: dict = {}
    real_transition = state_module.transition_terminal

    def counting_transition(state, key, status, code=None):
        counts[(key[2], status)] = counts.get((key[2], status), 0) + 1
        return real_transition(state, key, status, code)

    # 三个模块都以 `from ..state import transition_terminal` 持有私有引用，
    # 逐一替换才能把全部调用点都纳入计数
    for modname in (
        "core.server.state",
        "core.server.connection.ws_send",
        "core.server.http_file_runner",
    ):
        monkeypatch.setattr(
            importlib.import_module(modname),
            "transition_terminal",
            counting_transition,
        )

    source = make_container(tmp_path, "double-term.mp3")

    # 成功路径：sink 落库 DONE 后自己转换并释放，ws_send 不得再转
    async with running_runner_server(tmp_path) as harness:
        recovery = tmp_path / "ok.json"
        handle = await submit(
            harness, source, recovery, seg_duration=5.0, seg_overlap=1.0
        )
        assert (await wait_terminal(harness, recovery)).state == "DONE"
        assert counts.get((handle.job_id, "DONE")) == 1, counts
        assert counts.get((handle.job_id, "FAILED")) is None, counts
        assert list(harness.state.active_http_jobs) == []
        assert harness.state.tasks == {}

    # 失败路径：sink 的 fail_job 已落库+转换+释放，ws_send 不得再转
    failing = dict(FAKE_ENGINE, fail_on_call=2)
    async with running_runner_server(tmp_path, options=failing) as harness:
        recovery = tmp_path / "bad.json"
        handle = await submit(
            harness, source, recovery, seg_duration=5.0, seg_overlap=1.0
        )
        status = await wait_terminal(harness, recovery)
        assert status.state == "FAILED"
        assert counts.get((handle.job_id, "FAILED")) == 1, counts
        assert list(harness.state.active_http_jobs) == []
        assert harness.state.tasks == {}


if __name__ == "__main__":  # pragma: no cover - 手动调试入口
    pytest.main([__file__, "-q"])
