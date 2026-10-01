# 服务端返回完整 duration 但 tokens 截断：WebSocket 文件任务尾部缺失约 34 分钟

## 结论范围

本文只记录下游生产环境的实测现象、复现定位和已排除项，不对 CapsWriter 服务端的根因下结论。根因、具体代码位置和修复方式待 `zlxlabs/CapsWriter-ASR-Server` 仓取证判定。

长期证据文档路径：`docs/development/known-issues/capswriter-ws-tail-truncation.md`

音频文件位于第三方生产机，不能公开分发。对方仓如需复现，请通过 `ssh n305` 进入容器 `youtube-api` 取用，或向问题报告方索要副本。即使 GitHub 挂了，本文也包含音频文件定位信息、`ffprobe` 命令原文、`ffmpeg` 静音检测命令原文、缺口秒数和五项已排除项，足以让第三方按同一生产文件复现。

## 生产实测

**实测时间**：2026-10-01。下游任务从 13:55:52 开始，14:05:08 完成。

**下游任务**：

- 任务 ID：`task_611a46266bd7479e835f35178da7bbd4`
- 媒体 URL：`https://www.youtube.com/watch?v=ExhGXAfp3zg`
- 媒体 ID：`ExhGXAfp3zg`
- 生产环境使用 CapsWriter WebSocket 6016 老路径；本次没有使用新 HTTP 文件上传路径。

| 量 | 值 | 来源 |
| --- | ---: | --- |
| 媒体真实时长 | **10506.92 s**（2h55m07s） | `ffprobe` 与服务端自报 `result["duration"]` 一致 |
| 服务端自报 duration | **10506.9 s** | SDK `Transcript.duration` |
| 服务端自报纯处理耗时 | **205.81 s**（RTF 0.0196） | `time_complete - time_start` |
| ASR 输出末段时间戳 | **8469.03 s** | 1093 个分段最后一条 `end_time` |
| 结果缺口 | **2037.9 s**，约 34 分钟（占 19.4%） | `10506.92 - 8469.03` |
| 客户端表现 | **判定成功** | 正常渲染 `/view` 页面并发送完成通知，无告警 |

因此，实测同时出现了两个需要服务端解释的事实：服务端返回的 `duration` 覆盖完整媒体时长，但 ASR 输出分段的最后时间戳停在 8469.03 秒；客户端仍把该结果当作成功。

缺口区间内有连续人声，不是片尾静音。下游重建分段时还出现：

```text
text_clean=126149, reconstructed=155578, diff=29429
```

末段文本退化为幻觉重复，例如：

```text
...bye bye bye 没有没有没有有有有有有有有有有没有没有没有没有。
```

## 生产文件与复现命令

生产文件仍在：

```text
ssh n305
容器：youtube-api
文件：/app/data/files/audio/29fa7556-e980-47a3-b8b2-d75149bac4bd_ExhGXAfp3zg_AI Utopia + Rare-Disease Diagnosis _ Joel Borgen & Daniel McKinnon.webm
```

在 `youtube-api` 容器内设置文件变量：

```sh
F='/app/data/files/audio/29fa7556-e980-47a3-b8b2-d75149bac4bd_ExhGXAfp3zg_AI Utopia + Rare-Disease Diagnosis _ Joel Borgen & Daniel McKinnon.webm'
```

### 1. `ffprobe` 媒体时长与码率

完整命令：

```sh
ffprobe -hide_banner -v error -show_entries format=duration,bit_rate -of default=noprint_wrappers=1 "$F"
```

实测输出：

```text
duration=10506.921000
bit_rate=111608
```

### 2. `ffmpeg` 缺口区间静音检测

完整命令：

```sh
ffmpeg -hide_banner -nostats -ss 8000 -i "$F" -af silencedetect=noise=-35dB:d=2 -f null - 2>&1 | grep -E "silence_(start|end)"
```

实测输出摘要：

```text
只有 11 个 2-7s 句间停顿，零长静音段
```

每 10 分钟的平均音量（`mean_volume / max_volume`）如下。缺口内的音量与 ASR 已覆盖区间同档：

```text
t=7800-8400 : -23.4 dB / -0.1 dB      ← ASR 覆盖区内（有语音）
t=8400-9000 : -24.0 dB /  0.0 dB      ← ASR 已停（缺口起点附近）
t=9000-9600 : -23.9 dB /  0.0 dB      ← 缺口内，音量与覆盖区同档
t=9600-10200: -23.4 dB /  0.0 dB      ← 缺口内，同档
t=10200-10800:-24.8 dB /  0.0 dB      ← 缺口内，同档
```

## 已排除项（五项）

以下均是本次生产记录中的实测排除，不是对服务端内部实现的推断。

1. **不是客户端超时。** SDK deadline 为 `duration + 60`，约 2.9 小时；实际从开始到完成只耗时 205.81 秒，约 3.5 分钟。
2. **不是客户端断连或重试。** `capswriter_asr` 0.1.0 把完整音频一次性全量上传；`seg_duration=25`、`seg_overlap=2` 是作为参数交给服务端执行；全程无断连、无重试。
3. **不是音频本身短。** `ffprobe` 得到 `duration=10506.921000`，与服务端自报 `duration=10506.9` 一致。
4. **不是文件下载截断。** 下载字节数与 `ffprobe` 报出的时长自洽。
5. **不是片尾静音。** `8000` 秒起的静音检测只有 11 个 2-7 秒句间停顿，没有长静音段；缺口区间的平均音量为约 `-24.8` 至 `-23.4 dB`，与 ASR 覆盖区间的 `-23.4 dB` 同档，且其中有连续人声。

## 影响面

下游生产缓存共有 **501** 份转录结果：

- 尾部缺口超过 10 分钟：**35 份（7.0%）**
- 尾部缺口超过 5 分钟：**69 份**

以上是缺口分布统计，暂不能据此断言所有样本由同一服务端机制造成。

## 请求服务端仓回答

以下问题均为待验证问题，不是本文对根因的断言：

1. 缺陷实际位于文件任务的哪一段：分段、分段结果合并，还是任务收尾逻辑？
2. 是否存在静默丢弃路径，例如某个分段失败后被吞掉且任务不再续传？
3. 该问题是否只存在于 WebSocket 6016 老路径，还是新 HTTP 文件上传路径也受影响？本次生产样本没有走新 HTTP 路径；本仓今天推进的 HTTP 协议重构中，PR #36「E5: add explicit HTTP file SDK and CLI」和 PR #37「feat: add shared HTTP task owner and PCM foundation」已于 2026-10-01 02:5x 合并。请明确判断新路径是否还需要同类修复。
4. 服务端能否在返回结果前自检 `duration` 与最后输出时间戳、分段完整性或任务终态是否一致，并显式报错，而不是把截断结果作为成功返回？

请不要把本文的“待验证”问题当作已经确认的服务端机制；具体根因请以对方仓的代码取证和回归测试为准。
