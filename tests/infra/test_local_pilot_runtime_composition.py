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
    service_match = re.search(rf"(?m)^  {re.escape(service)}:\s*$", compose)
    assert service_match is not None, f"service is absent: {service}"
    start = service_match.start()
    match = re.search(r"(?m)^  [a-z0-9][a-z0-9-]*:\s*$", compose[start + 1 :])
    end = start + 1 + match.start() if match else len(compose)
    return compose[start:end]


def test_runtime_containerfile_is_frozen_nonroot_and_contains_runtime_sources() -> None:
    containerfile = _read(INFRA / "Containerfile.runtime")
    lock = json.loads(_read(INFRA / "images.lock.json"))
    build = _read(SCRIPTS / "build-runtime-image")

    python_ref = (
        "python:3.14.2-slim-bookworm"
        "@sha256:977174a93ac5b559a077d2e2997bfde0d9b2c0e283a3fddb12747e35aea43689"
    )
    uv_ref = (
        "ghcr.io/astral-sh/uv:0.9.28"
        "@sha256:b400c36bce7aa9a84965ad23d0e4339ccffaaa06e825d08b064a0f40a2eb90ab"
    )
    assert f"ARG PYTHON_BASE_IMAGE={python_ref}" in containerfile
    assert f"ARG UV_IMAGE={uv_ref}" in containerfile
    assert "COPY --from=uv" in containerfile
    assert "pip install" not in containerfile
    assert "latest" not in containerfile
    assert "uv sync --frozen --no-dev" in containerfile
    assert "apt-get update" in containerfile
    assert "apt-get upgrade -y" in containerfile
    assert "rm -rf /var/lib/apt/lists/*" in containerfile
    assert "COPY pyproject.toml uv.lock" in containerfile
    assert re.search(r"(?m)^COPY (?:--chown=[^ ]+ )?services ", containerfile)
    assert re.search(r"(?m)^COPY (?:--chown=[^ ]+ )?contracts ", containerfile)
    assert re.search(r"(?m)^USER gbos$", containerfile)
    assert "HEALTHCHECK" in containerfile
    for service, reference in (
        ("python-runtime-base", python_ref),
        ("uv-builder", uv_ref),
    ):
        image = next(item for item in lock["images"] if item["service"] == service)
        assert image["reference"] == reference
        assert image["platform"] == "linux/arm64"
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", image["local_inspect_digest"])
        assert image["local_repo_digest"].endswith(f"@{reference.split('@', 1)[1]}")
    assert "--confirm-network-build" in build
    assert "--platform linux/arm64" in build
    assert "--pull" in build
    assert "--service python-runtime-base" in build
    assert "--service uv-builder" in build


def test_runtime_build_installs_pinned_base_images_before_recording_them() -> None:
    build = _read(SCRIPTS / "build-runtime-image")

    python_pull = 'docker pull --platform linux/arm64 "${PYTHON_BASE_IMAGE}"'
    uv_pull = 'docker pull --platform linux/arm64 "${UV_IMAGE}"'
    image_build = "docker build"
    image_record = '"${SCRIPT_DIR}/record-images"'

    assert python_pull in build
    assert uv_pull in build
    assert build.index(python_pull) < build.index(image_build)
    assert build.index(uv_pull) < build.index(image_build)
    assert build.index(image_build) < build.index(image_record)


def test_runtime_image_carries_verified_exact_source_identity() -> None:
    containerfile = _read(INFRA / "Containerfile.runtime")
    build = _read(SCRIPTS / "build-runtime-image")

    assert "ARG RUNTIME_SOURCE_COMMIT" in containerfile
    assert "ARG RUNTIME_SOURCE_SHA256" in containerfile
    assert 'LABEL org.opencontainers.image.revision="${RUNTIME_SOURCE_COMMIT}"' in containerfile
    assert 'LABEL com.esan.gbos.runtime-source-sha256="${RUNTIME_SOURCE_SHA256}"' in containerfile
    assert "RUNTIME_SOURCE_PATHS=(" in build
    assert "services" in build
    assert "contracts" in build
    assert "pyproject.toml" in build
    assert "uv.lock" in build
    assert "infra/local/Containerfile.runtime" in build
    assert "scripts/local-pilot/build-runtime-image" in build
    source_paths = build.split("RUNTIME_SOURCE_PATHS=(", 1)[1].split(")", 1)[0]
    assert "images.lock.json" not in source_paths
    assert '--build-arg "RUNTIME_SOURCE_COMMIT=${RUNTIME_SOURCE_COMMIT}"' in build
    assert '--build-arg "RUNTIME_SOURCE_SHA256=${RUNTIME_SOURCE_SHA256}"' in build
    revision_verify = build.index("org.opencontainers.image.revision")
    digest_verify = build.index("com.esan.gbos.runtime-source-sha256")
    record = build.index('"${SCRIPT_DIR}/record-images"')
    assert revision_verify < record
    assert digest_verify < record


def test_one_shot_migration_is_checksum_ordered_repeatable_and_least_privilege() -> None:
    migration = _read(SCRIPTS / "migrate")
    compose = _read(INFRA / "compose.yml")
    block = _service_block(compose, "migrations")

    assert 'profiles: ["runtime"]' in block
    assert 'restart: "no"' in block
    assert "condition: service_healthy" in block
    assert "- postgres_password" in block
    for role in ("observer", "context", "agent", "media"):
        assert f"- postgres_{role}_password" in block
    assert "/run/secrets/postgres_password" in migration
    assert "/run/secrets/postgres_app_password" not in migration
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
    for role in ("observer", "context", "agent", "media"):
        assert f"/run/secrets/postgres_{role}_password" in migration
    assert "\\copy local_secret_input" in migration
    assert "pg_read_file" not in migration
    assert "Invalid migration secret format" in migration
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
        "runtime-observer.json": ("gbos_observer_app", "postgres_observer_password"),
        "runtime-identity.json": ("gbos_observer_app", "postgres_observer_password"),
        "runtime-context.json": ("gbos_context_app", "postgres_context_password"),
        "runtime-agent.json": ("gbos_agent_app", "postgres_agent_password"),
        "runtime-media.json": ("gbos_media_app", "postgres_media_password"),
    }
    for name, (role, secret_name) in expected_roles.items():
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
        assert payload["postgres"]["password_file"] == f"/run/secrets/{secret_name}"
        assert all(
            value.startswith("/run/secrets/")
            for key, value in payload["auth"].items()
            if key.endswith("_file")
        )
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    identity = json.loads(_read(output / "runtime-identity.json"))
    assert identity["context_endpoint"] == {
        "base_url": "http://frappe-backend:8000",
        "unix_socket": None,
    }
    assert identity["auth"]["context_auth_ref"] == "observer-identity-resolver-v1"
    assert identity["worker"]["worker_id"] == "local-pilot-identity-worker"
    assert all(not component["enabled"] for component in identity["components"].values())

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
    projection = json.loads(_read(output / "projection-connections.json"))
    assert set(projection) == {
        "schema_version",
        "site_id",
        "controlled_egress",
        "evidence_cas_root",
        "tokenizer_vault_root",
        "connections",
    }
    assert projection["controlled_egress"] is False
    assert projection["evidence_cas_root"] == "/var/lib/gbos/evidence"
    assert projection["tokenizer_vault_root"] == "/var/lib/gbos/tokenizer-vault"
    assert set(projection["connections"]) == {"observer", "context", "agent"}
    assert {item["password_file"] for item in projection["connections"].values()} == {
        "/run/secrets/postgres_observer_password",
        "/run/secrets/postgres_context_password",
        "/run/secrets/postgres_agent_password",
    }
    assert stat.S_IMODE((output / "projection-connections.json").stat().st_mode) == 0o600


def test_synthetic_runtime_renderer_enables_only_core_without_relaxing_source_manifest(
    tmp_path: Path,
) -> None:
    output = tmp_path / "runtime"
    source = json.loads(_read(INFRA / "local-pilot-manifest.json"))
    result = subprocess.run(
        [
            str(SCRIPTS / "render-config"),
            "--manifest",
            str(INFRA / "local-pilot-manifest.json"),
            "--output-dir",
            str(output),
            "--synthetic-runtime",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    derived = json.loads(_read(output / "local-pilot-manifest.json"))
    assert source["local_pilot_go"] is False
    assert derived["local_pilot_go"] is True
    assert derived["local_pilot_status"] == "ready"
    assert derived["production_go"] is False
    assert all(value is False for value in derived["capabilities"].values())
    assert derived["deepseek"]["enabled"] is False
    assert all(channel["enabled"] is False for channel in derived["channels"].values())

    context = json.loads(_read(output / "runtime-context.json"))
    agent = json.loads(_read(output / "runtime-agent.json"))
    observer = json.loads(_read(output / "runtime-observer.json"))
    materialization = json.loads(_read(output / "runtime-materialization.json"))
    assert context["components"]["context_api"]["enabled"] is True
    assert agent["components"]["agent_api"]["enabled"] is True
    assert observer["components"]["model_worker"]["enabled"] is False
    assert agent["components"]["agent_worker"]["enabled"] is False
    assert materialization["components"]["agent_worker"]["enabled"] is True
    assert materialization["components"]["agent_worker"]["synthetic_e2e"] is True


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
        "identity-resolution-worker",
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


def test_local_bridge_publishes_loopback_ports_without_outbound_masquerade() -> None:
    compose = _read(INFRA / "compose.yml")
    networks = compose[compose.index("\nnetworks:\n") :]
    local = networks[
        networks.index("  local-internal:\n") : networks.index("  controlled-egress:\n")
    ]

    assert "driver: bridge" in local
    assert "com.docker.network.bridge.enable_ip_masquerade" in local
    assert '"false"' in local
    assert "internal: true" not in local
    webhook = networks[networks.index("  webhook-tunnel:\n") : networks.index("\nvolumes:\n")]
    assert "internal: true" in webhook


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
    assert 'export GBOS_DEMO_PASSWORD="$$(cat /run/secrets/frappe_demo_password)";' in bootstrap
    assert "esan_gbos.demo.seed" in bootstrap
    assert "confirm_synthetic" in bootstrap
    assert "127.0.0.1:8080/api/method/ping" in pwa
    assert "127.0.0.1:8080/gbos" not in pwa

    frappe = next(item for item in lock["images"] if item["service"] == "frappe-pwa")
    assert frappe["source"] == "local-build"
    assert frappe["reference"] == "esan-gbos-local-pilot-frappe:2026-08-08"
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", frappe["local_inspect_digest"])
    assert frappe["local_repo_digest"] is None
    assert "scripts/dev/build-custom-image" in build
    assert "--service frappe-pwa" in build


def test_frappe_site_keeps_multiline_bench_arguments_in_one_shell_command() -> None:
    compose = _read(INFRA / "compose.yml")
    site = _service_block(compose, "frappe-site")
    synthetic = _service_block(compose, "frappe-synthetic-bootstrap")
    materializer = _service_block(compose, "frappe-materializer-bootstrap")
    identity = _service_block(compose, "frappe-identity-resolver-bootstrap")

    assert 'bench new-site "$$site" \\' in site
    assert '--mariadb-user-host-login-scope="%" \\' in site
    assert "--db-root-username=root \\" in site
    assert '--db-root-password="$$(cat /run/secrets/mariadb_root_password)" \\' in site
    assert '--admin-password="$$(cat /run/secrets/frappe_admin_password)" \\' in site
    assert "--install-app erpnext \\" in site
    for key in (
        "gbos_observer_url",
        "gbos_observer_auth_ref",
        "gbos_observer_token_file",
        "gbos_agent_url",
        "gbos_agent_auth_ref",
        "gbos_agent_token_file",
    ):
        needle = 'bench --site "$$site" set-config ' + "\\" + "\n" + f"          {key} "
        assert needle in site
    assert 'bench --site "$$site" set-config -p \\' in site
    assert (
        'bench --site "${GBOS_FRAPPE_SITE_NAME:-gbos.localhost}" '
        "execute esan_gbos.demo.seed "
        """--kwargs '{"confirm_synthetic": True}'"""
    ) in synthetic
    assert (
        'bench --site "${GBOS_FRAPPE_SITE_NAME:-gbos.localhost}" '
        "execute esan_gbos.local_pilot.provision_materializer "
        """--kwargs "{'confirm_local_pilot': True}\""""
    ) in materializer
    assert (
        'bench --site "${GBOS_FRAPPE_SITE_NAME:-gbos.localhost}" '
        "execute esan_gbos.identity_resolver_service.provision_identity_resolver "
        """--kwargs "{'confirm_local_pilot': True}"""
    ) in identity


def test_identity_resolution_worker_is_internal_profile_gated_and_secret_separated() -> None:
    compose = _read(INFRA / "compose.yml")
    worker = _service_block(compose, "identity-resolution-worker")
    connector = _service_block(compose, "connector-worker")

    assert 'profiles: ["identity"]' in worker
    assert (
        'command: ["python", "-m", "services.local_pilot_runtime.identity_resolution_worker"]'
    ) in worker
    assert "GBOS_IDENTITY_RESOLUTION_KILL_SWITCH" in worker
    assert "runtime-identity.json:/config/local-pilot-runtime.json:ro" in worker
    assert "postgres_observer_password" in worker
    assert "identity_resolver_api_key" in worker
    assert "identity_resolver_api_secret" in worker
    assert "identity_hmac_key" not in worker
    assert "controlled-egress" not in worker
    assert "ports:" not in worker
    assert "identity_hmac_key" in connector
    assert "identity_resolver_api_key" not in connector
    assert "identity_resolver_api_secret" not in connector


def test_identity_resolver_bootstrap_is_profile_only_and_runs_before_worker() -> None:
    compose = _read(INFRA / "compose.yml")
    entrypoints = json.loads(_read(INFRA / "runtime-entrypoints.json"))
    bootstrap = _service_block(compose, "frappe-identity-resolver-bootstrap")
    site = _service_block(compose, "frappe-site")
    start = _read(SCRIPTS / "start")

    assert 'profiles: ["identity-bootstrap"]' in bootstrap
    assert "GBOS_IDENTITY_RESOLVER_API_KEY_FILE" in bootstrap
    assert "GBOS_IDENTITY_RESOLVER_API_SECRET_FILE" in bootstrap
    assert "test -s /run/secrets/frappe_identity_resolver_api_key" in bootstrap
    assert "test -s /run/secrets/frappe_identity_resolver_api_secret" in bootstrap
    assert "identity_hmac_key" not in bootstrap
    assert "gbos_identity_resolver_identities" in site
    assert "observer-identity-resolver-v1" in site
    assert "gbos-identity-resolver@localhost.invalid" in site
    assert '"processing_purposes":["identity_resolution"]' in site
    assert entrypoints["services"]["frappe-identity-resolver-bootstrap"]["status"] == ("executable")
    migration = "compose --profile runtime run --rm migrations"
    bootstrap_run = (
        "compose --profile runtime --profile identity-bootstrap "
        "run --rm --no-deps frappe-identity-resolver-bootstrap"
    )
    runtime_up = 'compose "${profile_args[@]}" up -d --wait'
    assert start.index(migration) < start.index(bootstrap_run) < start.index(runtime_up)


def test_materializer_identity_bootstrap_is_profile_only_and_secret_file_backed() -> None:
    compose = _read(INFRA / "compose.yml")
    entrypoints = json.loads(_read(INFRA / "runtime-entrypoints.json"))
    bootstrap = _service_block(compose, "frappe-materializer-bootstrap")
    site = _service_block(compose, "frappe-site")
    start = _read(SCRIPTS / "start")

    assert 'profiles: ["materializer-bootstrap"]' in bootstrap
    assert "- frappe_materializer_api_key" in bootstrap
    assert "- frappe_materializer_api_secret" in bootstrap
    assert "test -s /run/secrets/frappe_materializer_api_key" in bootstrap
    assert "test -s /run/secrets/frappe_materializer_api_secret" in bootstrap
    assert 'GBOS_MATERIALIZER_API_KEY="$$(cat ' in bootstrap
    assert 'GBOS_MATERIALIZER_API_SECRET="$$(cat ' in bootstrap
    assert "esan_gbos.local_pilot.provision_materializer" in bootstrap
    assert "--kwargs \"{'confirm_local_pilot': True}\"" in bootstrap
    assert "GBOS_LOCAL_PILOT_SITE_ID" in bootstrap
    assert 'GBOS_PRODUCTION_ENABLED: "false"' in bootstrap
    assert "echo" not in bootstrap
    assert "gbos_agent_materialization_identities" in site
    assert "agent-materializer-v1" in site
    assert "gbos-materializer@localhost.invalid" in site
    assert (
        '"observation_processing","sales_follow_up","procurement_coordination",'
        '"product_sample_management","metric_reporting"'
    ) in site
    assert entrypoints["services"]["frappe-materializer-bootstrap"]["status"] == "executable"
    assert (
        entrypoints["required_always"]["frappe-materializer-bootstrap"]
        == "apps/esan_gbos/esan_gbos/local_pilot.py"
    )
    migration = "compose --profile runtime run --rm migrations"
    bootstrap_run = (
        "compose --profile runtime --profile materializer-bootstrap "
        "run --rm --no-deps frappe-materializer-bootstrap"
    )
    runtime_up = 'compose "${profile_args[@]}" up -d --wait'
    assert migration in start
    assert bootstrap_run in start
    assert start.index(migration) < start.index(bootstrap_run) < start.index(runtime_up)


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
    projection = json.loads(_read(output / "projection-connections.json"))
    assert observer["components"]["model_worker"]["enabled"] is True
    assert observer["components"]["model_worker"]["provider_mode"] == "deepseek"
    assert agent["components"]["agent_worker"]["enabled"] is True
    assert agent["components"]["agent_worker"]["provider_mode"] == "deepseek"
    assert materialization["components"]["agent_worker"]["enabled"] is True
    assert materialization["components"]["agent_worker"]["kill_switch"] is False
    assert materialization["components"]["model_worker"]["enabled"] is False
    assert materialization["context_endpoint"]["base_url"] == "http://frappe-backend:8000"
    assert projection["controlled_egress"] is True
    assert {
        (connection["host"], connection["port"])
        for connection in projection["connections"].values()
    } == {("postgres", 5432)}

    compose = _service_block(_read(INFRA / "compose.yml"), "model-projection-worker")
    assert "GBOS_MODEL_PROJECTION_KILL_SWITCH" in compose
    assert "GBOS_DEEPSEEK_EGRESS_ENABLED" in compose


def test_blocked_entrypoints_are_honest_and_webhook_has_no_fake_health() -> None:
    entrypoints = json.loads(_read(INFRA / "runtime-entrypoints.json"))
    compose = _read(INFRA / "compose.yml")
    manifest = json.loads(_read(INFRA / "local-pilot-manifest.json"))

    assert entrypoints["composition"]["status"] == "not_composed"
    assert manifest["local_pilot_go"] is False
    for service in ("connector-worker", "webhook-ingress", "email-poller"):
        assert entrypoints["services"][service]["status"] == "executable"
    assert entrypoints["services"]["wecom-poller"]["status"] == "blocked_official_sdk"
    assert (
        entrypoints["services"]["model-projection-worker"]["status"]
        == "blocked_user_lexicon_and_credentials"
    )
    assert entrypoints["services"]["media-worker"]["status"] == "blocked"
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


def test_channel_commands_configs_and_profiles_match_the_concrete_entrypoints() -> None:
    compose = _read(INFRA / "compose.yml")
    entrypoints = json.loads(_read(INFRA / "runtime-entrypoints.json"))

    expected_commands = {
        "connector-worker": (
            'command: ["python", "-m", "services.local_pilot_runtime.observer_worker"]'
        ),
        "identity-resolution-worker": (
            'command: ["python", "-m", "services.local_pilot_runtime.identity_resolution_worker"]'
        ),
        "webhook-ingress": ('command: ["python", "-m", "services.local_pilot_runtime.webhook"]'),
        "email-poller": (
            'command: ["python", "-m", "services.local_pilot_runtime.pollers", "email"]'
        ),
        "wecom-poller": (
            'command: ["python", "-m", "services.local_pilot_runtime.pollers", "wecom"]'
        ),
    }
    for service, command in expected_commands.items():
        block = _service_block(compose, service)
        assert command in block
        assert "/config/local-pilot-manifest.json:ro" in block
        assert "/config/local-pilot-runtime.json:ro" in block
        if service != "identity-resolution-worker":
            assert "/config/connectors.json:ro" in block
            assert "GBOS_CONNECTOR_KILL_SWITCH" in block
        assert 'GBOS_EXTERNAL_SEND_ENABLED: "false"' in compose
        assert "condition: service_healthy" not in block

    assert entrypoints["required_when_enabled"]["wecom"] == {
        "wecom-poller": "services/local_pilot_runtime/pollers.py",
        "connector-worker": "services/local_pilot_runtime/observer_worker.py",
        "identity-resolution-worker": (
            "services/local_pilot_runtime/identity_resolution_worker.py"
        ),
        "frappe-identity-resolver-bootstrap": (
            "apps/esan_gbos/esan_gbos/identity_resolver_service.py"
        ),
    }


def test_exact_internal_urls_and_frappe_token_files_are_closed(tmp_path: Path) -> None:
    compose = _read(INFRA / "compose.yml")
    site = _service_block(compose, "frappe-site")
    backend = _service_block(compose, "frappe-backend")
    materialization = json.loads(_read(INFRA / "runtime-entrypoints.json"))["services"][
        "materialization-worker"
    ]

    for key, url in (
        ("observer", "http://observer-api:8003"),
        ("agent", "http://agent-api:8002"),
    ):
        assert f"gbos_{key}_url {url}" in site
        assert f"gbos_{key}_auth_ref local-pilot-context-auth-v1" in site
        assert f"gbos_{key}_token_file /run/secrets/agent_api_bearer" in site
    assert "gbos_context_url" not in site
    assert "gbos_observer_token " not in site
    assert "gbos_agent_token " not in site
    assert "source: agent_api_bearer" in backend
    assert "target: agent_api_bearer" in backend
    assert "mode: 0600" in backend

    rendered = tmp_path / "rendered"
    result = subprocess.run(
        [
            str(SCRIPTS / "render-config"),
            "--manifest",
            str(INFRA / "local-pilot-manifest.json"),
            "--output-dir",
            str(rendered),
            "--synthetic",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    agent = json.loads(_read(rendered / "runtime-agent.json"))
    materialization_config = json.loads(_read(rendered / "runtime-materialization.json"))
    assert agent["context_endpoint"]["base_url"] == "http://context-api:8001"
    assert agent["auth"]["context_auth_ref"] == "local-pilot-context-auth-v1"
    assert materialization_config["context_endpoint"]["base_url"] == "http://frappe-backend:8000"
    assert materialization_config["auth"]["context_auth_ref"] == "agent-materializer-v1"
    assert materialization["status"] == "executable"


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
        "identity-resolution-worker",
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
    assert "--pull" in build
    assert "--platform linux/arm64" in build
    assert "--confirm-network-build" in build
    assert "uv sync" in _read(INFRA / "Containerfile.runtime")
    assert "network access" in build
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
        "postgres_observer_password",
        "postgres_context_password",
        "postgres_agent_password",
        "postgres_media_password",
        "agent_api_bearer",
        "context_api_bearer",
        "context_client_bearer",
        "cursor_hmac_key",
        "tokenizer_hmac_key",
        "mapping_vault_key",
        "trusted_phrase_lexicon",
        "deepseek_api_key",
        "frappe_materializer_api_key",
        "frappe_materializer_api_secret",
        "identity_hmac_key",
        "frappe_identity_resolver_api_key",
        "frappe_identity_resolver_api_secret",
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
    assert "postgres_app_password" not in prepare
    assert "postgres_app_password" not in compose
    assert "useradd --uid 10001" in _read(INFRA / "Containerfile.runtime")
    assert "OrbStack" in _read(INFRA / "README.md")
    assert "uid 10001" in _read(INFRA / "README.md")
    assert "keychain://com.esan.gbos.local-pilot/trusted-phrase-lexicon" in prepare
    assert "keychain://com.esan.gbos.local-pilot/identity-hmac-key" in prepare
    assert "keychain://com.esan.gbos.local-pilot/frappe-identity-resolver-api-key" in prepare
    assert "keychain://com.esan.gbos.local-pilot/frappe-identity-resolver-api-secret" in prepare
    projection = _service_block(compose, "model-projection-worker")
    assert "- trusted_phrase_lexicon" in projection
    for unrelated in ("agent-worker", "materialization-worker", "connector-worker"):
        assert "trusted_phrase_lexicon" not in _service_block(compose, unrelated)
    entrypoints = json.loads(_read(INFRA / "runtime-entrypoints.json"))
    assert (
        entrypoints["services"]["model-projection-worker"]["status"]
        == "blocked_user_lexicon_and_credentials"
    )
