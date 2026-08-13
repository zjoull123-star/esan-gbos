from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]
RUNNER = ROOT / "scripts" / "dev" / "test-email-gateway-frappe"


def test_runner_is_unique_current_source_twice_migrated_and_always_torn_down() -> None:
    source = RUNNER.read_text(encoding="utf-8")

    assert "set -euo pipefail" in source
    assert "mktemp -d" in source
    assert '--project-name "${PROJECT_NAME}"' in source
    assert source.count('bench --site "$SITE_NAME" migrate') == 2
    assert "gbos-email-send-" in source
    assert "email-send-" in source
    assert "apps/esan_gbos:/home/frappe/frappe-bench/apps/esan_gbos:ro" in source
    assert "down --volumes --remove-orphans" in source
    assert "trap cleanup EXIT INT TERM" in source
    cleanup_start = source.index("cleanup() {")
    cleanup = source[cleanup_start : source.index("\n}\n", cleanup_start)]
    assert "--profile core" in cleanup
    assert "internal: true" in source


def test_runner_executes_only_named_native_authority_modules_without_provider_services() -> None:
    source = RUNNER.read_text(encoding="utf-8")
    native_approval_test = (
        ROOT
        / "apps"
        / "esan_gbos"
        / "esan_gbos"
        / "gbos"
        / "doctype"
        / "gbos_email_send_approval"
        / "test_gbos_email_send_approval.py"
    ).read_text(encoding="utf-8")

    assert (
        "run-tests --module esan_gbos.gbos.doctype.gbos_party_profile.test_gbos_party_profile"
        in source
    )
    assert (
        "run-tests --module esan_gbos.gbos.doctype.gbos_external_identity."
        "test_gbos_external_identity" in source
    )
    assert (
        "run-tests --module esan_gbos.gbos.doctype.gbos_email_send_approval."
        "test_gbos_email_send_approval" in source
    )
    assert (
        "run-tests --module esan_gbos.gbos.doctype.gbos_approved_command."
        "test_gbos_approved_command" in source
    )
    assert (
        "run-tests --module esan_gbos.gbos.doctype.gbos_command_publication."
        "test_gbos_command_publication" in source
    )
    assert source.count("--skip-test-records") == 5
    assert "run-tests --app" not in source
    assert "OBSERVER_POSTGRES_PASSWORD=authority-observer-" in source
    assert "observer-api" not in source.casefold()
    assert "observer-worker" not in source.casefold()
    assert "provider" not in source.casefold()
    assert "mailbox" not in source.casefold()
    assert "frontend" not in source
    assert "scheduler" not in source
    assert "queue-short" not in source
    assert "esan_gbos.api.internal.email_command_publication" in source
    assert "test_native_publication_claim_heartbeat_ack_and_replay" in native_approval_test
    assert "test_native_publication_expired_claim_is_rejected" in native_approval_test


def test_runner_passes_container_shell_variables_without_pid_expansion() -> None:
    source = RUNNER.read_text(encoding="utf-8")

    assert "$$app" not in source
    assert "$$SITE_NAME" not in source
    assert "$$DB_ROOT_PASSWORD" not in source
    assert "$$ADMIN_PASSWORD" not in source
