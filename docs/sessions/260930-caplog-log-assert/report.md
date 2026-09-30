# 日志断言不再依赖 caplog 抓 propagate=False 的 logger

## 根因

`core/logger.py` 对具名 logger 设置 `propagate = False`，日志只进自己的文件和控制台 handler，不到 root。pytest 9.0 的 caplog 只往 root 上挂捕获 handler，所以 `task_end` 进了 `logs/server_latest.log`，`caplog.records` 仍是空的。pytest 9.1 会在每个阶段开始时，把同一个 handler 挂到当时已经存在的非传播 logger 上，因此 `pytest 9.1.1 + pytest-asyncio 1.4.0` 原来就是绿的。这与 websockets 版本无关。生产日志行为没有改，`propagate` 仍是 `False`。

## 手法

选「测试期给 `core.logger` 的具名 logger 挂可捕获 handler」，没有去读日志文件，也没有在单个测试里临时挂 handler。

`tests/conftest.py` 的 autouse fixture 给已创建的 logger，以及测试期间 `Logger.setup` 新建的 logger，加一个转发 handler。pytest 9.1 已经把 `caplog.handler` 挂上时，转发直接跳过，避免同一条日志记两次。9.0 只有这条转发能把记录送进 caplog。

`tests/test_protocol_v2.py` 里原有的 `task_end` 断言一字未改。`tests/test_logger.py` 加了一条元测试：阶段开始之后才创建的 `propagate=False` logger 也必须出现在 caplog 里。pytest 9.1 不会给阶段中途新建的 logger 补挂 handler，所以这条在两个版本上都能把转发本身锁住。

## CI

安装步的最低版本钉扎已经写在工作区的 `.github/workflows/ci.yml`：`pytest>=9.1.1`、`pytest-asyncio>=1.4.0`。YAML 语法检查退出码 0：

```text
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"
```

这次提交没有带上该文件。卡面没有 `- **验证根豁免**：.github/workflows/`，pre-commit 拒绝把验证根写入提交，报错是 `delegated executor cannot commit verification roots without a card exemption`。工作区修改保留，没有改用别的文件绕过。

因此本卡提交进分支的覆盖是 `tests/test_logger.py` 的元测试：现有 CI 的 `pytest tests/` 会跑它。logger 在阶段开始之后才创建，pytest 9.1 也不会自动给它挂捕获 handler，转发被拿掉时两个版本都会红。pytest 版本下限本身要等卡面补上验证根豁免后，才能把工作区里的 `ci.yml` 提交进去。

## 实测

本机全量（系统环境 pytest 9.0.3 + pytest-asyncio 1.3.0，即原先必红的组合）：

```text
python3 -m pytest tests/ -q
228 passed, 3 skipped, 81 warnings in 89.56s
```

派发时基线是 1 failed, 226 passed, 3 skipped。本卡新增 1 条元测试，原先失败的那条转为通过，结果是 0 failed。

旧组合：

```text
/tmp/vt-old/bin/python -m pytest tests/test_protocol_v2.py -q
9 passed, 18 warnings in 2.06s
```

`/tmp/vt-old` 为 pytest 9.0.3、pytest-asyncio 1.3.0。退出码 0。

新组合：

```text
/tmp/vt-new/bin/python -m pytest tests/test_protocol_v2.py -q
9 passed, 18 warnings in 2.03s
```

`/tmp/vt-new` 为 pytest 9.1.1、pytest-asyncio 1.4.0。退出码 0。

元测试在两套组合下都是 `1 passed`。

## 约束力

临时删掉 `core/server/state.py` 里 `task_end` 的 `duration_s=` 字段后：

```text
python3 -m pytest tests/test_protocol_v2.py::test_task_end_logs_one_structured_line_for_success_and_failure -q --tb=short
FAILED tests/test_protocol_v2.py::test_task_end_logs_one_structured_line_for_success_and_failure
tests/test_protocol_v2.py:242: in test_task_end_logs_one_structured_line_for_success_and_failure
    assert all("duration_s=" in line and "elapsed_s=" in line and "segments=" in line for line in own_lines)
E   assert False
1 failed in 0.10s
```

该文件已用 `git checkout -- core/server/state.py` 改回。同一条测试随后 `1 passed in 0.10s`。

## CI 基线

派发时主干基线不可用（gh api request failed）。继承红未能判定。本卡提交时还没有新的 CI 结果，没有可归类的新红。
