from __future__ import annotations

import json
from pathlib import Path

import pytest
from esan_gbos.domain.frontend_assets import load_vite_assets


def _write_manifest(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_load_vite_assets_returns_only_same_app_paths(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    _write_manifest(
        manifest,
        {
            "index.html": {
                "file": "assets/index-A1B2.js",
                "isEntry": True,
                "css": ["assets/index-C3D4.css"],
            }
        },
    )

    assets = load_vite_assets(manifest)

    assert assets.entry == "/assets/esan_gbos/frontend/assets/index-A1B2.js"
    assert assets.styles == ("/assets/esan_gbos/frontend/assets/index-C3D4.css",)


@pytest.mark.parametrize(
    "file_name",
    (
        "../private/site_config.json",
        "/api/method/frappe.auth.get_logged_user",
        "https://example.invalid/app.js",
        "assets/../../private/app.js",
    ),
)
def test_load_vite_assets_rejects_unsafe_paths(tmp_path: Path, file_name: str) -> None:
    manifest = tmp_path / "manifest.json"
    _write_manifest(
        manifest,
        {
            "index.html": {
                "file": file_name,
                "isEntry": True,
                "css": [],
            }
        },
    )

    with pytest.raises(ValueError, match="unsafe Vite asset path"):
        load_vite_assets(manifest)


def test_load_vite_assets_fails_closed_without_exactly_one_entry(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    _write_manifest(
        manifest,
        {
            "one.js": {"file": "assets/one.js", "isEntry": True},
            "two.js": {"file": "assets/two.js", "isEntry": True},
        },
    )

    with pytest.raises(ValueError, match="exactly one entry"):
        load_vite_assets(manifest)
