"""Default-off local Agent worker entrypoint."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from services.agent_runtime.local_entrypoint import (
    LocalEntrypointDisabled,
    load_local_manifest,
    require_component_enabled,
)

DEFAULT_MANIFEST = Path("/config/local-pilot-manifest.json")


def main(
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    environ: Mapping[str, str] | None = None,
) -> int:
    environment = os.environ if environ is None else environ
    try:
        manifest = load_local_manifest(manifest_path)
        require_component_enabled(
            manifest,
            component="agent-worker",
            environ=environment,
        )
    except LocalEntrypointDisabled:
        return 78
    return 78


if __name__ == "__main__":
    raise SystemExit(main())
