"""Shared default-off guards for local pilot runtime entrypoints."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "mode",
        "site_id",
        "compliance_state",
        "retention_days",
        "production_go",
        "local_pilot_go",
        "local_pilot_status",
        "capabilities",
        "deepseek",
        "channels",
    }
)


class LocalEntrypointDisabled(RuntimeError):
    """A local entrypoint was not explicitly enabled by both manifest and environment."""


def load_local_manifest(path: Path, *, max_bytes: int = 65_536) -> dict[str, Any]:
    manifest_path = Path(path)
    if not manifest_path.is_file() or manifest_path.stat().st_size > max_bytes:
        raise LocalEntrypointDisabled("local pilot manifest is absent or unbounded")
    try:
        value = json.loads(manifest_path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LocalEntrypointDisabled("local pilot manifest is invalid") from exc
    required = {
        "schema_version",
        "mode",
        "site_id",
        "local_pilot_go",
        "local_pilot_status",
        "deepseek",
    }
    if (
        not isinstance(value, dict)
        or not required.issubset(value)
        or not set(value).issubset(_TOP_LEVEL_FIELDS)
        or value.get("schema_version") != "1.0"
        or value.get("mode") != "local_pilot"
    ):
        raise LocalEntrypointDisabled("local pilot manifest is not a closed supported schema")
    return value


def require_component_enabled(
    manifest: Mapping[str, Any],
    *,
    component: str,
    environ: Mapping[str, str],
) -> None:
    if environ.get("GBOS_LOCAL_RUNTIME_ENABLED") != "true":
        raise LocalEntrypointDisabled(f"{component} is disabled by default")
    if manifest.get("local_pilot_go") is not True or manifest.get("local_pilot_status") not in {
        "ready",
        "running",
    }:
        raise LocalEntrypointDisabled(f"{component} is disabled by manifest")
    if component == "model-worker":
        deepseek = manifest.get("deepseek")
        if (
            not isinstance(deepseek, Mapping)
            or deepseek.get("enabled") is not True
            or deepseek.get("kill_switch") is not False
            or environ.get("GBOS_MODEL_KILL_SWITCH", "true") != "false"
        ):
            raise LocalEntrypointDisabled(f"{component} model path is disabled")


__all__ = [
    "LocalEntrypointDisabled",
    "load_local_manifest",
    "require_component_enabled",
]
