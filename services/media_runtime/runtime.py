from __future__ import annotations

from dataclasses import dataclass
from threading import Event
from typing import Protocol

from .worker import WorkerRunResult, WorkerRunStatus


@dataclass(frozen=True, slots=True)
class MediaRuntimeConfig:
    enabled: bool = False
    poll_interval_seconds: float = 1.0
    offline: bool = True
    allow_runtime_download: bool = False

    def __post_init__(self) -> None:
        if not self.offline or self.allow_runtime_download:
            raise ValueError("offline_runtime_required")
        if not 0.01 <= self.poll_interval_seconds <= 60:
            raise ValueError("poll_interval_out_of_bounds")


class MediaWorkerRunner(Protocol):
    def run_once(self) -> WorkerRunResult: ...


class LocalMediaRuntime:
    """Inert-by-default resident loop with no downloader or network client."""

    def __init__(self, *, worker: MediaWorkerRunner, config: MediaRuntimeConfig) -> None:
        self._worker = worker
        self._config = config

    def run(self, *, stop_event: Event) -> int:
        if not self._config.enabled:
            return 0
        processed = 0
        while not stop_event.is_set():
            result = self._worker.run_once()
            if result.status not in {WorkerRunStatus.IDLE, WorkerRunStatus.DISABLED}:
                processed += 1
            else:
                stop_event.wait(self._config.poll_interval_seconds)
        return processed


__all__ = ["LocalMediaRuntime", "MediaRuntimeConfig"]
