"""Default-off scheduler for fenced local retention execution."""

from __future__ import annotations

import os
import signal
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Protocol

from .retention_worker import main as retention_worker_main

DEFAULT_INTERVAL_SECONDS = 86_400
MIN_INTERVAL_SECONDS = 3_600
MAX_INTERVAL_SECONDS = 86_400
DEFAULT_METRICS_PORT = 9_101
DEFAULT_EMERGENCY_STOP_FILE = Path("/run/gbos/EMERGENCY_STOP")
_FAILURE_CODE = "retention_run_failed"


class StopSignal(Protocol):
    def is_set(self) -> bool: ...

    def wait(self, timeout: float | None = None) -> bool: ...


class MetricsServer(Protocol):
    def start(self) -> None: ...

    def shutdown(self) -> None: ...


class SchedulerMetrics:
    """Low-cardinality, content-free retention scheduler metrics."""

    def __init__(self, *, clock: Callable[[], datetime]) -> None:
        self._clock = clock
        self._lock = Lock()
        self._last_success_timestamp = 0.0
        self._last_failure_timestamp = 0.0
        self._failure_total = 0

    def record_success(self) -> None:
        timestamp = _timestamp(self._clock())
        with self._lock:
            self._last_success_timestamp = timestamp

    def record_failure(self) -> None:
        timestamp = _timestamp(self._clock())
        with self._lock:
            self._last_failure_timestamp = timestamp
            self._failure_total += 1

    def render(self, *, now: datetime | None = None) -> str:
        current_timestamp = _timestamp(self._clock() if now is None else now)
        with self._lock:
            success = self._last_success_timestamp
            failure = self._last_failure_timestamp
            failures = self._failure_total
        success_age = -1.0 if success == 0 else max(0.0, current_timestamp - success)
        return "\n".join(
            (
                "# TYPE gbos_retention_scheduler_last_success_timestamp_seconds gauge",
                "gbos_retention_scheduler_last_success_timestamp_seconds "
                f"{_metric_number(success)}",
                "# TYPE gbos_retention_scheduler_last_failure_timestamp_seconds gauge",
                "gbos_retention_scheduler_last_failure_timestamp_seconds "
                f"{_metric_number(failure)}",
                "# TYPE gbos_retention_scheduler_last_success_age_seconds gauge",
                f"gbos_retention_scheduler_last_success_age_seconds {_metric_number(success_age)}",
                "# TYPE gbos_retention_scheduler_failure_total counter",
                f'gbos_retention_scheduler_failure_total{{code="{_FAILURE_CODE}"}} {failures}',
                "",
            )
        )


def run_loop(
    *,
    run_once: Callable[[], int],
    interval_seconds: int,
    stop_event: StopSignal,
    metrics: SchedulerMetrics,
) -> int:
    """Run one pass at a time and recover from a failed pass next period."""

    while not stop_event.is_set():
        try:
            status = run_once()
        except Exception:
            status = 78
        if status == 0:
            metrics.record_success()
        else:
            metrics.record_failure()
        stop_event.wait(timeout=interval_seconds)
    return 0


def main(
    *,
    environ: Mapping[str, str] | None = None,
    run_once: Callable[[], int] | None = None,
    stop_event: StopSignal | None = None,
    server_factory: Callable[[SchedulerMetrics, int], MetricsServer] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> int:
    """Run periodic real retention only under exact closed opt-ins."""

    environment = os.environ if environ is None else environ
    if not _enabled(environment):
        return 78
    try:
        interval_seconds = _bounded_int(
            environment.get("GBOS_RETENTION_INTERVAL_SECONDS", str(DEFAULT_INTERVAL_SECONDS)),
            minimum=MIN_INTERVAL_SECONDS,
            maximum=MAX_INTERVAL_SECONDS,
        )
        metrics_port = _bounded_int(
            environment.get("GBOS_RETENTION_METRICS_PORT", str(DEFAULT_METRICS_PORT)),
            minimum=1_024,
            maximum=65_535,
        )
        emergency_stop_file = Path(
            environment.get("GBOS_EMERGENCY_STOP_FILE", str(DEFAULT_EMERGENCY_STOP_FILE))
        )
        if not emergency_stop_file.is_absolute():
            return 78
        runtime_clock = clock or _utc_now
        metrics = SchedulerMetrics(clock=runtime_clock)
        server = (server_factory or _PrometheusServer)(metrics, metrics_port)
    except OSError, TypeError, ValueError:
        return 78

    if stop_event is None:
        created_event = Event()
        _install_stop_handlers(created_event)
        event: StopSignal = created_event
    else:
        event = stop_event
    operation = run_once or (lambda: retention_worker_main(environ=environment))

    def guarded_operation() -> int:
        if _latch_exists(emergency_stop_file):
            return 78
        return operation()

    server.start()
    try:
        return run_loop(
            run_once=guarded_operation,
            interval_seconds=interval_seconds,
            stop_event=event,
            metrics=metrics,
        )
    finally:
        server.shutdown()


def _enabled(environment: Mapping[str, str]) -> bool:
    return (
        environment.get("GBOS_LOCAL_RUNTIME_ENABLED") == "true"
        and environment.get("GBOS_RETENTION_SCHEDULER_ENABLED") == "true"
        and environment.get("GBOS_RETENTION_SCHEDULER_KILL_SWITCH") == "false"
        and environment.get("GBOS_RETENTION_ENABLED") == "true"
        and environment.get("GBOS_RETENTION_DRY_RUN") == "false"
    )


def _bounded_int(value: str, *, minimum: int, maximum: int) -> int:
    parsed = int(value)
    if str(parsed) != value or not minimum <= parsed <= maximum:
        raise ValueError("scheduler integer setting is invalid")
    return parsed


def _latch_exists(path: Path) -> bool:
    try:
        return path.exists() or path.is_symlink()
    except OSError:
        return True


def _timestamp(value: datetime) -> float:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("scheduler clock must be timezone-aware")
    return value.astimezone(UTC).timestamp()


def _metric_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else format(value, ".6f").rstrip("0").rstrip(".")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _install_stop_handlers(event: Event) -> None:
    def stop(_signum: int, _frame: object) -> None:
        event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)


class _PrometheusServer:
    def __init__(self, metrics: SchedulerMetrics, port: int) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/metrics":
                    body = metrics.render().encode("ascii")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; version=0.0.4")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path == "/healthz":
                    body = b"ok\n"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_error(404)

            def log_message(self, _format: str, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        self._thread = Thread(
            target=self._server.serve_forever,
            name="retention-metrics",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["SchedulerMetrics", "main", "run_loop"]
