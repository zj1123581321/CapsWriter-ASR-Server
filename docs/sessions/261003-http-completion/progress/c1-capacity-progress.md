# M4-C1 进度（源配额与结果/WAL/物理容量预留）

Task-Id：卡面未填（待主脑补）｜Goal：http-core/M4
规划来源：`7ab3551` 的 `m4-plan.md`（修正版）｜Verify：ci-standard-v1

## 当前阶段

实现完成并自测通过（`tests/test_http_capacity.py` 20 条 + 原有 HTTP 测试全绿），
等待主脑验收。周期删源（C2）不在本卡范围，未开始。

## 本段结论

- **源预留改 source-presence 口径**：`_iter_present_sources` 逐个已登记源取**真实文件长度**，
  留在磁盘上的 UPLOADING / EXPIRED / COMMITTED 三类都按声明长度计费；只有源文件已被删除
  （ENOENT）才退出预留。旧口径 `WHERE state != 'EXPIRED'` 会把仍占磁盘的过期 partial 漏记。
- **结果/WAL 预留按 SQLite 页几何算，实测推翻了「2 倍」**：真实 sqlite3、`page_size=4096`、
  真实 63.99 MiB 合法完整结果、生产默认自动 checkpoint 下，单次提交的真实增量
  **134,922,696 B ≈ 2.0105×payload**，比 `2 × 64 MiB = 134,217,728 B` 高 704,968 B。
  实现改为 `⌈(记录页数 + 32) × (2×page_size + 32) × 1.05⌉`，生产值 **141,902,242 B
  （2.1145×）**，对实测峰值留 5.17% 余量；页大小运行时读 `PRAGMA page_size`，不写死 4096。
- **DB guard 补齐**：`_db_bytes() + QUEUED/RUNNING 预留 + 本次新增 Job 预留 > 2 GiB` 才
  507 `storage_guard_full`。终态后 pending 自动退出，同一份结果字节转为 `_db_bytes()`
  实测到的文件占用，不开第二本资源账本。
- **物理余量三处覆盖**（create / PATCH / commit）：`free - 未物化源字节 - 待处理结果预留
  - 本次新增承诺 ≥ 2 GiB`。未物化字节按「声明长度 − 真实文件长度」算，所以已写盘字节只在
  free 里扣一次，崩溃残留的「已写入未确认尾」既不重复收费也不漏算。
- **读数失败一律显式上抛**：`disk_usage` 的 OSError、源 `stat` 的非 ENOENT 错误、DB 主文件
  缺失（`StoreUnavailable`，不再当 0 字节）都不被吞成「有容量」。
- **不挡既有语义**：幂等重放（200 同一 Job）、`job_record` 200、DONE result 200、FAILED
  result 409 + `error_code` 在容量满时照常；等值侧按额度类 `>`、余量类 `<`、槽位类 `>=`。

## 关键决策与已否决方案

决策：(1) 单 Job 预留改页几何而非 2 倍乘数，并把实测数字写进常量注释；
(2) 未物化字节用真实文件长度而非 `confirmed_offset`；(3) `_db_bytes` 用 `os.stat` 区分
「-wal/-shm 合法不存在」与「DB 主文件缺失」；(4) 预留量纲不加配置项，全部是模块常量；
(5) 测试用缩小预算 + 真实 SQLite，不真造 2 GiB。

已否决（沿用规划并补充本次实证）：用 `WHERE state != EXPIRED` 排除保留源；2×payload 当
WAL 上界；按 `confirmed_offset` 算未物化字节（重复收费已写字节）；DB 缺失当 0 字节；
stat/SQLite 失败视 0；只为 create 查一次容量；用预算函数自身输出当参考模型；
为 M4 新增资源表或通用账本；把 `Path.exists()` 当可靠读数（权限错误会静默变 False）。

## 远程地址

- draft PR：https://github.com/zlxlabs/CapsWriter-ASR-Server/pull/60 （draft，执行器不置 ready）
- 分支：`card/http-m4-c1-capacity-261003`｜base：`e066930`
- 全量验证（两套 websockets 各跑一遍，均为 `445 passed, 3 skipped`）：
  - websockets 最新：`445 passed, 3 skipped, 149 warnings in 213.54s (0:03:33)`
  - websockets==15.0.1：`445 passed, 3 skipped, 149 warnings in 216.63s (0:03:36)`
- 3 skip 的实际身份（`-rs`）：`test_aligner_integration.py:53/62` ForceAligner 后端/模型未安装；
  `test_segmenter.py:208` 缺 silero-VAD 模型或 onnxruntime。base 为 425 passed/3 skipped，
  净增 20 条，无 HTTP 解码类 skip。

## 下一步唯一动作

主脑在 draft PR 上跑主审并核对本文件与 `m4-plan.md` 的来源标注；通过后置 ready。
C2 另开卡：周期终态源清理必须沿用本卡的 source-presence 口径，先 unlink 后记释放。