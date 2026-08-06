from __future__ import annotations

import stat

import pytest

from .conftest import ROOT


@pytest.mark.parametrize("name", ["preflight", "plan"])
def test_release_cli_entrypoint_is_executable_and_offline(name: str) -> None:
    entrypoint = ROOT / "scripts" / "release" / name

    assert entrypoint.is_file()
    assert entrypoint.stat().st_mode & stat.S_IXUSR
    assert "uv run --offline" in entrypoint.read_text(encoding="utf-8")
