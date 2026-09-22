---
name: capswriter-internal-review-pipeline
description: 用户认可 office-hours → CEO review → eng review 的完整 review pipeline，且要求不跳步
kind: preference
valid_as_of: 2026-09-04
origin: 3bc3013b-ad78-40ff-819d-eacf084e889b
---

这条偏好来自 CapsWriter-Offline-with-AI（origin 会话 `3bc3013b-ad78-40ff-819d-eacf084e889b`）。

用户在做完 CEO review 后主动问"不需要跑 eng-review 吗？"，说明他认可并期望完整的 review pipeline（office-hours → plan-ceo-review → plan-eng-review）。

**Why:** 用户在意工程严谨性，不希望跳过 review 步骤。Codex outside voice 在 eng review 中发现了 CEO review 遗漏的致命缺陷（AudioCache per-websocket），验证了多步 review 的价值。

**How to apply:** 对这个项目的新功能，建议走完整 review pipeline。不要因为"设计看起来没问题"就跳过 eng review。
