from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
ADR_DIR = REPO_ROOT / "docs" / "adr"
PERMISSION_MATRIX = REPO_ROOT / "docs" / "permission-matrix.md"
EXTERNAL_DEPS = REPO_ROOT / "docs" / "external-deps.md"
MCP_AUTHORIZATION = REPO_ROOT / "docs" / "governance" / "mcp-authorization.md"

GBOS_ROLES = (
    "GBOS Admin",
    "Integration Admin",
    "Privacy/Audit",
    "CEO",
    "Sales Manager",
    "Sales User",
    "Purchase Manager",
    "Buyer",
    "Product/R&D",
    "Reviewer",
    "Finance Readonly",
)


def test_all_gate_zero_adrs_exist() -> None:
    assert len(list(ADR_DIR.glob("ADR-000[1-8]-*.md"))) == 8


def test_permission_matrix_names_every_gate_one_role() -> None:
    matrix = PERMISSION_MATRIX.read_text(encoding="utf-8")

    for role in GBOS_ROLES:
        assert f"| {role} |" in matrix


def test_governance_keeps_external_integrations_disabled() -> None:
    dependencies = EXTERNAL_DEPS.read_text(encoding="utf-8")

    assert "production disabled" in dependencies
    assert "real calls disabled" in dependencies
    assert "no write tool" in dependencies


def test_mcp_authorization_exposes_only_the_frozen_minimum_scopes() -> None:
    authorization = MCP_AUTHORIZATION.read_text(encoding="utf-8")

    assert "2026-07-28" in authorization
    assert "`kingdee-read`" in authorization
    assert "`gbos-read`" in authorization
    assert "`gbos-propose`" in authorization
    assert "`kingdee-write`" not in authorization
    assert "token passthrough" in authorization.lower()
    assert "SSRF" in authorization
