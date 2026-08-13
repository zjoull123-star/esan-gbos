from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parents[2]
ADR = ROOT / "docs" / "adr" / "ADR-0004-ai-drafts-and-human-commands.md"
PERMISSIONS = ROOT / "docs" / "permission-matrix.md"
LOCAL_MANIFEST = ROOT / "infra" / "local" / "local-pilot-manifest.json"
PROD_TOPOLOGY = ROOT / "infra" / "prod" / "single-tenant-v1.json"
PROD_TEMPLATE = ROOT / "infra" / "prod" / "site-per-tenant-v1.template.json"
COMPOSE = ROOT / "infra" / "local" / "compose.yml"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _json(path: Path) -> dict[str, object]:
    value = json.loads(_read(path))
    assert isinstance(value, dict)
    return value


def test_adr_freezes_delegated_current_owner_approval_without_direct_send() -> None:
    adr = _read(ADR)

    for statement in (
        "email_send_owner_v1",
        "delegated current-owner approval",
        "Sales User has no general approval authority",
        "PWA and BFF cannot write Send Outbox",
        "AI and background services cannot approve or send",
    ):
        assert statement in adr


def test_adr_defines_two_durable_transactions_and_no_distributed_acid_claim() -> None:
    adr = _read(ADR)

    assert "Frappe durable transaction" in adr
    assert "decision + ApprovedCommand + command publication" in adr
    assert "Gateway durable transaction" in adr
    assert "command receipt + Send Outbox" in adr
    assert "no cross-database ACID" in adr
    assert "idempotent consumer" in adr


def test_adr_requires_specialized_guard_live_recheck_and_safe_attempt_lifecycle() -> None:
    adr = _read(ADR)

    for statement in (
        "specialized Action Guard",
        "live authority recheck",
        "append-only attempt ledger",
        "provider_accepted",
        "provider_rejected",
        "reconciliation_required",
        "no blind retry",
        "emergency stop",
    ):
        assert statement in adr


def test_permission_matrix_keeps_human_and_service_roles_least_privileged() -> None:
    matrix = _read(PERMISSIONS)

    assert "Sales User |" in matrix
    assert "delegated current-owner approval" in matrix
    assert "no general approval authority" in matrix
    assert "Email Command Publication Consumer" in matrix
    assert "email-command-executor" in matrix
    assert "email-send-worker" in matrix
    assert "no System Manager or broad DocPerm" in matrix
    assert "only command-executor may insert Send Outbox" in matrix
    assert "send worker cannot approve or create Send Outbox" in matrix


def test_checked_in_manifests_keep_every_external_send_switch_closed() -> None:
    local = _json(LOCAL_MANIFEST)
    prod = _json(PROD_TOPOLOGY)
    template = _json(PROD_TEMPLATE)
    compose = _read(COMPOSE)

    local_capabilities = local["capabilities"]
    email_gateway = local["email_gateway"]
    assert isinstance(local_capabilities, dict)
    assert isinstance(email_gateway, dict)
    assert local_capabilities["external_send"] is False
    assert email_gateway["external_send"] is False

    for topology in (prod, template):
        capabilities = topology["capabilities"]
        assert isinstance(capabilities, dict)
        assert capabilities["external_sends_enabled"] is False

    assert 'GBOS_EXTERNAL_SEND_ENABLED: "true"' not in compose
    assert 'GBOS_EXTERNAL_SEND_ENABLED: "false"' in compose
