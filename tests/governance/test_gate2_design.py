from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
GOVERNANCE_DIR = REPO_ROOT / "docs" / "governance"
DATA_FLOW = GOVERNANCE_DIR / "gate2-data-flow-and-capacity.md"
TEST_STRATEGY = GOVERNANCE_DIR / "gate2-test-strategy.md"
MCP_AUTHORIZATION = GOVERNANCE_DIR / "mcp-authorization.md"
THREAT_MODEL = GOVERNANCE_DIR / "threat-model.md"
GOVERNANCE_INDEX = GOVERNANCE_DIR / "README.md"
EXTERNAL_DEPENDENCIES = REPO_ROOT / "docs" / "external-deps.md"
PERMISSION_MATRIX = REPO_ROOT / "docs" / "permission-matrix.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_gate2_data_flow_names_four_truths_and_service_boundaries() -> None:
    design = _read(DATA_FLOW)

    for truth in (
        "Transaction Truth",
        "Workflow Truth",
        "Context Truth",
        "Analytical Truth",
    ):
        assert truth in design

    for boundary in (
        "Frappe/MariaDB",
        "Observer",
        "Context/Decision Service",
        "Agent Runtime",
        "Metrics API",
        "ESAN MCP Gateway",
        "Kingdee",
    ):
        assert boundary in design

    assert "不得反向覆盖" in design
    assert "不直连业务数据库" in design
    assert "不暴露任意 SQL" in design


def test_gate2_capacity_assumptions_are_bounded_and_fail_closed() -> None:
    design = _read(DATA_FLOW)

    for term in (
        "容量假设",
        "假设而非实测",
        "site_id",
        "p95",
        "rate limit",
        "backpressure",
        "dead-letter",
        "fail closed",
        "unavailable",
        "verification_required",
    ):
        assert term.lower() in design.lower()

    assert "Gate 3" in design
    assert "Gate 4" in design
    assert "Gate 5" in design


def test_gate2_strategy_assigns_gate3_through_gate5_test_ownership() -> None:
    strategy = _read(TEST_STRATEGY)

    required_gate_controls = {
        "Gate 3": ("Observer", "重放", "租户隔离", "提示注入"),
        "Gate 4": ("Agent Runtime", "lease", "预算", "Action Guard"),
        "Gate 5": ("Metrics", "Kingdee", "只读", "对账"),
    }
    for gate, controls in required_gate_controls.items():
        assert gate in strategy
        for control in controls:
            assert control.lower() in strategy.lower()

    for term in ("测试 owner", "证据 owner", "退出条件", "负向测试"):
        assert term.lower() in strategy.lower()


def test_gate2_mcp_scopes_are_activated_only_by_their_allowed_gate() -> None:
    authorization = _read(MCP_AUTHORIZATION)

    expected = {
        "`gbos-read`": "Gate 4",
        "`gbos-propose`": "Gate 4",
        "`metrics-read`": "Gate 5",
        "`kingdee-read`": "Gate 5",
    }
    for scope, earliest_gate in expected.items():
        rows = [
            line for line in authorization.splitlines() if line.startswith("|") and scope in line
        ]
        assert rows
        assert all(earliest_gate in row for row in rows)

    assert "Gate 2 capability state" in authorization
    assert "disabled/mock-only" in authorization
    assert "credentials loaded" in authorization
    assert "0" in authorization


def test_gate2_external_capabilities_are_explicitly_not_started_or_not_applicable() -> None:
    dependencies = _read(EXTERNAL_DEPENDENCIES)

    assert "Gate 2 capability ledger" in dependencies
    for capability in (
        "real connector",
        "real model",
        "production channel",
        "Kingdee live access",
        "cloud runtime",
        "production deployment",
    ):
        rows = [
            line
            for line in dependencies.splitlines()
            if line.startswith("|") and capability in line
        ]
        assert len(rows) == 1
        assert "`not_started`" in rows[0] or "`not_applicable`" in rows[0]


def test_gate2_governance_index_and_risk_record_rules_are_linked() -> None:
    index = _read(GOVERNANCE_INDEX)
    threat_model = _read(THREAT_MODEL)

    assert "gate2-data-flow-and-capacity.md" in index
    assert "gate2-test-strategy.md" in index
    assert "../evidence/gate2/gate2-summary.md" in index

    for field in (
        "risk_id",
        "severity",
        "owner",
        "status",
        "test_refs",
        "evidence_refs",
        "human_review",
    ):
        assert f"`{field}`" in threat_model
    assert "字符串" in threat_model
    assert "关闭证据" in threat_model


def test_gate2_permission_matrix_freezes_service_identities_and_future_activation() -> None:
    matrix = _read(PERMISSION_MATRIX)

    assert "服务身份" in matrix
    for identity in (
        "`observer-ingest`",
        "`context-service`",
        "`agent-runtime`",
        "`gbos-bff-service`",
        "`metrics-service`",
        "`kingdee-adapter`",
    ):
        assert identity in matrix

    for control in (
        "每请求",
        "site_id",
        "purpose",
        "scope",
        "audience",
        "不直连 Frappe/MariaDB",
        "无 Kingdee 写工具",
        "当前分支已实现",
        "正式 local pilot 仍为 No-Go",
    ):
        assert control.lower() in matrix.lower()

    assert "Gate 1 已通过" in matrix
