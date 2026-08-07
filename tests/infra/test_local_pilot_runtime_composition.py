from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[2]
INFRA = ROOT / "infra" / "local"
SCRIPTS = ROOT / "scripts" / "local-pilot"


def _read(path: Path) -> str:
    assert path.is_file(), f"required asset is missing: {path.relative_to(ROOT)}"
    return path.read_text(encoding="utf-8")


def _service_block(compose: str, service: str) -> str:
    start = compose.index(f"  {service}:")
    match = re.search(r"(?m)^  [a-z0-9][a-z0-9-]*:\s*$", compose[start + 1 :])
    end = start + 1 + match.start() if match else len(compose)
    return compose[start:end]


def test_runtime_containerfile_is_frozen_nonroot_and_contains_runtime_sources() -> None:
    containerfile = _read(INFRA / "Containerfile.runtime")

    assert re.search(r"(?m)^ARG PYTHON_BASE_IMAGE=python:3\.14\.[0-9]+-slim-", containerfile)
    assert "latest" not in containerfile
    assert "uv sync --frozen --no-dev" in containerfile
    assert "COPY pyproject.toml uv.lock" in containerfile
    assert re.search(r"(?m)^COPY (?:--chown=[^ ]+ )?services ", containerfile)
    assert re.search(r"(?m)^COPY (?:--chown=[^ ]+ )?contracts ", containerfile)
    assert re.search(r"(?m)^USER gbos$", containerfile)
    assert "HEALTHCHECK" in containerfile


def test_one_shot_migration_is_checksum_ordered_repeatable_and_least_privilege() -> None:
    migration = _read(SCRIPTS / "migrate")
    compose = _read(INFRA / "compose.yml")
    block = _service_block(compose, "migrations")

    assert 'profiles: ["runtime"]' in block
    assert 'restart: "no"' in block
    assert "condition: service_healthy" in block
    assert "- postgres_password" in block
    assert "- postgres_app_password" in block
    assert "/run/secrets/postgres_password" in migration
    assert "/run/secrets/postgres_app_password" in migration
    assert "observer context agent media" in migration
    assert "observer.schema_migrations" in migration
    assert "sha256sum" in migration
    assert "Applied migration checksum changed" in migration
    assert "--single-transaction" in migration
    for role in (
        "gbos_observer_app",
        "gbos_context_app",
        "gbos_agent_app",
        "gbos_media_app",
    ):
        assert role in migration
    assert migration.count("NOBYPASSRLS") >= 4
    assert "pg_read_file('/run/secrets/postgres_app_password')" in migration
    assert "scripts/dev" not in migration


def test_renderer_emits_closed_role_scoped_configs_with_secret_file_refs(tmp_path: Path) -> None:
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
    expected_roles = {
        "runtime-observer.json": "gbos_observer_app",
        "runtime-context.json": "gbos_context_app",
        "runtime-agent.json": "gbos_agent_app",
        "runtime-media.json": "gbos_media_app",
    }
    for name, role in expected_roles.items():
        path = output / name
        payload = json.loads(_read(path))
        assert set(payload) == {
            "schema_version",
            "site_id",
            "postgres",
            "auth",
            "context_endpoint",
            "listen",
            "components",
            "worker",
        }
        assert payload["postgres"]["user"] == role
        assert payload["postgres"]["password_file"] == "/run/secrets/postgres_app_password"
        assert all(
            value.startswith("/run/secrets/")
            for key, value in payload["auth"].items()
            if key.endswith("_file")
        )
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    connectors = json.loads(_read(output / "connectors.json"))
    assert set(connectors) == {
        "schema_version",
        "site_id",
        "external_send",
        "evidence_cas_root",
        "channels",
    }
    assert connectors["external_send"] is False
    assert connectors["evidence_cas_root"] == "/var/lib/gbos/evidence"
    assert set(connectors["channels"]) == {"email", "wecom", "whatsapp", "media"}
    serialized = json.dumps(connectors, sort_keys=True)
    assert "/run/secrets/" in serialized
    assert "keychain://" not in serialized
    assert stat.S_IMODE((output / "connectors.json").stat().st_mode) == 0o600


def test_compose_declares_full_isolated_topology_and_filesystem_cas_truth() -> None:
    compose = _read(INFRA / "compose.yml")
    readme = _read(INFRA / "README.md")
    required = {
        "postgres",
        "migrations",
        "context-api",
        "agent-api",
        "observer-api",
        "connector-worker",
        "model-projection-worker",
        "agent-worker",
        "materialization-worker",
        "webhook-ingress",
        "email-poller",
        "wecom-poller",
        "media-worker",
        "mariadb",
        "redis-cache",
        "redis-queue",
        "frappe-configurator",
        "frappe-site",
        "frappe-backend",
        "frappe-websocket",
        "frappe-worker",
        "frappe-scheduler",
        "pwa",
    }
    assert required <= set(re.findall(r"(?m)^  ([a-z0-9][a-z0-9-]*):\s*$", compose))
    assert "deepseek-worker:" not in compose
    assert "model_worker.py" not in _read(INFRA / "runtime-entrypoints.json")
    assert "minio" not in compose.lower()
    assert "object-store:" not in compose

    assert "local-pilot-evidence-cas" in compose
    for service in (
        "connector-worker",
        "model-projection-worker",
        "agent-worker",
        "media-worker",
    ):
        assert "/var/lib/gbos/evidence" in _service_block(compose, service)
    assert "filesystem CAS" in _read(INFRA / "runtime-entrypoints.json")
    assert "MinIO is not part of the required runtime" in readme
    assert "not_composed" in readme
    assert "filesystem CAS" in readme

    assert "0.0.0.0:" not in compose
    published = re.findall(r'(?m)^\s+- "([^"]+:\d{2,5})"\s*$', compose)
    assert published
    assert all(value.startswith("127.0.0.1:") for value in published)


def test_frappe_pwa_uses_local_image_two_migrations_and_explicit_synthetic_bootstrap() -> None:
    compose = _read(INFRA / "compose.yml")
    lock = json.loads(_read(INFRA / "images.lock.json"))
    site = _service_block(compose, "frappe-site")
    bootstrap = _service_block(compose, "frappe-synthetic-bootstrap")
    pwa = _service_block(compose, "pwa")
    build = _read(SCRIPTS / "build-frappe-image")

    assert 'x-frappe-image: &frappe-image "esan-gbos-local-pilot-frappe:2026-08-08"' in compose
    assert "--install-app erpnext" in site
    assert "bench --site" in site
    assert "install-app crm" in site
    assert "install-app esan_gbos" in site
    assert site.count(" migrate") >= 2

    assert 'profiles: ["synthetic-bootstrap"]' in bootstrap
    assert "- frappe_demo_password" in bootstrap
    assert 'export GBOS_DEMO_PASSWORD="$$(cat /run/secrets/frappe_demo_password)"' in bootstrap
    assert "esan_gbos.demo.seed" in bootstrap
    assert "confirm_synthetic" in bootstrap
    assert "127.0.0.1:8080/gbos" in pwa

    frappe = next(item for item in lock["images"] if item["service"] == "frappe-pwa")
    assert frappe["source"] == "local-build"
    assert frappe["reference"] == "esan-gbos-local-pilot-frappe:2026-08-08"
    assert frappe["local_inspect_digest"] is None
    assert frappe["local_repo_digest"] is None
    assert "scripts/dev/build-custom-image" in build
    assert "--service frappe-pwa" in build


def test_real_renderer_enables_direct_model_consumers_without_a_durable_queue(
    tmp_path: Path,
) -> None:
    manifest = json.loads(_read(INFRA / "local-pilot-manifest.json"))
    manifest["local_pilot_go"] = True
    manifest["local_pilot_status"] = "ready"
    manifest["deepseek"]["enabled"] = True
    manifest["deepseek"]["kill_switch"] = False
    candidate = tmp_path / "manifest.json"
    candidate.write_text(json.dumps(manifest), encoding="utf-8")
    output = tmp_path / "rendered"

    result = subprocess.run(
        [
            str(SCRIPTS / "render-config"),
            "--manifest",
            str(candidate),
            "--output-dir",
            str(output),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    observer = json.loads(_read(output / "runtime-observer.json"))
    agent = json.loads(_read(output / "runtime-agent.json"))
    materialization = json.loads(_read(output / "runtime-materialization.json"))
    assert observer["components"]["model_worker"]["enabled"] is True
    assert observer["components"]["model_worker"]["provider_mode"] == "deepseek"
    assert agent["components"]["agent_worker"]["enabled"] is True
    assert agent["components"]["agent_worker"]["provider_mode"] == "deepseek"
    assert materialization["components"]["agent_worker"]["enabled"] is True
    assert materialization["components"]["agent_worker"]["kill_switch"] is False
    assert materialization["components"]["model_worker"]["enabled"] is False
    assert materialization["context_endpoint"]["base_url"] == "http://frappe-backend:8000"

    compose = _service_block(_read(INFRA / "compose.yml"), "model-projection-worker")
    assert "GBOS_MODEL_PROJECTION_KILL_SWITCH" in compose
    assert "GBOS_DEEPSEEK_EGRESS_ENABLED" in compose


def test_blocked_entrypoints_are_honest_and_webhook_has_no_fake_health() -> None:
    entrypoints = json.loads(_read(INFRA / "runtime-entrypoints.json"))
    compose = _read(INFRA / "compose.yml")
    manifest = json.loads(_read(INFRA / "local-pilot-manifest.json"))

    assert entrypoints["composition"]["status"] == "not_composed"
    assert manifest["local_pilot_go"] is False
    for service in (
        "connector-worker",
        "model-projection-worker",
        "webhook-ingress",
        "email-poller",
        "wecom-poller",
        "media-worker",
    ):
        assert entrypoints["services"][service]["status"] == "blocked"
    assert entrypoints["services"]["observer-api"]["status"] == "executable"
    observer = _service_block(compose, "observer-api")
    assert "- agent_api_bearer" in observer
    assert "main(internal_network=True)" in observer
    assert "http://127.0.0.1:8003/health" in observer
    for service in ("context-api", "agent-api"):
        assert "main(internal_network=True)" in _service_block(compose, service)
    assert entrypoints["services"]["agent-worker"]["provider_topology"] == (
        "direct_shared_provider_factory"
    )
    assert entrypoints["services"]["model-projection-worker"]["provider_topology"] == (
        "direct_shared_provider_factory"
    )
    assert entrypoints["durable_model_queue"] is False

    webhook = _service_block(compose, "webhook-ingress")
    assert "healthcheck:" not in webhook
    assert "condition: service_healthy" not in _service_block(compose, "cloudflared")
    assert "webhook-ingress" in _read(INFRA / "cloudflared" / "config.yml")


def test_start_orders_config_and_migration_before_runtime_and_keeps_real_gate() -> None:
    start = _read(SCRIPTS / "start")
    stop = _read(SCRIPTS / "stop")
    emergency = _read(SCRIPTS / "emergency-stop")

    preflight = start.index('"${SCRIPT_DIR}/preflight"')
    secrets = start.index('"${SCRIPT_DIR}/prepare-secrets"')
    render = start.index('"${SCRIPT_DIR}/render-config"')
    migrate = start.index("run --rm migrations")
    runtime_up = start.rindex(" up -d --wait")
    assert preflight < secrets < render < migrate < runtime_up
    assert "--require-go" in start
    assert "--synthetic" not in start
    assert 'GBOS_LOCAL_RUNTIME_ENABLED: "true"' in _read(INFRA / "compose.yml")
    assert "GBOS_LOCAL_RUNTIME_ENABLED" not in _read(SCRIPTS / "lib.sh")

    executable_stop = "\n".join(
        line for line in stop.splitlines() if not line.lstrip().startswith("#")
    )
    assert "--volumes" not in executable_stop
    assert "local-pilot-evidence-cas" not in stop
    for worker in (
        "connector-worker",
        "model-projection-worker",
        "agent-worker",
        "materialization-worker",
        "email-poller",
        "wecom-poller",
        "media-worker",
        "webhook-ingress",
        "frappe-worker",
        "frappe-scheduler",
    ):
        assert worker in emergency


def test_synthetic_preflight_cannot_relax_real_go_gate() -> None:
    synthetic = subprocess.run(
        [
            str(SCRIPTS / "preflight"),
            "--repo-root",
            str(ROOT),
            "--manifest",
            str(INFRA / "local-pilot-manifest.json"),
            "--synthetic",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    real = subprocess.run(
        [
            str(SCRIPTS / "preflight"),
            "--repo-root",
            str(ROOT),
            "--manifest",
            str(INFRA / "local-pilot-manifest.json"),
            "--require-go",
            "--skip-runtime-image-check",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    conflict = subprocess.run(
        [
            str(SCRIPTS / "preflight"),
            "--repo-root",
            str(ROOT),
            "--synthetic",
            "--require-go",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert synthetic.returncode == 0, synthetic.stderr
    assert real.returncode != 0
    assert "local_pilot_go must be true" in real.stderr
    assert conflict.returncode != 0


def test_image_recording_is_atomic_and_never_invents_digest() -> None:
    lock = json.loads(_read(INFRA / "images.lock.json"))
    record = _read(SCRIPTS / "record-images")
    build = _read(SCRIPTS / "build-runtime-image")

    assert all(
        value is None or re.fullmatch(r"sha256:[0-9a-f]{64}", value)
        for item in lock["images"]
        for value in (item["local_inspect_digest"],)
    )
    assert "os.replace" in record
    assert '"docker", "image", "inspect"' in record
    assert "docker pull" not in record
    assert "docker build" in build
    assert "--pull=false" in build
    assert "docker image inspect" in build
    assert "Base image is not present locally" in build
    assert "record-images" in build
    assert "Containerfile.runtime" in build
    help_result = subprocess.run(
        [str(SCRIPTS / "record-images"), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert help_result.returncode == 0, help_result.stderr


def test_secret_materialization_covers_runtime_crypto_frappe_and_channels() -> None:
    prepare = _read(SCRIPTS / "prepare-secrets")
    compose = _read(INFRA / "compose.yml")
    required = {
        "postgres_password",
        "postgres_app_password",
        "agent_api_bearer",
        "context_api_bearer",
        "context_client_bearer",
        "cursor_hmac_key",
        "tokenizer_hmac_key",
        "mapping_vault_key",
        "deepseek_api_key",
        "frappe_materializer_api_key",
        "frappe_materializer_api_secret",
        "frappe_demo_password",
        "email_credential",
        "wecom_credential",
        "whatsapp_credential",
    }
    for name in required:
        assert name in prepare
        assert f"  {name}:" in compose
        assert f"/{name}" in compose
    assert "chmod 600" in prepare
    assert "keychain://" in prepare
    assert "write_optional_keychain_secret" in prepare
    assert re.search(
        r"write_optional_keychain_secret\s+\\?\s*frappe_demo_password",
        prepare,
    )
    assert not re.search(r"(?m)^\s*(?:DEEPSEEK_API_KEY|PASSWORD|TOKEN):", compose)
    assert os.environ.get("DEEPSEEK_API_KEY") is None or "DEEPSEEK_API_KEY" not in compose
