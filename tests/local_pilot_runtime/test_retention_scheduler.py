from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

import pytest

import services.local_pilot_runtime.retention_scheduler as scheduler_runtime
from services.local_pilot_runtime.retention_scheduler import (
    SchedulerMetrics,
    main,
    run_loop,
)

NOW = datetime(2026, 8, 11, 5, 0, tzinfo=UTC)
ENABLED_ENVIRONMENT = {
    "GBOS_LOCAL_RUNTIME_ENABLED": "true",
    "GBOS_RETENTION_SCHEDULER_ENABLED": "true",
    "GBOS_RETENTION_SCHEDULER_KILL_SWITCH": "false",
    "GBOS_RETENTION_ENABLED": "true",
    "GBOS_RETENTION_DRY_RUN": "false",
    "GBOS_RETENTION_INTERVAL_SECONDS": "86400",
    "GBOS_RETENTION_METRICS_PORT": "9101",
}


class _StopAfterWaits:
    def __init__(self, waits: int) -> None:
        self._remaining = waits
        self.timeouts: list[float] = []

    def is_set(self) -> bool:
        return self._remaining == 0

    def wait(self, timeout: float | None = None) -> bool:
        assert timeout is not None
        self.timeouts.append(timeout)
        self._remaining -= 1
        return self.is_set()


class _MetricsServer:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def shutdown(self) -> None:
        self.stopped = True


def test_main_is_default_off_before_server_or_retention_worker() -> None:
    calls: list[str] = []

    assert (
        main(
            environ={},
            run_once=lambda: calls.append("run") or 0,
            server_factory=lambda _metrics, _port: calls.append("server") or _MetricsServer(),
        )
        == 78
    )
    assert calls == []


@pytest.mark.parametrize(
    "override",
    [
        {"GBOS_RETENTION_SCHEDULER_ENABLED": "TRUE"},
        {"GBOS_RETENTION_SCHEDULER_KILL_SWITCH": "true"},
        {"GBOS_RETENTION_ENABLED": "false"},
        {"GBOS_RETENTION_DRY_RUN": "true"},
        {"GBOS_RETENTION_INTERVAL_SECONDS": "3599"},
        {"GBOS_RETENTION_INTERVAL_SECONDS": "86401"},
        {"GBOS_RETENTION_METRICS_PORT": "0"},
    ],
)
def test_main_requires_exact_execute_configuration(override: dict[str, str]) -> None:
    calls: list[str] = []
    environment = {**ENABLED_ENVIRONMENT, **override}

    assert (
        main(
            environ=environment,
            run_once=lambda: calls.append("run") or 0,
            server_factory=lambda _metrics, _port: calls.append("server") or _MetricsServer(),
        )
        == 78
    )
    assert calls == []


def test_loop_executes_bounded_runs_serially_and_recovers_next_period() -> None:
    outcomes: list[object] = [RuntimeError("raw identity ref"), 0]
    active = 0
    maximum_active = 0

    def run_once() -> int:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        outcome = outcomes.pop(0)
        active -= 1
        if isinstance(outcome, BaseException):
            raise outcome
        return int(outcome)

    stop = _StopAfterWaits(2)
    moments = iter((NOW, NOW + timedelta(days=1)))
    metrics = SchedulerMetrics(clock=lambda: next(moments))

    assert (
        run_loop(
            run_once=run_once,
            interval_seconds=86400,
            stop_event=stop,
            metrics=metrics,
        )
        == 0
    )

    assert outcomes == []
    assert maximum_active == 1
    assert stop.timeouts == [86400, 86400]
    rendered = metrics.render(now=NOW + timedelta(days=1, seconds=5))
    assert 'gbos_retention_scheduler_failure_total{code="retention_run_failed"} 1' in rendered
    assert (
        "gbos_retention_scheduler_last_success_timestamp_seconds "
        f"{int((NOW + timedelta(days=1)).timestamp())}"
    ) in rendered
    assert "gbos_retention_scheduler_last_success_age_seconds 5" in rendered
    assert "raw identity ref" not in rendered


def test_loop_honors_a_pre_set_stop_without_a_run() -> None:
    stop = Event()
    stop.set()
    calls: list[str] = []

    assert (
        run_loop(
            run_once=lambda: calls.append("run") or 0,
            interval_seconds=86400,
            stop_event=stop,
            metrics=SchedulerMetrics(clock=lambda: NOW),
        )
        == 0
    )
    assert calls == []


def test_emergency_stop_latch_blocks_every_scheduled_worker_call(tmp_path: Path) -> None:
    latch = tmp_path / "EMERGENCY_STOP"
    latch.write_text("active\n", encoding="utf-8")
    stop = _StopAfterWaits(1)
    server = _MetricsServer()
    calls: list[str] = []

    assert (
        main(
            environ={
                **ENABLED_ENVIRONMENT,
                "GBOS_EMERGENCY_STOP_FILE": str(latch),
            },
            run_once=lambda: calls.append("delete") or 0,
            stop_event=stop,
            server_factory=lambda _metrics, _port: server,
            clock=lambda: NOW,
        )
        == 0
    )
    assert calls == []


def test_restarts_reuse_real_execute_worker_entrypoint_and_shutdown_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, str]] = []
    servers: list[_MetricsServer] = []

    def retention_worker(*, environ: Mapping[str, str]) -> int:
        captured.append(dict(environ))
        return 0

    def server_factory(_metrics: SchedulerMetrics, port: int) -> _MetricsServer:
        assert port == 9101
        server = _MetricsServer()
        servers.append(server)
        return server

    monkeypatch.setattr(scheduler_runtime, "retention_worker_main", retention_worker)

    for _restart in range(2):
        assert (
            main(
                environ=ENABLED_ENVIRONMENT,
                stop_event=_StopAfterWaits(1),
                server_factory=server_factory,
                clock=lambda: NOW,
            )
            == 0
        )

    assert len(captured) == 2
    assert all(environment["GBOS_RETENTION_DRY_RUN"] == "false" for environment in captured)
    assert all(server.started and server.stopped for server in servers)
