# DESIGN-note：公开仓先交付无凭据质量入口

## 目标

业务仓提供可被统一门禁调用的真实测试入口 `scripts/gate-quality`，一次跑完整 `tests/`。既有 CI 双矩阵保留。平台 caller、secret、迁仓未落地，本卡不宣称接入成功。

## 非目标

不新增门禁 caller、不配置 secret、不迁移 GitHub 仓、不改平台仓、不部署、不删历史。不把 key 烤进镜像，不新增凭据 broker。

## 为什么不是分区 / 删除 / 约定

- **分区**：测试入口必须落在业务仓，后续 quality job 才能在无 Silo 凭据的一次性环境执行 PR 代码。平台评审与存储必须换 job；同 job 的 step env 不是隔离。本仓不再拆框架。
- **删除**：不能删真实测试入口。没有它，后续接入只能继续 hosted CI 或跳过，fork 绿仍不代表完整主审。
- **约定**：口头约定「跑 pytest」锁不住 Python 3.12、`websockets==15.0.1` 与完整 `tests/` argv；必须是可执行脚本并由子进程契约消费。

## 方案要点与已否决方案

- **要点**：入口用现成 uv：`uv run --no-project --python 3.12 --with … python -m pytest tests/ -q`。依赖与命令取自 `.github/workflows/ci.yml` 的 `websockets==15.0.1` 矩阵腿。ffmpeg 缺失则失败并打印可 grep 错误串，不在本机 sudo 安装。脚本不读平台 secret。统一方案依据 [gate#248](https://github.com/zlxlabs/gate/issues/248)；登记见 [gate-hub#1134](https://github.com/zlxlabs/gate-hub/issues/1134)。当前平台未落地，调用配置未完成。
- **信任边界**：
  - **test**：执行 PR 树内 `scripts/gate-quality` 与测试代码；不得持有私有存储或模型登录凭据。
  - **primary**：模型主审。是否执行 PR 代码、是否与 test 共享 runner/cache 标待证，本卡不落地。
  - **storage**：ledger/artifact 必须在固定可信来源的独立 job；step env 不是隔离。
- **已否决**：改 `ci.yml` 当门禁（锁定决策要求原样保留）；根目录 pyproject/lock/通用测试框架；step 级收窄 env 当隔离（同 job 文件系统可污染，见 gate#248）；把平台 secret 拷进本仓或镜像。

## 关键不变式

1. [实测] 入口实际 argv 为 `uv run --no-project --python 3.12`、CI 同款 `--with`（含 `websockets==15.0.1`）以及 `python -m pytest tests/ -q`。代码：`scripts/gate-quality`。测试：`tests/test_gate_quality.py` 经替身 uv 写文件消费。
2. [实测] 替身 uv 非零退出时入口保持非零，无吞错。同上测试。
3. [实测] 替身只记 argv/env 后退出，不递归调用 pytest。同上测试。

## 待验证前提

1. [推断] review-primary / 模型工具循环是否执行 PR 代码；若会执行必须物理隔离。验证入口：平台仓按 [gate#248](https://github.com/zlxlabs/gate/issues/248) 基线核对，不在本卡。
2. [推断] hosted/self-hosted shared cache 与一次性 runner 是否让后续持 key 步骤读到 test 产物。验证入口：gate#248 故障注入，尚未做。
3. [推断] 线上 gate-v2 tag 与顾问审查检出是否一致；实施前须在实际基线重核。见 gate#248。
4. [推断] 公开 own-branch、external fork、Dependabot、draft→ready 的 secret 与主审政策未写。Dependabot 同仓 head 不等于可信。验证入口：[gate-hub#1134](https://github.com/zlxlabs/gate-hub/issues/1134) 接入卡。
5. [推断] 调用配置（caller/onboard/registry）未完成，本卡结束后门禁不会自动跑本入口。

## 验收路径

1. 入口：`bash scripts/gate-quality`
2. 步骤：先跑 `tests/test_gate_quality.py` 锁 argv 与非零传播；再跑完整入口；`git diff --check`。
3. 预期：契约测试绿；完整入口退出码等于 pytest 退出码；ffmpeg/uv 缺失时非零。不预期平台接入成功。后续真实 gate runner 验证属于接入卡。
