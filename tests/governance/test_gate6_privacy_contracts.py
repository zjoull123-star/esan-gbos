from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource

ROOT = Path(__file__).parents[2]
GATE6_CONTRACTS = ROOT / "contracts" / "gate6"
GATE6_GOVERNANCE = ROOT / "docs" / "governance" / "gate6"
EXAMPLES = GATE6_GOVERNANCE / "synthetic"

SCHEMAS_BY_EXAMPLE = {
    "retention-policy.json": "privacy-retention-policy.schema.json",
    "deletion-request.json": "privacy-deletion-request.schema.json",
    "data-subject-access-export.json": "privacy-data-subject-request.schema.json",
    "consent-withdrawal.json": "privacy-consent-withdrawal.schema.json",
    "legal-hold.json": "privacy-legal-hold.schema.json",
    "cross-border-approval.json": "privacy-cross-border-approval.schema.json",
}


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _registry() -> Registry[Any]:
    registry: Registry[Any] = Registry()
    for path in GATE6_CONTRACTS.glob("privacy-*.schema.json"):
        schema = _json(path)
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
    return registry


def _validator(schema_name: str) -> Draft202012Validator:
    schema = _json(GATE6_CONTRACTS / schema_name)
    return Draft202012Validator(
        schema,
        registry=_registry(),
        format_checker=FormatChecker(),
    )


def _validate(example_name: str, value: dict[str, Any] | None = None) -> None:
    schema_name = SCHEMAS_BY_EXAMPLE[example_name]
    _validator(schema_name).validate(value or _json(EXAMPLES / example_name))


def test_required_gate6_privacy_assets_exist() -> None:
    expected = {
        *(GATE6_CONTRACTS / name for name in SCHEMAS_BY_EXAMPLE.values()),
        GATE6_CONTRACTS / "privacy-common.schema.json",
        *(EXAMPLES / name for name in SCHEMAS_BY_EXAMPLE),
        GATE6_GOVERNANCE / "privacy-operations.md",
        GATE6_GOVERNANCE / "privacy-checklist.json",
    }

    missing = sorted(str(path.relative_to(ROOT)) for path in expected if not path.is_file())
    assert missing == []


@pytest.mark.parametrize("example_name", SCHEMAS_BY_EXAMPLE)
def test_synthetic_privacy_examples_validate(example_name: str) -> None:
    _validate(example_name)


def test_all_gate6_privacy_schemas_are_valid_draft_2020_12() -> None:
    for path in GATE6_CONTRACTS.glob("privacy-*.schema.json"):
        Draft202012Validator.check_schema(_json(path))


@pytest.mark.parametrize("example_name", SCHEMAS_BY_EXAMPLE)
def test_local_fixtures_preserve_external_input_blockers(example_name: str) -> None:
    example = _json(EXAMPLES / example_name)

    assert example["fixture_mode"] == "synthetic_only"
    assert example["external_readiness"] == {
        "formal_privacy_legal": "blocked_external_input",
        "real_personal_data": "blocked_external_input",
        "singapore_cross_border": "blocked_external_input",
    }


def test_deletion_without_approval_fails_closed() -> None:
    deletion = _json(EXAMPLES / "deletion-request.json")
    deletion["approval_refs"] = []

    with pytest.raises(ValidationError):
        _validate("deletion-request.json", deletion)


def test_deletion_request_can_remain_requested_without_future_approval_or_receipt() -> None:
    deletion = _json(EXAMPLES / "deletion-request.json")
    deletion["state"] = "requested"
    deletion["state_history"] = deletion["state_history"][:1]
    deletion["approval_refs"] = []
    del deletion["authorization"]
    del deletion["legal_hold_check"]
    del deletion["deletion_receipt"]

    _validate("deletion-request.json", deletion)


def test_approved_deletion_does_not_claim_execution_receipt() -> None:
    deletion = _json(EXAMPLES / "deletion-request.json")
    deletion["state"] = "approved"
    deletion["state_history"] = deletion["state_history"][:2]
    del deletion["deletion_receipt"]

    _validate("deletion-request.json", deletion)


def test_retention_request_does_not_require_future_execution_timestamps() -> None:
    retention = _json(EXAMPLES / "retention-policy.json")
    retention["state"] = "requested"
    retention["state_history"] = retention["state_history"][:1]
    retention["approval_refs"] = []
    del retention["retention"]["approved_at"]
    del retention["retention"]["executed_at"]

    _validate("retention-policy.json", retention)


def test_access_request_does_not_require_future_delivery_receipt() -> None:
    request = _json(EXAMPLES / "data-subject-access-export.json")
    request["state"] = "requested"
    request["state_history"] = request["state_history"][:1]
    request["approval_refs"] = []
    del request["delivery"]

    _validate("data-subject-access-export.json", request)


def test_consent_withdrawal_request_does_not_claim_processing_stopped() -> None:
    withdrawal = _json(EXAMPLES / "consent-withdrawal.json")
    withdrawal["state"] = "requested"
    withdrawal["state_history"] = withdrawal["state_history"][:1]
    withdrawal["approval_refs"] = []
    withdrawal["withdrawal"] = {
        "received_at": "2026-08-01T09:00:00Z",
    }

    _validate("consent-withdrawal.json", withdrawal)


def test_legal_hold_request_does_not_claim_hold_execution() -> None:
    hold = _json(EXAMPLES / "legal-hold.json")
    hold["state"] = "requested"
    hold["state_history"] = hold["state_history"][:1]
    hold["approval_refs"] = []
    hold["hold"] = {
        "hold_id": "hold:synthetic-001",
        "custodian_ref": "custodian:synthetic-privacy-review",
        "review_at": "2026-09-01T00:00:00Z",
    }

    _validate("legal-hold.json", hold)


def test_expired_cross_border_approval_cannot_be_executed() -> None:
    approval = _json(EXAMPLES / "cross-border-approval.json")
    approval["state"] = "executed"
    approval["state_history"] = [
        {
            "state": "requested",
            "at": "2026-08-01T09:00:00Z",
            "actor_ref": "requester:privacy-program",
        },
        {
            "state": "approved",
            "at": "2026-08-02T09:00:00Z",
            "actor_ref": "approver:privacy-lead",
        },
        {
            "state": "executed",
            "at": "2026-08-03T09:00:00Z",
            "actor_ref": "executor:platform-privacy-ops",
        },
    ]
    approval["approval_refs"] = ["approval:external-privacy-review-2026-001"]
    approval["cross_border"]["approvers"] = [
        "approver:privacy-lead",
        "approver:security-lead",
    ]
    approval["cross_border"]["expiry_status"] = "expired"

    with pytest.raises(ValidationError):
        _validate("cross-border-approval.json", approval)


def test_executed_cross_border_transfer_rejects_pending_basis_and_real_data_review() -> None:
    approval = _json(EXAMPLES / "cross-border-approval.json")
    approval["fixture_mode"] = "externally_verified"
    approval["state"] = "executed"
    approval["state_history"] = [
        {
            "state": "requested",
            "at": "2026-08-01T09:00:00Z",
            "actor_ref": "requester:privacy-program",
        },
        {
            "state": "approved",
            "at": "2026-08-02T09:00:00Z",
            "actor_ref": "approver:privacy-lead",
        },
        {
            "state": "executed",
            "at": "2026-08-03T09:00:00Z",
            "actor_ref": "executor:platform-privacy-ops",
        },
    ]
    approval["approval_refs"] = ["approval:external-privacy-review-2026-001"]
    approval["external_readiness"]["formal_privacy_legal"] = "verified_external_input"
    approval["external_readiness"]["singapore_cross_border"] = "verified_external_input"
    approval["cross_border"]["legal_mechanism_assessment"] = {
        "mechanism": "contractual_transfer_mechanism",
        "assessment_ref": "assessment:external-privacy-review-2026-001",
        "assessed_at": "2026-08-02T09:00:00Z",
        "conclusion_status": "externally_approved",
    }
    approval["cross_border"]["expiry_status"] = "current"

    with pytest.raises(ValidationError):
        _validate("cross-border-approval.json", approval)


def test_cross_border_approval_missing_required_review_fails_closed() -> None:
    approval = _json(EXAMPLES / "cross-border-approval.json")
    del approval["cross_border"]["legal_mechanism_assessment"]

    with pytest.raises(ValidationError):
        _validate("cross-border-approval.json", approval)


def test_active_legal_hold_blocks_deletion_execution() -> None:
    deletion = _json(EXAMPLES / "deletion-request.json")
    deletion["legal_hold_check"] = {
        "status": "active_hold",
        "checked_at": "2026-08-02T08:00:00Z",
        "hold_refs": ["hold:synthetic-001"],
    }

    with pytest.raises(ValidationError):
        _validate("deletion-request.json", deletion)


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    (
        ("safe_summary", "Contact the person at alice@example.com"),
        ("safe_summary", "Call +65 8123 4567 for the request"),
        ("safe_summary", "Authorization: Bearer abcdefghijklmnop"),
        ("safe_summary", "token=synthetic-secret-value"),
    ),
)
def test_unredacted_sensitive_values_are_rejected(field: str, unsafe_value: str) -> None:
    request = _json(EXAMPLES / "data-subject-access-export.json")
    request[field] = unsafe_value

    with pytest.raises(ValidationError):
        _validate("data-subject-access-export.json", request)


def test_raw_communication_body_is_rejected() -> None:
    request = _json(EXAMPLES / "data-subject-access-export.json")
    request["raw_message_body"] = "raw message content must never be retained"

    with pytest.raises(ValidationError):
        _validate("data-subject-access-export.json", request)


def test_cross_site_request_is_rejected() -> None:
    request = _json(EXAMPLES / "data-subject-access-export.json")
    assert "site_id" not in request["scope"]
    assert "tenant_id" not in request["scope"]
    request["scope"]["site_id"] = "site-synthetic-b"

    with pytest.raises(ValidationError):
        _validate("data-subject-access-export.json", request)


def test_one_person_cross_border_approval_is_rejected() -> None:
    approval = _json(EXAMPLES / "cross-border-approval.json")
    approval["cross_border"]["approvers"] = ["approver:privacy-lead"]

    with pytest.raises(ValidationError):
        _validate("cross-border-approval.json", approval)


@pytest.mark.parametrize(
    "approvers",
    (
        ["approver:privacy-lead", "approver:privacy-counsel"],
        ["approver:security-lead", "approver:security-reviewer"],
    ),
)
def test_cross_border_approval_requires_privacy_and_security_roles(
    approvers: list[str],
) -> None:
    approval = _json(EXAMPLES / "cross-border-approval.json")
    approval["cross_border"]["approvers"] = approvers

    with pytest.raises(ValidationError):
        _validate("cross-border-approval.json", approval)


def test_state_transition_bypass_is_rejected() -> None:
    deletion = _json(EXAMPLES / "deletion-request.json")
    deletion["state_history"] = [
        deletion["state_history"][0],
        deletion["state_history"][2],
    ]

    with pytest.raises(ValidationError):
        _validate("deletion-request.json", deletion)


def test_synthetic_cross_border_fixture_never_claims_legal_approval() -> None:
    approval = _json(EXAMPLES / "cross-border-approval.json")

    assert approval["state"] == "blocked_external_input"
    assert approval["cross_border"]["local_fixture_is_legal_approval"] is False
    assert approval["approval_refs"] == []

    candidate = deepcopy(approval)
    candidate["cross_border"]["local_fixture_is_legal_approval"] = True
    with pytest.raises(ValidationError):
        _validate("cross-border-approval.json", candidate)


def test_privacy_checklist_is_machine_checkable_and_fail_closed() -> None:
    checklist = _json(GATE6_GOVERNANCE / "privacy-checklist.json")

    assert checklist["status"] == "blocked_external_input"
    assert checklist["production_transfer_enabled"] is False
    assert checklist["real_personal_data_used"] is False
    assert all(
        item["status"] in {"verified_local", "blocked_external_input"}
        for item in checklist["items"]
    )
    assert any(item["status"] == "blocked_external_input" for item in checklist["items"])
