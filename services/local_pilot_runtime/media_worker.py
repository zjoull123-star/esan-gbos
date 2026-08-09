"""Default-disabled local media runtime entrypoint."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event

from services.media_runtime.runtime import LocalMediaRuntime


@dataclass(frozen=True, slots=True)
class LocalMediaWorkerConfig:
    enabled: bool = False
    offline: bool = True

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool or type(self.offline) is not bool:
            raise TypeError("media worker flags must be booleans")
        if not self.offline:
            raise ValueError("offline_runtime_required")


class LocalMediaWorkerEntrypoint:
    """Delegate only to a verified local runtime; never construct network capability."""

    __slots__ = ("_config", "_runtime")

    def __init__(
        self,
        *,
        runtime: LocalMediaRuntime | object | None = None,
        config: LocalMediaWorkerConfig | None = None,
    ) -> None:
        config = LocalMediaWorkerConfig() if config is None else config
        if not isinstance(config, LocalMediaWorkerConfig):
            raise TypeError("invalid media worker configuration")
        self._runtime = runtime
        self._config = config

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(runtime=<redacted>, "
            f"enabled={self._config.enabled}, offline={self._config.offline})"
        )

    def run(self, *, stop_event: Event) -> int:
        if not self._config.enabled:
            return 0
        if not isinstance(self._runtime, LocalMediaRuntime):
            return 78
        return self._runtime.run(stop_event=stop_event)


def main(
    *,
    runtime: LocalMediaRuntime | object | None = None,
    config: LocalMediaWorkerConfig | None = None,
    stop_event: Event | None = None,
) -> int:
    """CLI-compatible boundary with no environment or credential parsing."""

    config = LocalMediaWorkerConfig() if config is None else config
    if not config.enabled:
        return 78
    return LocalMediaWorkerEntrypoint(
        runtime=runtime,
        config=config,
    ).run(stop_event=stop_event or Event())


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "LocalMediaWorkerConfig",
    "LocalMediaWorkerEntrypoint",
    "main",
]
