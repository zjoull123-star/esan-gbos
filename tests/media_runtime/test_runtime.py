from __future__ import annotations

from threading import Event

import pytest

from services.media_runtime.runtime import LocalMediaRuntime, MediaRuntimeConfig
from services.media_runtime.worker import WorkerRunResult, WorkerRunStatus


class RecordingWorker:
    def __init__(
        self,
        stop: Event,
        *,
        status: WorkerRunStatus = WorkerRunStatus.IDLE,
    ) -> None:
        self.calls = 0
        self.stop = stop
        self.status = status

    def run_once(self) -> WorkerRunResult:
        self.calls += 1
        self.stop.set()
        return WorkerRunResult(status=self.status)


def test_runtime_is_inert_when_disabled() -> None:
    stop = Event()
    worker = RecordingWorker(stop)
    runtime = LocalMediaRuntime(
        worker=worker,
        config=MediaRuntimeConfig(
            enabled=False,
            poll_interval_seconds=0.01,
            offline=True,
            allow_runtime_download=False,
        ),
    )

    result = runtime.run(stop_event=stop)

    assert result == 0
    assert worker.calls == 0


def test_runtime_processes_until_stop_without_network_or_download_capability() -> None:
    stop = Event()
    worker = RecordingWorker(stop, status=WorkerRunStatus.SUCCEEDED)
    runtime = LocalMediaRuntime(
        worker=worker,
        config=MediaRuntimeConfig(
            enabled=True,
            poll_interval_seconds=0.01,
            offline=True,
            allow_runtime_download=False,
        ),
    )

    result = runtime.run(stop_event=stop)

    assert result == 1
    assert worker.calls == 1


@pytest.mark.parametrize(
    ("offline", "allow_runtime_download"),
    [(False, False), (True, True), (False, True)],
)
def test_runtime_rejects_network_or_runtime_download_configuration(
    offline: bool,
    allow_runtime_download: bool,
) -> None:
    with pytest.raises(ValueError, match="offline_runtime_required"):
        MediaRuntimeConfig(
            enabled=True,
            offline=offline,
            allow_runtime_download=allow_runtime_download,
        )
