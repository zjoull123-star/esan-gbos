from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]
RUNNER = ROOT / "scripts" / "dev" / "test-email-gateway-frappe"


def test_runner_is_unique_current_source_twice_migrated_and_always_torn_down() -> None:
    source = RUNNER.read_text(encoding="utf-8")

    assert "set -euo pipefail" in source
    assert "mktemp -d" in source
    assert '--project-name "${PROJECT_NAME}"' in source
    assert "gbos-email-authority-" in source
    assert "email-authority-" in source
    assert source.count('bench --site "$$SITE_NAME" migrate') == 2
    assert "apps/esan_gbos:/home/frappe/frappe-bench/apps/esan_gbos:ro" in source
    assert "down --volumes --remove-orphans" in source
    assert "trap cleanup EXIT INT TERM" in source
    assert "internal: true" in source


def test_runner_executes_only_named_native_authority_modules_without_provider_services() -> None:
    source = RUNNER.read_text(encoding="utf-8")

    assert (
        "run-tests --module esan_gbos.gbos.doctype.gbos_party_profile.test_gbos_party_profile"
        in source
    )
    assert (
        "run-tests --module esan_gbos.gbos.doctype.gbos_external_identity."
        "test_gbos_external_identity" in source
    )
    assert "run-tests --app" not in source
    assert "observer" not in source.casefold()
    assert "provider" not in source.casefold()
    assert "mailbox" not in source.casefold()
    assert "frontend" not in source
    assert "scheduler" not in source
    assert "queue-short" not in source
