from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

from services.local_pilot_runtime.deployment_secret_preflight import (
    PREFLIGHT_ERROR_BINDING,
    PREFLIGHT_ERROR_CONTRACT,
)

ROOT = Path(__file__).parents[2]
LAUNCHER = ROOT / "scripts" / "deploy" / "preflight-secrets"
VALID_CONTRACT = (
    ROOT / "contracts" / "examples" / "gate6" / "deployment-secret-projection-valid.json"
)


def _clean_environment() -> dict[str, str]:
    return {
        "HOME": os.environ["HOME"],
        "PATH": os.environ["PATH"],
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def test_launcher_is_executable_and_uses_only_repository_bound_runtime() -> None:
    assert LAUNCHER.is_file()
    assert LAUNCHER.stat().st_mode & stat.S_IXUSR

    source = LAUNCHER.read_text(encoding="utf-8")
    assert "BASH_SOURCE[0]" in source
    assert '"${deploy_repo_root}/.venv/bin/python"' in source
    assert "services.local_pilot_runtime.deployment_secret_preflight" in source
    for forbidden in (
        "--repo-root",
        "--schema",
        "--secret-root",
        "GBOS_SECRET_ROOT",
        "python3",
    ):
        assert forbidden not in source


def test_launcher_binds_repository_from_its_own_path_when_called_elsewhere() -> None:
    result = subprocess.run(
        [str(LAUNCHER), "--help"],
        cwd=Path("/"),
        check=False,
        capture_output=True,
        text=True,
        env=_clean_environment(),
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()
    assert "--contract" in result.stdout
    assert "--site-id" in result.stdout
    assert "--environment" in result.stdout
    assert "--secret-root" not in result.stdout
    assert "--repo-root" not in result.stdout
    assert "--schema" not in result.stdout


def test_copied_launcher_fails_closed_without_repository_python(tmp_path: Path) -> None:
    copied = tmp_path / "scripts" / "deploy" / "preflight-secrets"
    copied.parent.mkdir(parents=True)
    shutil.copy2(LAUNCHER, copied)

    result = subprocess.run(
        [str(copied), "--help"],
        cwd=Path("/"),
        check=False,
        capture_output=True,
        text=True,
        env=_clean_environment(),
    )

    assert result.returncode == 78
    assert result.stdout == ""
    assert result.stderr.strip() == "DEPLOYMENT_SECRET_PREFLIGHT_RUNTIME_UNAVAILABLE"


def test_launcher_emits_only_stable_failure_code_without_starting_runtime() -> None:
    result = subprocess.run(
        [
            str(LAUNCHER),
            "--contract",
            str(VALID_CONTRACT),
            "--site-id",
            "wrong-site-distinctive",
            "--environment",
            "preproduction",
        ],
        cwd=Path("/"),
        check=False,
        capture_output=True,
        text=True,
        env=_clean_environment(),
    )

    assert result.returncode == 78
    assert result.stdout == ""
    assert result.stderr.strip() == PREFLIGHT_ERROR_BINDING
    assert "wrong-site-distinctive" not in result.stderr


def test_launcher_rejects_caller_supplied_root_with_only_a_stable_code() -> None:
    marker = "/tmp/distinctive-caller-secret-root"
    result = subprocess.run(
        [
            str(LAUNCHER),
            "--contract",
            str(VALID_CONTRACT),
            "--site-id",
            "gbos-site-001",
            "--environment",
            "preproduction",
            "--secret-root",
            marker,
        ],
        cwd=Path("/"),
        check=False,
        capture_output=True,
        text=True,
        env=_clean_environment(),
    )

    assert result.returncode == 78
    assert result.stdout == ""
    assert result.stderr.strip() == PREFLIGHT_ERROR_CONTRACT
    assert marker not in result.stderr
