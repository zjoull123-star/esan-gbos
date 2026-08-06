from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_ASSET_PREFIX = "/assets/esan_gbos/frontend/"


@dataclass(frozen=True)
class FrontendAssets:
    entry: str
    styles: tuple[str, ...]


def _asset_url(value: object, *, suffix: str) -> str:
    if not isinstance(value, str):
        raise ValueError("unsafe Vite asset path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.parts[:1] != ("assets",)
        or ".." in path.parts
        or not value.endswith(suffix)
    ):
        raise ValueError("unsafe Vite asset path")
    return f"{_ASSET_PREFIX}{path.as_posix()}"


def load_vite_assets(manifest_path: Path) -> FrontendAssets:
    payload: object = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Vite manifest must be an object")

    entries = [
        value
        for value in payload.values()
        if isinstance(value, dict) and value.get("isEntry") is True
    ]
    if len(entries) != 1:
        raise ValueError("Vite manifest must contain exactly one entry")

    entry = entries[0]
    css = entry.get("css", [])
    if not isinstance(css, list):
        raise ValueError("Vite entry css must be a list")

    return FrontendAssets(
        entry=_asset_url(entry.get("file"), suffix=".js"),
        styles=tuple(_asset_url(item, suffix=".css") for item in css),
    )
