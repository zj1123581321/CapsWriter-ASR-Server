# E5 SDK 修复增量与已安装消费者终审

failure-visibility: p2-only

- risk-tier：internal
- 冻结全量：`9e40df201e9e3cdf4dad18deec7629974b3d6fb3..454873f3aa7cb94d731d59ce69fac920e9633dae`
- 修复增量：`2fbf645fec8d72b9ec5c9fb48bde1ac14a50ce3e..454873f3aa7cb94d731d59ce69fac920e9633dae`
- 对照 A 轮：`http-e5-review-a-261001` 的 `b068875` verdict；未读取实现方报告、推理或私聊。
- 结论：H1 修复了 A 轮四条结果/上传身份 P2；全量消费复查新发现一条 P2。没有 P1。spec #2 的损坏恢复文件约束尚未完全满足，本 verdict 不把 P2 级别写成验收通过。

## H0..H1 四问

1. **是否只修登记项**：代码增量仅为 `_upload_info` 拒绝短 offset/缺 job 的 COMMITTED、`_get_upload` 固定 upload ID、结果校验固定请求 job ID 和严格布尔 `true`；分别对应 A 轮 P2-4、P2-3、P2-1、P2-2。测试覆盖这些负态。没有修复其他已接受项。
2. **是否增加抽象/状态/事实源/retry/fallback**：没有新增生产抽象、状态或事实源；没有 retry、fallback、catch 降级或新依赖。新增测试 fixture helper 只供同一组参数化负态使用。
3. **是否增加双路径或绕过**：没有新增请求路径；HTTP 仍是显式子命令，WebSocket 仍走旧入口。
4. **原 success 是否回退**：已安装包的旧 CLI 普通位置参数/default 逻辑未变，`_formats` 抽取保持原校验。首参数恰为文件名 `http` 的冲突是 A 轮已列 P2、卡面已接受并给出 `./http` 绕法，本轮不重提。

## 全量消费者与负态证据

从冻结 SHA 导出副本到 `/tmp/e5-sdk-review-b-261001.di9JSU/src`，用独立虚拟环境构建并安装 SDK。探针断言 `capswriter_asr.__file__` 位于该虚拟环境、安装元数据含直接依赖 `httpx==0.28.1`；测试副本移除了源码路径注入，CLI 子进程未设置 `PYTHONPATH`，实际导入安装包。源码树没有生成安装副产物。

最终 TCP/CLI 探针集共 30 项，连续 5 轮均为 30/30 通过（150/150）；日志留在该临时目录的 `probe/round-1.log` 至 `round-5.log`。覆盖真实 HTTP 方法、路径、Authorization/幂等头、JSON 请求体、PATCH 原始字节、Content-Length、Upload-Offset、私有恢复文件 mode、失响应后的显式恢复、四个 CLI 操作、DONE 与完整结果、失败不落输出文件及 token 不出现在 CLI 输出。代理环境变量指向不可达地址且清空 NO_PROXY，CLI 本地 TCP 请求仍成功，符合 `trust_env=False`。正确 DONE、POST/PATCH/commit 失响应恢复、COMMITTED 恢复均有实际请求序列；错误 task ID、非布尔 final、缺字段/错类型结果均失败且没有输出文件。

### 新发现：P2 — 缺失 upload_id 的损坏恢复文件仍发起创建请求

- **违反**：spec #2“损坏恢复不得发网络/改旧凭据”。
- **位置**：`sdk/capswriter_asr/http_client.py:174-207` 的恢复文件必需字段集合没有要求 `upload_id`；`resume_file_http` 在 646-649 行把缺失字段与初始 `null` 状态等同，转而调用 `POST /v1/uploads`。
- **真实消费探针**：已安装的冻结 SDK 收到一份原本有效的恢复 JSON，只删除 `upload_id` 字段，源文件、URL、token、create key、size、SHA 和 options 均保留。调用 `resume_file_http` 后真实 loopback TCP 捕获到带 Authorization 与 Idempotency-Key 的 `POST /v1/uploads`；服务端返回 500 后调用可见失败，恢复文件字节没有被改写。这个负态在五轮中每轮均复现。正常生成的初始恢复文件会显式写 `upload_id: null`，所以仅凭当前代码不能把“字段缺失”安全解释为初始状态。
- **本仓 P1 两问**：① 已安装 SDK 的真实消费路径可触发；触发条件是恢复 JSON 缺少字段，正常 `_local_recovery` 输出包含该字段，当前没有证据表明正常提交会生成这种文件。② 后果是额外向恢复文件绑定的 URL 发出带旧凭据的创建请求，违反明确的无网络约束；本次 500 路径未改写文件，也未观察到错误 Transcript。重复 Job 的后果未由本探针证明，且请求沿用原幂等 key；按 internal 档未达 P1，定为 P2。
- **验收状态**：本卡没有实现修改授权；该 P2 未列为已接受不修。风险等级是 P2，但 spec #2 这条原始验收仍未完成，需由 owner 决定修复或明确接受。

## 已核对的合同与 unknown

已核对四组 async/sync API 导出、四个 CLI 操作、HTTPX 直接依赖与 CI 单行增补、恢复文件先于首个请求原子落盘、Linux mode 0600、64 KiB 哈希读取、最大 1 MiB 原字节 PATCH、请求长度和 offset 对齐、无 FFmpeg、无自动 retry/redirect/proxy/poll/WS fallback、失败保留恢复文件、Job 身份和结果 final 校验、失败查询不改恢复文件、旧 WebSocket 常规 CLI 路径。GET 原 upload 的 ID 不再被替换；COMMITTED 必须有完整 offset 与 job ID。缺失 upload ID 是上列唯一新 finding。

unknown：没有 Windows ACL 环境、完整 HTTP 服务端 producer、模型质量或 E1 缺少依赖的实证；不把 Linux fixture 结果外推为这些兼容性结论。派发时主干 CI 基线查询不可用；本轮只运行上述隔离消费者探针，未启动仓库全量 CI，因此继承红/新红不能判定。OCR 主腿已在 A 轮 primary reviewed，候选 JSON 为 `/tmp/ocr-result-dlg-20261001-030056-3c406f.json`；本轮未重跑 OCR，也不把它当通过依据。

## 收尾与限制

- **踩坑**：第一次从 worktree 调用 memory reader 时 cwd 不符合其相对路径约定，得到目录不匹配；改从 agent-config 根目录读取后成功。测试先确认源码路径注入已从临时副本移除，再运行已安装包。
- **闸绕过**：没有绕过安全闸、生产环境或仓库测试配置；动态端口由 TCP fixture 分配。GitHub issue 收件箱命令只输出 remote 映射诊断，无法据此声称 issue 列表为空。
- **与卡偏差**：没有改实现/测试/依赖；按禁止项未读取 `docs/sessions/261001-http-files/progress/E5-sdk-progress.md` 的正文。接手简报未找到本 worktree 交接单。没有主干 CI 结论。
- **最贵一步**：冻结源码打包、安装后以无源码路径注入运行 30 项 TCP/CLI 探针五轮；所有日志保存在 `/tmp/e5-sdk-review-b-261001.di9JSU/probe/`。
