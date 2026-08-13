from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INFRA = ROOT / "infra/local"
SCRIPTS = ROOT / "scripts/local-pilot"


def _service_block(compose: str, service: str) -> str:
    start = compose.index(f"  {service}:\n")
    following = re.search(r"^  [a-z0-9][a-z0-9-]*:\s*$", compose[start + 3 :], re.MULTILINE)
    return compose[start:] if following is None else compose[start : start + 3 + following.start()]


def test_manifest_and_compose_keep_identity_projection_default_off_and_secret_separated() -> None:
    manifest = json.loads((INFRA / "local-pilot-manifest.json").read_text())
    compose = (INFRA / "compose.yml").read_text()
    identity = _service_block(compose, "identity-resolution-worker")
    gateway = _service_block(compose, "email-gateway-api")

    assert manifest["email_gateway"]["identity_projection_kill_switch"] is True
    assert "GBOS_IDENTITY_PROJECTION_KILL_SWITCH" in identity
    assert "identity_projection_bearer" in identity
    assert "postgres_observer_identity_projector_password" in identity
    assert "identity_projection_bearer" in gateway
    assert "postgres_observer_identity_projector_password" not in gateway
    assert "identity_projection_bearer:" in compose
    assert "postgres_observer_identity_projector_password:" in compose
    assert "GBOS_IDENTITY_PROJECTION_BEARER" not in compose


def test_renderer_declares_closed_identity_projection_ingest_credential(tmp_path: Path) -> None:
    output = tmp_path / "rendered"
    result = subprocess.run(
        [
            str(SCRIPTS / "render-config"),
            "--manifest",
            str(INFRA / "local-pilot-manifest.json"),
            "--output-dir",
            str(output),
            "--synthetic",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    gateway = json.loads((output / "runtime-email-gateway-api.json").read_text())
    assert gateway["auth"]["identity_projection_bearer_file"] == (
        "/run/secrets/identity_projection_bearer"
    )
    assert gateway["auth"]["identity_projection_auth_ref"] == ("observer-identity-projection-v1")


def test_secret_preparation_migration_and_entrypoint_use_dedicated_projector_role() -> None:
    prepare = (SCRIPTS / "prepare-secrets").read_text()
    migrate = (SCRIPTS / "migrate").read_text()
    entrypoints = json.loads((INFRA / "runtime-entrypoints.json").read_text())

    assert "identity_projection_bearer" in prepare
    assert "keychain://com.esan.gbos.local-pilot/identity-projection-bearer" in prepare
    assert "postgres_observer_identity_projector_password" in prepare
    assert "gbos_observer_identity_projector" in migrate
    assert "/run/secrets/postgres_observer_identity_projector_password" in migrate
    assert entrypoints["services"]["identity-resolution-worker"]["database_roles"] == [
        "gbos_observer_app",
        "gbos_observer_identity_projector",
    ]
