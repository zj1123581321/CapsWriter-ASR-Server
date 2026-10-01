<!--
DESIGN-note 方案对齐单。落盘路径：docs/sessions/<会话名>/design.md（进 git）。
用户点头的对象是这份文档，不是对话记录。
-->

# DESIGN-note：SDK 声明的样本数改由自己发出的字节流推导

关联 issue：#43（Refs #43）

## 目标

直播录制文件、以及任何「容器时长与真实音频长度不一致」的媒体，用 SDK 默认的 `flac` 编码转录时不再被服务端拒收为 `decode_failed`，转录成功返回结果。

## 非目标

- 不改协议：`samples_total` 仍是 v2 末帧必填字段，语义不变。
- 不改服务端校验：`SAMPLES_TOLERANCE` 与 `_verify_samples_total` 保持原样（理由见「关键不变式」第 3 条）。
- 不解决「源录音本身缺尾」的完整性证明（见「待验证前提」第 1 条，那是协议层的下一步）。
- 不收紧 `SAMPLES_TOLERANCE` 常量（缺少各编码正常偏差分布数据，另议）。
- 不修 `result.duration` 漏计极短末段（实测上限 0.1s 且不累积，见「关键不变式」第 4 条）。

## 为什么不是分区 / 删除 / 约定

- **分区**：缺陷不在「谁写哪个文件」，而在「同一个函数用哪个数据源算长度」。分成 SDK 侧和服务端侧两张卡解决不了数据源错误——服务端没有客户端的字节流，只能看到已解码的样本数。分区不适用。
- **删除**：不能删。`samples_total` 是当前唯一能兜住静默截断的机制：实测把 flac 截断到 60%~90% 后 ffmpeg 退出码仍是 0、只写 stderr，而已提交的推理段会被丢弃。删掉它会把「必挂但安全」换成「静默出错误结果」，那是 P1。
- **约定**：不能靠约定解决。约定「客户端应当声明精确值」无法约束第三方客户端（下游 `VideoTranscriptAPI` 自己也发这个字段），也无法约束本仓 SDK 未来的改动。必须让错误在代码层面不可表达。

## 方案要点与已否决方案

- **要点**：
  - 压缩编码路径（`flac` / `ogg_opus`）的 `samples_total` 改为**从已发出的字节流反推**：SDK 转码后已持有完整压缩字节，把这份字节重新送入 ffmpeg 解码为 16 kHz 单声道 PCM，流式累计字节数即精确样本数。服务端因此永远拿到与实际解码一致的值。
  - `deadline` 同步改用这份真实样本数计算，不再使用 ffprobe 容器时长。
  - `_audio_duration()` 整体删除：`samples_total` 与 `deadline` 都不再依赖它，删除后 SDK 不再调用 `ffprobe`，无 `format.duration` 的有效音频也不会提前 `decode_failed`。SDK 对 `ffprobe` 的硬依赖随之解除。
  - 回归测试必须用**真实 ffmpeg**（现有 `tests/test_sdk_client.py:51` 的假 `ffprobe` 对任何输入恒返回 `1.0`，任何时长断言在它面前都恒真），并断言 SDK **实际发出的末帧字段**，不是内部函数返回值。

- **已否决**：
  - *落盘临时 flac 再读 STREAMINFO*：Ogg/Opus 根本没有 STREAMINFO，两种编码要分叉实现，等于把同一个 bug 的两个变体都写出来；且引入磁盘占用、权限、磁盘满、清理失败四类新故障面。重新解码计数一套代码覆盖两种编码，实测两者与源直接解码逐样本相等。
  - *换一个更准的 ffprobe 字段*（`duration_ts` / `nb_frames` / `stream=duration`）：实测这些字段与 `format=duration` 同源，容器头错时一起错。换字段不改变数据源。
  - *放宽或删除服务端 `samples_total` 校验*：理由见「删除」。
  - *让 SDK 接受调用方传入已转好的 flac 路径*：可行作可选接口，但不能代替本次修复——SDK 收到这类文件后仍须从实际将发送的内容计算声明值，否则同一错误换个入口复现。

## 关键不变式

1. [实测] 压缩路径声明的 `samples_total` 与服务端实际解码样本数之差为 0。
   证据：`tmp_issue43_scan.py`（本仓根目录，未提交）对 6 种来源 × 4 种编码共 24 组测量，元数据正常时最大偏差 522 样本；修复后对「时长头砍半的 mp4」与「音视频轨不等（视频 300s + 音频 60s）的 mp4」两类夹具，压缩路径偏差须为 0。
   测试锁死：新增回归测试，断言 SDK 实际发出的末帧 `samples_total` 等于真实解码样本数。

2. [实测] `deadline` 由真实样本数决定，不受容器时长影响。
   证据：修复前实测 600 秒音频被砍头后 `set_deadline` 收到 `360.0`（`tmp_issue43_deadline.py`）。
   测试锁死：新增用例，容器时长被砍半时 `deadline` 仍覆盖真实音频长度。

3. [实测] 服务端 `samples_total` 校验保持硬失败，且必须留有覆盖「ffmpeg 退出 0 但流被截断」的测试。
   证据：`tmp_issue43_truncation.py` 实测 flac 截断 90% / 60% 时 ffmpeg 退出码为 0。
   测试锁死：`tests/test_protocol_v2.py:105` `test_truncated_and_invalid_flac_fail_and_reap_ffmpeg`（本次不修改，作为回归护栏）。

4. [实测] `result.duration` 漏计极短末段不影响本次范围：只在文件末尾出现一次，不累积。
   证据：`tmp_duration_leak.py` 走真实 `PcmSegmenter.drain_ready` + `process_audio_task`，文件 25.3s / 50.7s / 75.9s / 100.1s / 300.7s 全部跳过 0 段、漂移 0.000s；仅 5.05s 文件出现 1 段、漂移 +0.050s。
   测试锁死：无（本次不改该行为，记录在此以免后续误判为同源缺陷）。

5. [实测] 本仓 SDK 测试此前无法发现此类缺陷——假 `ffprobe` 恒返回 `1.0`。
   证据：`tests/test_sdk_client.py:51-70` `fake_media_tools` 夹具，脚本内容为 `printf '1.0\n'`，对任何存在的输入都一样。
   测试锁死：新增回归测试必须绕开该夹具、调用真实 ffmpeg；若新测试仍走 `fake_media_tools`，视为未完成。

6. [实测] SDK 不得再调用 `ffprobe`。
   证据：`grep -rn "_audio_duration" --include="*.py" .` 在实现完成后无命中；`_audio_duration` 已整体删除，SDK 只用 `ffmpeg`。
   测试锁死：`tests/test_sdk_samples_total.py::test_transcription_never_shells_out_to_ffprobe` 在 `PATH` 前置一个一旦被调用就退出 1 的假 `ffprobe` 并记录调用痕迹，断言痕迹文件不存在。

## 待验证前提

1. [推断] 「从产物重新解码计数」只能证明 SDK 发出的流有多长，**不能证明源录音本身没有缺尾**。若 SDK 解码源文件时已静默少读，按产物计数会让服务端一致地接受这个较短结果。
   注意这**不是本次修复引入的盲区**：当前声明值本身就取自不可信的源元数据，在这一层同样无能。「从产物计数」是严格改进。
   验证入口：需要另找可信的源完整性信号（如 ffmpeg 转码时的解码告警计数），属协议层下一步，不在本批。
   提升条件：确定独立信号来源与验证入口后，提升为不变式。

2. [推断] 重新解码计数的精度依赖「SDK 侧 ffmpeg 的解码样本数与服务端 ffmpeg 一致」。FLAC 解码由规范精确确定，预期逐样本相同；Ogg/Opus 的 pre-skip 裁剪可能随 ffmpeg/libopus 版本有极小差异。
   验证入口：本批回归测试已对 `flac` 和 `ogg_opus` 断言偏差为 0（容差 16000 样本 = 1 秒），两编码实测均通过。
   待补证据：跨 ffmpeg 版本的一致性尚未验证（本机与服务端均为 ffmpeg 6.1.1）。若 `ogg_opus` 在其他版本出现非零偏差，须按编码区分容差。
   提升条件：跨版本验证补齐后转为不变式。

3. [推断] 音视频轨长度不等的 mp4 触发同一缺陷，是本次实测的新发现，爆炸半径大于 issue #43 描述的直播录制场景（视频轨比音频轨长超过 1 秒即必挂）。
   验证入口：回归测试须包含「视频 300s + 音频 60s」夹具。
   提升条件：回归测试转绿后，随不变式 1 一并确认。

## 验收路径

1. 入口：SDK 公共 API `transcribe_file(path, url, encoding="flac")`（`sdk/capswriter_asr/client.py`），配合本仓 `tests/harness/server.py` 的假服务端 harness。
2. 步骤：
   - 用真实 ffmpeg 构造两个夹具：① 60 秒音频的 mp4，把 `mvhd`/`mdhd`/`tkhd` 时长砍半、`stts` 不动；② 视频轨 300 秒 + 音频轨 60 秒的正常 mp4。
   - 对每个夹具、每种压缩编码（`flac`、`ogg_opus`）调用 `transcribe_file`，捕获 WebSocket 上行帧。
   - 断言末帧 `samples_total` 等于真实解码样本数（差值为 0），且任务以成功终态收尾。
   - 另跑一遍 `python3 tmp_issue43_scan.py`，确认输出中不再出现 `decode_failed`。
3. 预期：两类夹具、四种编码全部通过；服务端不再返回 `decode_failed`；`deadline` 覆盖真实音频长度。

库函数绿、单元测试绿不算验收——必须走 `transcribe_file` 入口并断言实际发出的帧字段。
