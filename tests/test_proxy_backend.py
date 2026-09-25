# coding: utf-8

import pytest

from core.proxy.backend import BackendState


def test_backend_active_tasks_increment_and_decrement():
    backend = BackendState(id="backend-0", url="ws://localhost:6016")

    backend.acquire_task()
    backend.acquire_task()
    assert backend.active_tasks == 2

    backend.release_task()
    backend.release_task()
    backend.release_task()
    assert backend.active_tasks == 0


def test_backend_connect_failures_mark_unhealthy_after_threshold():
    backend = BackendState(
        id="backend-0",
        url="ws://localhost:6016",
        max_connect_failures=3,
    )

    backend.record_connect_failure()
    backend.record_connect_failure()
    assert backend.healthy is True
    assert backend.consecutive_failures == 2

    backend.record_connect_failure()
    assert backend.healthy is False
    assert backend.consecutive_failures == 3


def test_backend_success_resets_failures_and_health():
    backend = BackendState(
        id="backend-0",
        url="ws://localhost:6016",
        max_connect_failures=2,
    )
    backend.record_connect_failure()
    backend.record_connect_failure()
    assert backend.healthy is False

    backend.record_connect_success()

    assert backend.healthy is True
    assert backend.consecutive_failures == 0


def test_connect_success_does_not_override_failed_health_probe():
    backend = BackendState(id="backend-0", url="ws://localhost:6016")

    backend.record_health_failure()
    backend.record_connect_success()

    assert backend.healthy is False

    backend.record_legacy_health()

    assert backend.healthy is True


def test_backend_connect_failure_records_failure_time(monkeypatch):
    monkeypatch.setattr("core.proxy.backend.time.time", lambda: 123.0)
    backend = BackendState(
        id="backend-0",
        url="ws://localhost:6016",
        max_connect_failures=1,
    )

    backend.record_connect_failure()

    assert backend.healthy is False
    assert backend.last_failure_time == 123.0


def test_backend_connect_failure_can_preserve_cooldown_start(monkeypatch):
    monkeypatch.setattr("core.proxy.backend.time.time", lambda: 123.0)
    backend = BackendState(
        id="backend-0",
        url="ws://localhost:6016",
        max_connect_failures=1,
        last_failure_time=100.0,
    )

    backend.record_connect_failure(refresh_cooldown=False)

    assert backend.healthy is False
    assert backend.last_failure_time == 100.0


def test_backend_records_processing_latency_with_ewma():
    backend = BackendState(id="backend-0", url="ws://localhost:6016")

    assert backend.record_processing_latency(2.0) is True
    assert backend.avg_latency == pytest.approx(2.0)
    assert backend.latency_samples == 1

    assert backend.record_processing_latency(4.0) is True
    assert backend.avg_latency == pytest.approx(2.4)
    assert backend.latency_samples == 2


def test_backend_records_latency_sample_time(monkeypatch):
    monkeypatch.setattr("core.proxy.backend.monotonic", lambda: 456.0)
    backend = BackendState(id="backend-0", url="ws://localhost:6016")

    assert backend.record_processing_latency(2.0) is True

    assert backend.last_latency_time == 456.0


def test_backend_resets_stale_latency_window(monkeypatch):
    monkeypatch.setattr("core.proxy.backend.monotonic", lambda: 1000.0)
    backend = BackendState(
        id="backend-0",
        url="ws://localhost:6016",
        avg_latency=300.0,
        latency_samples=4,
        last_latency_time=600.0,
    )

    assert backend.record_processing_latency(1.0, latency_ttl_seconds=300) is True

    assert backend.avg_latency == pytest.approx(1.0)
    assert backend.latency_samples == 1
    assert backend.last_latency_time == 1000.0


def test_backend_rejects_invalid_processing_latency(monkeypatch):
    warnings = []
    monkeypatch.setattr(
        "core.proxy.backend.logger.warning",
        lambda message, *args, **kwargs: warnings.append(message % args),
    )
    backend = BackendState(id="backend-0", url="ws://localhost:6016")

    assert backend.record_processing_latency(-1.0) is False
    assert backend.record_processing_latency(301.0) is False

    assert backend.avg_latency == 0.0
    assert backend.latency_samples == 0
    assert any("异常 processing_latency" in message for message in warnings)


def test_backend_accepts_processing_latency_up_to_300_seconds():
    backend = BackendState(id="backend-0", url="ws://localhost:6016")

    assert backend.record_processing_latency(299.0) is True
    assert backend.record_processing_latency(300.0) is True

    assert backend.latency_samples == 2


def test_backend_rejects_nan_processing_latency():
    backend = BackendState(id="backend-0", url="ws://localhost:6016")

    assert backend.record_processing_latency(float("nan")) is False

    assert backend.avg_latency == 0.0
    assert backend.latency_samples == 0


def test_backend_truncation_resets_stale_ewma():
    """When latency > 300 is rejected, any accumulated EWMA should be reset
    so the backend is not permanently penalized by stale high values."""
    backend = BackendState(id="backend-0", url="ws://localhost:6016")

    backend.record_processing_latency(10.0)
    backend.record_processing_latency(20.0)
    assert backend.latency_samples == 2
    assert backend.avg_latency > 0

    assert backend.record_processing_latency(301.0) is False

    assert backend.latency_samples == 0
    assert backend.avg_latency == 0.0


def test_backend_rejects_zero_weight():
    with pytest.raises(ValueError, match="weight"):
        BackendState(id="backend-0", url="ws://localhost:6016", weight=0.0)


def test_backend_rejects_negative_weight():
    with pytest.raises(ValueError, match="weight"):
        BackendState(id="backend-0", url="ws://localhost:6016", weight=-1.0)
