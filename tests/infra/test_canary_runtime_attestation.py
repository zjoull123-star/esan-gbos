from __future__ import annotations

import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]
CANARY_ENTRYPOINTS = (
    "canary-preflight",
    "canary-evidence",
    "verify-canary-chain",
)
sys.path.insert(0, str(ROOT / "scripts" / "local-pilot"))


def _attestation() -> Any:
    module = ROOT / "scripts" / "local-pilot" / "canary_attestation.py"
    assert module.is_file(), "repository/image attestation helper is not implemented"
    return importlib.import_module("canary_attestation")


def _load_script(name: str) -> Any:
    path = ROOT / "scripts" / "local-pilot" / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("entrypoint", CANARY_ENTRYPOINTS)
def test_canary_entrypoint_reexecutes_with_repository_python_from_host_python(
    entrypoint: str,
) -> None:
    host_python = Path("/usr/bin/python3")
    if not host_python.is_file():
        pytest.skip("macOS host Python is unavailable")

    result = subprocess.run(
        [str(host_python), str(ROOT / "scripts" / "local-pilot" / entrypoint), "--help"],
        cwd=Path("/"),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()


@pytest.mark.parametrize("entrypoint", CANARY_ENTRYPOINTS)
def test_canary_entrypoint_fails_closed_without_repository_python(
    tmp_path: Path,
    entrypoint: str,
) -> None:
    launcher = ROOT / "scripts" / "local-pilot" / entrypoint
    copied = tmp_path / "scripts" / "local-pilot" / entrypoint
    copied.parent.mkdir(parents=True)
    shutil.copyfile(launcher, copied)

    result = subprocess.run(
        [str(Path("/usr/bin/python3")), str(copied), "--help"],
        cwd=Path("/"),
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )

    assert result.returncode == 78
    assert "repository Python 3.14" in result.stderr
    assert result.stdout == ""


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _source_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    (repo / "services").mkdir(parents=True)
    (repo / "services" / "worker.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "apps" / "esan_gbos").mkdir(parents=True)
    (repo / "apps" / "esan_gbos" / "hooks.py").write_text("app = 'gbos'\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Canary Test")
    _git(repo, "config", "user.email", "canary@example.invalid")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "source")
    return repo, _git(repo, "rev-parse", "HEAD")


def _local_image(
    *,
    image_id: str,
    revision: str,
    source_label: str,
    source_sha256: str,
) -> dict[str, Any]:
    return {
        "Id": image_id,
        "RepoDigests": [],
        "Os": "linux",
        "Architecture": "arm64",
        "Config": {
            "Labels": {
                "org.opencontainers.image.revision": revision,
                source_label: source_sha256,
            }
        },
    }


def test_image_attestation_binds_actual_identity_revision_source_and_dirty_state(
    tmp_path: Path,
) -> None:
    canary_attestation = _attestation()
    repo, revision = _source_repo(tmp_path)
    repository = canary_attestation.repository_attestation(repo)
    expected_id = "sha256:" + "a" * 64
    lock = {
        "images": [
            {
                "service": "local-runtime",
                "source": "local-build",
                "platform": "linux/arm64",
                "reference": "runtime:test",
                "local_inspect_digest": expected_id,
                "local_repo_digest": None,
            }
        ]
    }

    images, issues = canary_attestation.attest_required_images(
        repo,
        lock,
        {"local-runtime"},
        repository=repository,
        inspector=lambda _reference: _local_image(
            image_id=expected_id,
            revision=revision,
            source_label="com.esan.gbos.runtime-source-sha256",
            source_sha256=repository["source_groups"]["local-runtime"]["sha256"],
        ),
    )

    assert issues == []
    assert images[0]["identity_verified"] is True
    assert images[0]["source_verified"] is True
    assert images[0]["actual_inspect_digest"] == expected_id
    assert images[0]["revision"] == revision

    (repo / "services" / "worker.py").write_text("VALUE = 2\n", encoding="utf-8")
    dirty_repository = canary_attestation.repository_attestation(repo)
    _images, dirty_issues = canary_attestation.attest_required_images(
        repo,
        lock,
        {"local-runtime"},
        repository=dirty_repository,
        inspector=lambda _reference: _local_image(
            image_id=expected_id,
            revision=revision,
            source_label="com.esan.gbos.runtime-source-sha256",
            source_sha256=repository["source_groups"]["local-runtime"]["sha256"],
        ),
    )

    assert "local-runtime source inputs are dirty" in dirty_issues


def test_running_binding_rejects_container_image_that_does_not_match_attested_image() -> None:
    canary_attestation = _attestation()
    images = [
        {
            "service": "local-runtime",
            "actual_inspect_digest": "sha256:" + "a" * 64,
            "identity_verified": True,
            "source_verified": True,
        }
    ]
    rows = {
        "agent-api": {
            "Service": "agent-api",
            "State": "running",
            "Health": "healthy",
            "ID": "container-1",
        }
    }

    bindings, issues = canary_attestation.attest_running_services(
        {"agent-api"},
        rows,
        images,
        inspector=lambda _container: {"Image": "sha256:" + "b" * 64},
    )

    assert bindings[0]["verified"] is False
    assert issues == ["running service agent-api image identity mismatch"]


def test_status_marks_enabled_runtime_no_go_when_running_image_binding_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    status = _load_script("status.py")
    manifest = tmp_path / "manifest.json"
    entrypoints = tmp_path / "entrypoints.json"
    image_lock = tmp_path / "images.json"
    compose = tmp_path / "compose.yml"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    manifest.write_text(
        json.dumps({"local_pilot_go": True, "channels": {}, "deepseek": {}}),
        encoding="utf-8",
    )
    entrypoints.write_text(json.dumps({"composition": {"status": "composed"}}), encoding="utf-8")
    image_lock.write_text(
        json.dumps({"images": [], "recording_scope": "machine-inspected"}),
        encoding="utf-8",
    )
    compose.write_text("services: {}\n", encoding="utf-8")
    required = status._required_services({"local_pilot_go": True, "channels": {}, "deepseek": {}})
    rows = {
        service: {
            "Service": service,
            "State": "running",
            "Health": "healthy",
            "ID": f"container-{service}",
        }
        for service in required
    }
    monkeypatch.setattr(status, "_running_services", lambda **_kwargs: (required, rows, None))
    monkeypatch.setattr(
        status,
        "repository_attestation",
        lambda _root: {"head": "a" * 40, "dirty": False, "source_groups": {}},
        raising=False,
    )
    monkeypatch.setattr(
        status,
        "attest_required_images",
        lambda *_args, **_kwargs: ([], []),
        raising=False,
    )
    monkeypatch.setattr(
        status,
        "attest_running_services",
        lambda *_args, **_kwargs: ([], ["running service agent-api image identity mismatch"]),
        raising=False,
    )

    exit_code = status.main(
        [
            "--manifest",
            str(manifest),
            "--entrypoints",
            str(entrypoints),
            "--image-lock",
            str(image_lock),
            "--runtime-dir",
            str(runtime),
            "--compose-file",
            str(compose),
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 3
    assert payload["schema_version"] == "1.1"
    assert payload["verdict"] == "no_go"
    assert "running_images_unbound" in payload["no_go_reasons"]
    assert payload["runtime_attestation"]["running_images_verified"] is False


def test_evidence_rejects_new_status_without_bound_source_images_and_containers() -> None:
    evidence = _load_script("canary-evidence.py")

    issues = evidence._status_attestation_issues(
        {
            "schema_version": "1.1",
            "runtime_attestation": {
                "repository_source_verified": True,
                "required_images_verified": True,
                "running_images_verified": False,
            },
        }
    )

    assert issues == ["status running image attestation is unbound"]
