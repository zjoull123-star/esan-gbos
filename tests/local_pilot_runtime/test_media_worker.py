from __future__ import annotations

from threading import Event

import pytest

from services.local_pilot_runtime.media_worker import (
    LocalMediaWorkerConfig,
    LocalMediaWorkerEntrypoint,
    main,
)
from services.media_runtime.runtime import LocalMediaRuntime, MediaRuntimeConfig
from services.media_runtime.worker import WorkerRunResult, WorkerRunStatus


class FakeWorker:
    def __init__(self, stop_event: Event) -> None:
        self.calls = 0
        self.stop_event = stop_event

    def run_once(self) -> WorkerRunResult:
        self.calls += 1
        self.stop_event.set()
        return WorkerRunResult(status=WorkerRunStatus.SUCCEEDED)


class UnverifiedRuntime:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, *, stop_event: Event) -> int:
        self.calls += 1
        stop_event.set()
        return 1


def test_default_media_entrypoint_is_disabled_and_requires_no_runtime() -> None:
    entrypoint = LocalMediaWorkerEntrypoint()

    result = entrypoint.run(stop_event=Event())

    assert result == 0
    assert main() == 78
    assert "runtime=<redacted>" in repr(entrypoint)


def test_enabled_entrypoint_accepts_only_verified_offline_local_media_runtime() -> None:
    stop_event = Event()
    worker = FakeWorker(stop_event)
    runtime = LocalMediaRuntime(
        worker=worker,
        config=MediaRuntimeConfig(
            enabled=True,
            offline=True,
            allow_runtime_download=False,
            poll_interval_seconds=0.01,
        ),
    )
    entrypoint = LocalMediaWorkerEntrypoint(
        runtime=runtime,
        config=LocalMediaWorkerConfig(enabled=True, offline=True),
    )

    result = entrypoint.run(stop_event=stop_event)

    assert result == 1
    assert worker.calls == 1


def test_enabled_entrypoint_fails_closed_for_unverified_runtime_or_online_mode() -> None:
    runtime = UnverifiedRuntime()
    entrypoint = LocalMediaWorkerEntrypoint(
        runtime=runtime,
        config=LocalMediaWorkerConfig(enabled=True, offline=True),
    )

    assert entrypoint.run(stop_event=Event()) == 78
    assert runtime.calls == 0
    with pytest.raises(ValueError, match="offline_runtime_required"):
        LocalMediaWorkerConfig(enabled=True, offline=False)
