from __future__ import annotations

from pathlib import Path

import pytest
from esan_gbos.domain.review_dto import (
    REVIEW_SUBJECT_DOCTYPES,
    ReviewDTOValidationError,
    canonical_payload_hash,
    validate_decision_payload,
    validate_evidence_references,
    validate_subject_pin,
)

EMAIL_SEND_POLICY = "email_send_owner_v1"


def test_external_identity_is_a_pinned_review_subject() -> None:
    assert "GBOS External Identity" in REVIEW_SUBJECT_DOCTYPES


def test_email_send_approval_is_a_pinned_but_specialized_review_subject() -> None:
    assert "GBOS Email Send Approval" in REVIEW_SUBJECT_DOCTYPES


def _decision_payload() -> dict[str, object]:
    return {
        "name": "REV-01",
        "decision": "Approved",
        "decision_note": "Evidence supports the internal transition.",
        "expected_revision": 3,
        "expected_case_payload_hash": "a" * 64,
        "expected_subject_revision": 7,
        "idempotency_key": "review-command-01",
        "subject_payload_sha256": "b" * 64,
        "evidence_refs": ["OBS-01"],
        "policy_version": "review-policy/v1",
    }


def test_decision_payload_is_strict_and_normalizes_reason() -> None:
    payload = _decision_payload()
    payload["decision_note"] = "  Evidence supports approval.  "

    value = validate_decision_payload(payload)

    assert value["decision_note"] == "Evidence supports approval."
    assert value["decision"] == "Approved"


def test_generic_decision_dto_rejects_the_email_send_owner_policy() -> None:
    payload = _decision_payload()
    payload["policy_version"] = EMAIL_SEND_POLICY

    with pytest.raises(ReviewDTOValidationError, match="specialized email send"):
        validate_decision_payload(payload)


def test_generic_review_api_excludes_and_rejects_email_send_owner_cases() -> None:
    source = (
        Path(__file__).parents[2]
        / "apps"
        / "esan_gbos"
        / "esan_gbos"
        / "api"
        / "v2"
        / "review_case.py"
    ).read_text(encoding="utf-8")

    assert '"policy_version": ["!=", "email_send_owner_v1"]' in source
    assert source.count('case.policy_version == "email_send_owner_v1"') == 1


@pytest.mark.parametrize("decision", ("Pending", "Superseded", "approved", ""))
def test_decision_payload_allows_only_approved_or_rejected(decision: str) -> None:
    payload = _decision_payload()
    payload["decision"] = decision

    with pytest.raises(ReviewDTOValidationError, match="decision"):
        validate_decision_payload(payload)


@pytest.mark.parametrize("reason", ("", "   ", None, 12))
def test_decision_payload_requires_a_nonempty_reason(reason: object) -> None:
    payload = _decision_payload()
    payload["decision_note"] = reason

    with pytest.raises(ReviewDTOValidationError, match="decision_note"):
        validate_decision_payload(payload)


def test_decision_payload_rejects_arbitrary_fields() -> None:
    payload = _decision_payload()
    payload["subject_doctype"] = "Sales Order"

    with pytest.raises(ReviewDTOValidationError, match="unexpected fields"):
        validate_decision_payload(payload)


@pytest.mark.parametrize(
    "field,value",
    (
        ("expected_revision", 0),
        ("expected_subject_revision", True),
        ("expected_case_payload_hash", "not-a-sha256"),
        ("subject_payload_sha256", "A" * 64),
        ("idempotency_key", "short"),
        ("policy_version", " "),
    ),
)
def test_decision_payload_validates_pins_and_idempotency_key(
    field: str,
    value: object,
) -> None:
    payload = _decision_payload()
    payload[field] = value

    with pytest.raises(ReviewDTOValidationError, match=field):
        validate_decision_payload(payload)


def test_canonical_payload_hash_is_key_order_independent() -> None:
    assert canonical_payload_hash({"name": "WRK-01", "revision": 4}) == canonical_payload_hash(
        {"revision": 4, "name": "WRK-01"}
    )


def test_subject_pin_requires_allowlisted_doctype_and_exact_snapshot_hash() -> None:
    snapshot = {
        "doctype": "GBOS Work Item",
        "name": "WRK-01",
        "revision": 4,
        "title": "Call the customer",
        "business_status": "Open",
    }
    digest = canonical_payload_hash(snapshot)

    value = validate_subject_pin(
        subject_doctype="GBOS Work Item",
        subject_name="WRK-01",
        subject_revision=4,
        subject_payload_hash=digest,
        subject_snapshot=snapshot,
    )

    assert value == snapshot


def test_subject_pin_rejects_transaction_doctype() -> None:
    snapshot = {"doctype": "Sales Order", "name": "SO-01", "revision": 1}

    with pytest.raises(ReviewDTOValidationError, match="subject_doctype"):
        validate_subject_pin(
            subject_doctype="Sales Order",
            subject_name="SO-01",
            subject_revision=1,
            subject_payload_hash=canonical_payload_hash(snapshot),
            subject_snapshot=snapshot,
        )


@pytest.mark.parametrize(
    "override",
    (
        {"subject_name": "WRK-02"},
        {"subject_revision": 5},
        {"subject_payload_hash": "c" * 64},
    ),
)
def test_subject_pin_rejects_mismatched_identity_revision_or_hash(
    override: dict[str, object],
) -> None:
    snapshot = {"doctype": "GBOS Work Item", "name": "WRK-01", "revision": 4}
    arguments: dict[str, object] = {
        "subject_doctype": "GBOS Work Item",
        "subject_name": "WRK-01",
        "subject_revision": 4,
        "subject_payload_hash": canonical_payload_hash(snapshot),
        "subject_snapshot": snapshot,
    }
    arguments.update(override)

    with pytest.raises(ReviewDTOValidationError, match="subject"):
        validate_subject_pin(**arguments)  # type: ignore[arg-type]


def test_evidence_references_require_a_policy_bounded_nonempty_list() -> None:
    value = validate_evidence_references(["OBS-01"])

    assert value == ["OBS-01"]


@pytest.mark.parametrize(
    "evidence",
    (
        [],
        [""],
        ["OBS-01", "OBS-01"],
        [{"evidence_type": "Observation", "reference": "OBS-01"}],
    ),
)
def test_evidence_references_reject_empty_or_open_ended_values(evidence: list[object]) -> None:
    with pytest.raises(ReviewDTOValidationError, match="evidence"):
        validate_evidence_references(evidence)


def test_evidence_references_are_bounded() -> None:
    with pytest.raises(ReviewDTOValidationError, match="evidence_refs"):
        validate_evidence_references([f"OBS-{index:03d}" for index in range(101)])
