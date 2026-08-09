"""Default-off local model worker entrypoint."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import Event
from typing import Any

from services.agent_runtime.local_entrypoint import (
    LocalEntrypointDisabled,
    load_local_manifest,
    require_component_enabled,
)
from services.agent_runtime.local_runtime import (
    LocalRuntimeError,
    validate_deepseek_manifest,
)

from .runtime_support import (
    RuntimeConfig,
    RuntimeSupportError,
    component_settings,
    load_runtime_config,
    reject_plaintext_secret_environment,
    validate_manifest_binding,
)

DEFAULT_MANIFEST = Path("/config/local-pilot-manifest.json")
DEFAULT_RUNTIME_CONFIG = Path("/config/local-pilot-runtime.json")
DeepSeekRunner = Callable[[dict[str, Any], RuntimeConfig, Event], None]


def main(
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    runtime_config_path: Path = DEFAULT_RUNTIME_CONFIG,
    environ: Mapping[str, str] | None = None,
    deepseek_runner: DeepSeekRunner | None = None,
    stop_event: Event | None = None,
) -> int:
    environment = os.environ if environ is None else environ
    try:
        reject_plaintext_secret_environment(environment)
        manifest = load_local_manifest(manifest_path)
        require_component_enabled(
            manifest,
            component="model-worker",
            environ=environment,
        )
        config = load_runtime_config(runtime_config_path)
        validate_manifest_binding(manifest, config)
        component = component_settings(config, "model_worker")
        if component.provider_mode != "deepseek" or deepseek_runner is None:
            raise RuntimeSupportError("real DeepSeek composition runner is not injected")
        validate_deepseek_manifest(manifest)
        deepseek_runner(manifest, config, stop_event or Event())
        return 0
    except (
        LocalEntrypointDisabled,
        LocalRuntimeError,
        RuntimeSupportError,
        ValueError,
    ):
        return 78


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
