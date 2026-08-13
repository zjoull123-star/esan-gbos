from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[2]


def _block(text: str, service: str) -> str:
    start = text.index(f"\n  {service}:\n") + 1
    match = re.search(r"(?m)^  [a-z0-9][a-z0-9-]*:\s*$", text[start + 1 :])
    end = start + 1 + match.start() if match else len(text)
    return text[start:end]


def test_gateway_and_relays_are_default_killed_least_privilege_and_local_only() -> None:
    compose = (ROOT / "infra/local/compose.yml").read_text()
    gateway = _block(compose, "email-gateway-api")
    frappe_site = _block(compose, "frappe-site")
    frappe_backend = _block(compose, "frappe-backend")
    gateway_worker = _block(compose, "email-gateway-worker")
    publication = _block(compose, "observer-email-publication-worker")
    projection = _block(compose, "mailbox-config-projection-worker")
    frappe_worker = _block(compose, "frappe-worker")
    frappe_scheduler = _block(compose, "frappe-scheduler")
    observer_api = _block(compose, "observer-api")
    connector = _block(compose, "connector-worker")

    assert "GBOS_EMAIL_GATEWAY_KILL_SWITCH: ${GBOS_EMAIL_GATEWAY_KILL_SWITCH:-true}" in gateway
    assert (
        "GBOS_EMAIL_PUBLICATION_KILL_SWITCH: ${GBOS_EMAIL_PUBLICATION_KILL_SWITCH:-true}"
        in publication
    )
    assert 'GBOS_EXTERNAL_SEND_ENABLED: "false"' in gateway
    assert "controlled-egress" not in gateway
    assert "email_credential" not in gateway
    assert "wecom_credential" not in gateway
    assert "local-internal" in gateway
    assert "postgres_email_gateway_password" in gateway
    assert "postgres_email_command_executor_password" in gateway
    assert "email_gateway_command_ingest_bearer" in gateway
    for secret in (
        "frappe_email_gateway_authority_api_key",
        "frappe_email_gateway_authority_api_secret",
    ):
        assert f"source: {secret}" in gateway
        assert f"target: {secret}" in gateway
        assert "mode: 0400" in gateway
        for service in (
            frappe_site,
            frappe_backend,
            gateway_worker,
            publication,
            projection,
            frappe_worker,
            frappe_scheduler,
            observer_api,
            connector,
        ):
            assert secret not in service
    assert (
        "GBOS_EMAIL_COMMAND_INGEST_KILL_SWITCH: "
        "${GBOS_EMAIL_COMMAND_INGEST_KILL_SWITCH:-true}" in gateway
    )
    assert "postgres_observer_publisher_password" not in gateway
    assert "postgres_observer_publisher_password" in publication
    assert "postgres_email_gateway_password" not in publication
    assert "postgres_email_gateway_password" in projection
    assert "postgres_observer_publisher_password" not in projection
    for service in (gateway, frappe_site, frappe_backend):
        assert "source: email_gateway_bff_bearer" in service
        assert "target: email_gateway_bff_bearer" in service
        assert "mode: 0600" in service
    for service in (gateway_worker, publication, projection, frappe_worker, frappe_scheduler):
        assert "email_gateway_bff_bearer" not in service
        assert "postgres_email_command_executor_password" not in service
    for service in (frappe_site, frappe_backend, observer_api):
        assert "source: observer_email_draft_material_bearer" in service
        assert "target: observer_email_draft_material_bearer" in service
        assert "mode: 0600" in service
    for service in (
        gateway,
        gateway_worker,
        publication,
        projection,
        frappe_worker,
        frappe_scheduler,
    ):
        assert "observer_email_draft_material_bearer" not in service
    assert "identity_hmac_key" in observer_api
    assert "identity_hmac_key" in connector
    for service in (gateway, frappe_site, frappe_backend):
        assert "identity_hmac_key" not in service
    assert "gbos_email_gateway_url http://email-gateway-api:8004" in frappe_site
    assert "gbos_email_gateway_auth_ref email-gateway-bff-v1" in frappe_site
    assert "gbos_email_gateway_token_file /run/secrets/email_gateway_bff_bearer" in frappe_site
    assert (
        "email_gateway_bff_bearer:\n"
        "    file: ${GBOS_SECRET_DIR:-/tmp/gbos-local-pilot-secrets-unavailable}/"
        "email_gateway_bff_bearer"
    ) in compose
    assert (
        "observer_email_draft_material_bearer:\n"
        "    file: ${GBOS_SECRET_DIR:-/tmp/gbos-local-pilot-secrets-unavailable}/"
        "observer_email_draft_material_bearer"
    ) in compose
    for service in (gateway, frappe_site, frappe_backend):
        assert "email_credential" not in service
        assert "wecom_credential" not in service


def test_renderer_emits_role_separated_gateway_configs(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            str(ROOT / "scripts/local-pilot/render-config"),
            "--manifest",
            str(ROOT / "infra/local/local-pilot-manifest.json"),
            "--output-dir",
            str(tmp_path),
            "--synthetic",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    expected = {
        "runtime-email-gateway-api.json": "gbos_email_gateway_app",
        "runtime-email-gateway-worker.json": "gbos_email_gateway_worker",
        "runtime-email-publication-worker.json": "gbos_observer_publisher",
        "runtime-mailbox-config-projection-worker.json": "gbos_email_gateway_worker",
    }
    for name, role in expected.items():
        payload = json.loads((tmp_path / name).read_text())
        assert payload["postgres"]["user"] == role
        assert payload["external_send"] is False
        assert all(item["kill_switch"] for item in payload["components"].values())
        assert payload["auth"]["email_gateway_bff_bearer_file"] == (
            "/run/secrets/email_gateway_bff_bearer"
        )
        assert payload["auth"]["email_gateway_bff_auth_ref"] == "email-gateway-bff-v1"
        assert payload["auth"]["observer_email_draft_material_bearer_file"] == (
            "/run/secrets/observer_email_draft_material_bearer"
        )
        assert payload["auth"]["observer_email_draft_material_auth_ref"] == (
            "observer-email-draft-material-v1"
        )
        assert payload["auth"]["frappe_email_gateway_authority_api_key_file"] == (
            "/run/secrets/frappe_email_gateway_authority_api_key"
        )
        assert payload["auth"]["frappe_email_gateway_authority_api_secret_file"] == (
            "/run/secrets/frappe_email_gateway_authority_api_secret"
        )
        assert payload["auth"]["frappe_email_gateway_authority_auth_ref"] == (
            "email-gateway-authority-v1"
        )

    runtime = json.loads((ROOT / "infra/local/runtime-entrypoints.json").read_text())
    assert runtime["services"]["observer-api"]["email_draft_material"] == {
        "auth_ref": "observer-email-draft-material-v1",
        "bearer_file": "/run/secrets/observer_email_draft_material_bearer",
        "network": "local-internal-only",
    }
    assert runtime["services"]["observer-api"]["email_mailbox_identity"] == {
        "path": "/internal/v1/bff/email-mailbox-identity/derive",
        "auth_ref": "observer-email-draft-material-v1",
        "bearer_file": "/run/secrets/observer_email_draft_material_bearer",
        "identity_key_file": "/run/secrets/identity_hmac_key",
        "processing_purpose": "email_mailbox_identity",
        "resolver_purpose": "observation_processing",
        "network": "local-internal-only",
    }


def test_manifest_has_closed_revisioned_mailbox_list_and_default_switches() -> None:
    manifest = json.loads((ROOT / "infra/local/local-pilot-manifest.json").read_text())
    gateway = manifest["email_gateway"]
    assert gateway["kill_switch"] is True
    assert gateway["publication_kill_switch"] is True
    assert gateway["external_send"] is False
    assert gateway["mailboxes"] == []


def test_command_relay_and_fake_send_worker_are_profile_only_closed_and_least_secret() -> None:
    compose = (ROOT / "infra/local/compose.yml").read_text()
    frappe_site = _block(compose, "frappe-site")
    bootstrap = _block(compose, "frappe-email-command-publication-bootstrap")
    authority_bootstrap = _block(compose, "frappe-email-gateway-authority-bootstrap")
    relay = _block(compose, "email-command-publication-worker")
    sender = _block(compose, "email-send-worker")

    assert 'profiles: ["email-approved-outbound"]' in bootstrap
    assert 'profiles: ["email-approved-outbound"]' in authority_bootstrap
    assert 'profiles: ["email-approved-outbound"]' in relay
    assert 'profiles: ["email-approved-outbound"]' in sender
    assert all("local-internal" in service for service in (bootstrap, authority_bootstrap, relay))
    assert "controlled-egress" not in bootstrap + authority_bootstrap + relay + sender
    assert (
        "esan_gbos.email_gateway_authority_service.provision_email_gateway_authority"
        in authority_bootstrap
    )
    assert "GBOS_EMAIL_GATEWAY_AUTHORITY_API_KEY_FILE" in authority_bootstrap
    assert "GBOS_EMAIL_GATEWAY_AUTHORITY_API_SECRET_FILE" in authority_bootstrap
    assert "gbos_email_gateway_authority_identities" in frappe_site
    assert "email-gateway-authority@localhost.invalid" in frappe_site
    assert '"processing_purposes":["email_gateway_authority"]' in frappe_site
    authority_dependency = (
        "frappe-email-gateway-authority-bootstrap:\n"
        "        condition: service_completed_successfully"
    )
    assert authority_dependency in relay
    assert "GBOS_EMAIL_COMMAND_PUBLICATION_KILL_SWITCH" in relay
    assert "GBOS_EMAIL_SEND_KILL_SWITCH" in sender
    for secret in (
        "frappe_email_command_publication_api_key",
        "frappe_email_command_publication_api_secret",
        "email_gateway_command_ingest_bearer",
    ):
        assert secret in relay
    for secret in (
        "frappe_email_gateway_authority_api_key",
        "frappe_email_gateway_authority_api_secret",
    ):
        assert secret not in relay
        assert secret not in sender
        assert secret in authority_bootstrap
        assert secret not in bootstrap
    assert "postgres" not in relay
    assert "provider" not in relay
    assert "frappe_email_command_publication_api_key" not in sender
    assert "email_gateway_command_ingest_bearer" not in sender

    manifest = json.loads((ROOT / "infra/local/local-pilot-manifest.json").read_text())
    gateway = manifest["email_gateway"]
    assert gateway["command_publication_kill_switch"] is True
    assert gateway["send_kill_switch"] is True
    assert gateway["external_send"] is False

    runtime = json.loads((ROOT / "infra/local/runtime-entrypoints.json").read_text())
    assert runtime["services"]["email-command-publication-worker"] == {
        "enabled": False,
        "network": "local-internal-only",
        "frappe_url": "http://frappe-backend:8000",
        "gateway_url": "http://email-gateway-api:8004",
    }


def test_outbound_runtime_configs_and_secret_preparation_remain_provider_free(
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        [
            str(ROOT / "scripts/local-pilot/render-config"),
            "--manifest",
            str(ROOT / "infra/local/local-pilot-manifest.json"),
            "--output-dir",
            str(tmp_path),
            "--synthetic",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    relay = json.loads((tmp_path / "runtime-email-command-publication-worker.json").read_text())
    sender = json.loads((tmp_path / "runtime-email-send-worker.json").read_text())
    assert relay["enabled"] is False and relay["kill_switch"] is True
    assert relay["external_send"] is False
    assert set(relay["auth"]) == {
        "frappe_api_key_file",
        "frappe_api_secret_file",
        "gateway_bearer_file",
    }
    assert "postgres" not in relay
    assert sender["enabled"] is False and sender["kill_switch"] is True
    assert sender["external_send"] is False
    assert sender["provider_mode"] == "fake_disabled"
    assert sender["postgres"]["user"] == "gbos_email_send_worker"

    prepare = (ROOT / "scripts/local-pilot/prepare-secrets").read_text()
    start = (ROOT / "scripts/local-pilot/start").read_text()
    for secret in (
        "frappe_email_command_publication_api_key",
        "frappe_email_command_publication_api_secret",
        "frappe_email_gateway_authority_api_key",
        "frappe_email_gateway_authority_api_secret",
        "email_gateway_command_ingest_bearer",
        "postgres_email_command_executor_password",
        "postgres_email_send_worker_password",
    ):
        assert secret in prepare
    assert 'GBOS_EMAIL_COMMAND_PUBLICATION_KILL_SWITCH="true"' in start
    assert 'GBOS_EMAIL_SEND_KILL_SWITCH="true"' in start
    assert 'GBOS_FAKE_EMAIL_SEND_ENABLED="false"' in start
    assert 'GBOS_EXTERNAL_SEND_ENABLED="false"' in start
    assert "email-approved-outbound" not in start


def test_default_closed_outbound_secrets_are_sentinels_not_keychain_requirements() -> None:
    prepare = (ROOT / "scripts/local-pilot/prepare-secrets").read_text()
    optional_loop = prepare[prepare.index("# Compose declares every optional secret") :]
    for secret in (
        "frappe_email_command_publication_api_key",
        "frappe_email_command_publication_api_secret",
        "frappe_email_gateway_authority_api_key",
        "frappe_email_gateway_authority_api_secret",
        "email_gateway_command_ingest_bearer",
        "postgres_email_command_executor_password",
        "postgres_email_send_worker_password",
    ):
        assert f"write_keychain_secret \\\n  {secret} " not in prepare
        assert f"write_optional_keychain_secret \\\n  {secret} " in prepare
        assert secret in optional_loop


def test_optional_outbound_keychain_values_materialize_before_empty_sentinels() -> None:
    prepare = (ROOT / "scripts/local-pilot/prepare-secrets").read_text()
    sentinel = prepare.index("# Compose declares every optional secret")
    for secret in (
        "frappe_email_command_publication_api_key",
        "frappe_email_command_publication_api_secret",
        "frappe_email_gateway_authority_api_key",
        "frappe_email_gateway_authority_api_secret",
        "email_gateway_command_ingest_bearer",
        "postgres_email_command_executor_password",
        "postgres_email_send_worker_password",
    ):
        reader = prepare.index(f"write_optional_keychain_secret \\\n  {secret} ")
        assert reader < sentinel
        assert 'if [[ ! -f "${secret_dir}/${optional}" ]]' in prepare[sentinel:]

    compose = (ROOT / "infra/local/compose.yml").read_text()
    bootstrap = _block(compose, "frappe-email-command-publication-bootstrap")
    assert "test -s /run/secrets/frappe_email_command_publication_api_key" in bootstrap
    assert "test -s /run/secrets/frappe_email_command_publication_api_secret" in bootstrap


def test_renderer_refuses_to_double_run_the_legacy_email_poller(tmp_path: Path) -> None:
    manifest = json.loads((ROOT / "infra/local/local-pilot-manifest.json").read_text())
    manifest["channels"]["email"].update(
        {
            "enabled": True,
            "activation_time": "2026-08-13T09:00:00Z",
            "credential_ref": "keychain://com.esan.gbos.local-pilot/legacy-email",
        }
    )
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))

    result = subprocess.run(
        [
            str(ROOT / "scripts/local-pilot/render-config"),
            "--manifest",
            str(path),
            "--output-dir",
            str(tmp_path / "rendered"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 78
    assert "legacy Email channel must be disabled" in result.stderr


def test_migration_materializes_gateway_roles_before_reusing_secret_input() -> None:
    migrate = (ROOT / "scripts/local-pilot/migrate").read_text()
    gateway_copy = migrate.index(
        "\\copy local_secret_input(password) FROM '/run/secrets/postgres_email_gateway_password'"
    )
    app_insert = migrate.index("SELECT 'gbos_email_gateway_app', password", gateway_copy)
    worker_insert = migrate.index("SELECT 'gbos_email_gateway_worker', password", app_insert)
    first_truncate = migrate.index("TRUNCATE local_secret_input;", app_insert)
    publisher_copy = migrate.index(
        "\\copy local_secret_input(password) FROM "
        "'/run/secrets/postgres_observer_publisher_password'",
        worker_insert,
    )

    assert gateway_copy < app_insert < worker_insert < first_truncate < publisher_copy


def test_migration_conditionally_materializes_outbound_roles_from_distinct_secrets() -> None:
    compose = (ROOT / "infra/local/compose.yml").read_text()
    migrations = _block(compose, "migrations")
    migrate = (ROOT / "scripts/local-pilot/migrate").read_text()

    for role, secret in (
        ("gbos_email_command_executor", "postgres_email_command_executor_password"),
        ("gbos_email_send_worker", "postgres_email_send_worker_password"),
    ):
        assert secret in migrations
        copy = migrate.index(f"\\copy local_secret_input(password) FROM '/run/secrets/{secret}'")
        insert = migrate.index(f"SELECT '{role}', password", copy)
        truncate = migrate.index("TRUNCATE local_secret_input;", insert)
        assert copy < insert < truncate
        assert f"'{role}'" in migrate[migrate.index("SELECT format(", truncate) :]

    assert "for optional_secret_file in" in migrate
    assert '[[ -s "${optional_secret_file}" ]] || continue' in migrate
    assert "local role secret import failed closed" in migrate


def test_email_gateway_retention_and_alert_contract_is_closed() -> None:
    alerts = (ROOT / "infra/local/prometheus/alerts.yml").read_text()
    group_start = alerts.index("  - name: email-gateway\n")
    next_group = alerts.find("\n  - name:", group_start + 1)
    group = alerts[group_start : next_group if next_group >= 0 else len(alerts)]

    assert group.count("- alert:") == 4
    for alert, expression, duration in (
        (
            "EmailGatewayWorkerHeartbeatStale",
            "gbos_email_gateway_worker_heartbeat_age_seconds > 30",
            "2m",
        ),
        (
            "EmailGatewayDeadLetterIncrease",
            "increase(gbos_email_gateway_dead_letter_total[5m]) > 0",
            "5m",
        ),
        (
            "EmailGatewayPublicationBacklogStale",
            'max(gbos_email_gateway_publication_oldest_age_seconds{state=~"queued|retry"}) > 300',
            "10m",
        ),
        (
            "EmailGatewaySlaOverdue",
            "gbos_email_gateway_sla_overdue > 0",
            "15m",
        ),
    ):
        entry_start = group.index(f"- alert: {alert}")
        entry_end = group.find("\n      - alert:", entry_start + 1)
        entry = group[entry_start : entry_end if entry_end >= 0 else len(group)]
        assert f"expr: {expression}" in entry
        assert f"for: {duration}" in entry

    migration = (
        ROOT / "services/email_gateway/migrations/006_email_gateway_human_retention.sql"
    ).read_text()
    for required in (
        "FOR UPDATE SKIP LOCKED",
        "lease_generation",
        "idempotency_key",
        "observer_tombstone_receipt_ref",
        "legal_hold_ref",
        "interval '30 days'",
    ):
        assert required in migration
    assert "GRANT DELETE" not in migration
    assert "DELETE FROM observer." not in migration
    assert "UPDATE observer." not in migration
