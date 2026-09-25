# coding: utf-8
"""Backend state tracking for the ASR proxy."""

from __future__ import annotations

import time
import math
from dataclasses import dataclass, field
from typing import ClassVar
from time import monotonic

from core.logger import get_logger


logger = get_logger("proxy")


@dataclass
class BackendState:
    """Mutable health and load counters for one backend ASR server."""

    _rr_clock: ClassVar[int] = 0

    id: str
    url: str
    weight: float = 1.0
    active_tasks: int = 0
    healthy: bool = True
    health_probe_failed: bool = False
    protocol_version: int = 1
    encodings: list[str] = field(default_factory=list)
    model: str | None = None
    git_sha: str | None = None
    consecutive_failures: int = 0
    last_failure_time: float = 0.0
    last_result_time: float = 0.0
    avg_latency: float = 0.0
    latency_samples: int = 0
    last_latency_time: float = 0.0
    max_connect_failures: int = 3
    _last_selected: int = 0

    def __post_init__(self) -> None:
        if self.weight <= 0:
            raise ValueError(f"weight must be positive, got {self.weight}")

    def mark_selected(self) -> None:
        BackendState._rr_clock += 1
        self._last_selected = BackendState._rr_clock

    def acquire_task(self) -> None:
        self.active_tasks += 1

    def release_task(self) -> None:
        self.active_tasks = max(0, self.active_tasks - 1)

    def record_connect_success(self) -> None:
        self.consecutive_failures = 0
        self.healthy = not self.health_probe_failed

    def record_health(self, payload: dict) -> None:
        self.healthy = True
        self.health_probe_failed = False
        self.protocol_version = payload["protocol_version"]
        self.encodings = payload.get("encodings", [])
        self.model = payload.get("model")
        self.git_sha = payload.get("git_sha")
        self.consecutive_failures = 0

    def record_legacy_health(self) -> None:
        self.healthy = True
        self.health_probe_failed = False
        self.protocol_version = 1
        self.encodings = []
        self.model = None
        self.git_sha = None
        self.consecutive_failures = 0

    def record_health_failure(self) -> None:
        self.healthy = False
        self.health_probe_failed = True
        self.protocol_version = 1
        self.encodings = []
        self.model = None
        self.git_sha = None

    def record_connect_failure(self, refresh_cooldown: bool = True) -> None:
        self.consecutive_failures += 1
        if refresh_cooldown:
            self.last_failure_time = time.time()
        if self.consecutive_failures >= self.max_connect_failures:
            self.healthy = False

    def record_result(self) -> None:
        self.last_result_time = time.time()

    def record_processing_latency(self, latency: float, latency_ttl_seconds: int = 300) -> bool:
        if not math.isfinite(latency) or latency < 0 or latency > 300:
            logger.warning(
                "异常 processing_latency 已排除: backend=%s latency=%.3f",
                self.id,
                latency,
            )
            if self.latency_samples > 0:
                self.avg_latency = 0.0
                self.latency_samples = 0
            return False

        now = monotonic()
        latency_is_stale = (
            self.last_latency_time > 0
            and now - self.last_latency_time > latency_ttl_seconds
        )
        if self.latency_samples == 0 or latency_is_stale:
            self.avg_latency = latency
            self.latency_samples = 1
        else:
            self.avg_latency = (self.avg_latency * 0.8) + (latency * 0.2)
            self.latency_samples += 1
        self.last_latency_time = now
        return True
