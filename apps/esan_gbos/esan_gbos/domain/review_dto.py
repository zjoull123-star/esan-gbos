from __future__ import annotations

import hashlib
import json
import re
from typing import Any

REVIEW_SUBJECT_DOCTYPES = frozenset(
    {
        "GBOS Demand Signal",
        "GBOS Party Profile",
        "GBOS Product Brief",
        "GBOS Sample Feedback",
        "GBOS Sample Iteration",
        "GBOS Sample Project",
        "GBOS Sample Shipment",
        "GBOS Sourcing Event",
        "GBOS Work Item",
    }
)

DECISIONS = frozenset({"Approved", "Rejected"})

_HASH = re.compile(r"^[0-9a-f]{64}$")
_DECISION_REQUIRED = frozenset(
    {
        "name",
        "decision",
        "decision_note",
        "expected_revision",
        "expected_subject_revision",
        "idempotency_key",
        "subject_payload_sha256",
        "evidence_refs",
        "policy_version",
    }
)
_DECISION_OPTIONAL = frozenset({"expected_case_payload_hash"})


class ReviewDTOValidationError(ValueError):
    """Raised when a Gate 4 review DTO violates its closed shape."""


def canonical_payload_hash(payload: dict[str, Any]) -> str:
    """Return the lowercase SHA-256 of a canonical JSON object."""
    try:
        serialized = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise ReviewDTOValidationError("payload must be canonical JSON") from error
    return hashlib.sha256(serialized.encode()).hexdigest()


def _require_text(value: object, field: str, *, maximum: int = 140) -> str:
    if not isinstance(value, str):
        raise ReviewDTOValidationError(f"{field} must be a nonempty string")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ReviewDTOValidationError(
            f"{field} must contain 1 to {maximum} non-whitespace-bound characters"
        )
    return normalized


def _require_positive_revision(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ReviewDTOValidationError(f"{field} must be a positive integer")
    return value


def _require_hash(value: object, field: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ReviewDTOValidationError(f"{field} must be a lowercase SHA-256 hex digest")
    return value


def validate_evidence_references(value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ReviewDTOValidationError("evidence_refs must be a nonempty list")
    if len(value) > 100:
        raise ReviewDTOValidationError("evidence_refs must contain at most 100 references")
    normalized: list[str] = []
    for index, item in enumerate(value):
        normalized.append(
            _require_text(
                item,
                f"evidence_refs[{index}]",
                maximum=500,
            )
        )
    if len(set(normalized)) != len(normalized):
        raise ReviewDTOValidationError("evidence_refs must not contain duplicates")
    return normalized


def validate_decision_payload(payload: dict[str, Any]) -> dict[str, Any]:
    supplied = frozenset(payload)
    missing = _DECISION_REQUIRED - supplied
    unexpected = supplied - _DECISION_REQUIRED - _DECISION_OPTIONAL
    if missing:
        raise ReviewDTOValidationError(f"missing required fields: {', '.join(sorted(missing))}")
    if unexpected:
        raise ReviewDTOValidationError(f"unexpected fields: {', '.join(sorted(unexpected))}")

    decision = payload["decision"]
    if decision not in DECISIONS:
        raise ReviewDTOValidationError("decision must be Approved or Rejected")

    normalized = dict(payload)
    normalized["name"] = _require_text(payload["name"], "name")
    normalized["decision_note"] = _require_text(
        payload["decision_note"],
        "decision_note",
        maximum=2000,
    )
    normalized["expected_revision"] = _require_positive_revision(
        payload["expected_revision"],
        "expected_revision",
    )
    normalized["expected_subject_revision"] = _require_positive_revision(
        payload["expected_subject_revision"],
        "expected_subject_revision",
    )
    normalized["idempotency_key"] = _require_text(
        payload["idempotency_key"],
        "idempotency_key",
        maximum=256,
    )
    if len(normalized["idempotency_key"]) < 8:
        raise ReviewDTOValidationError("idempotency_key must contain 8 to 256 characters")
    normalized["subject_payload_sha256"] = _require_hash(
        payload["subject_payload_sha256"],
        "subject_payload_sha256",
    )
    if "expected_case_payload_hash" in payload:
        normalized["expected_case_payload_hash"] = _require_hash(
            payload["expected_case_payload_hash"],
            "expected_case_payload_hash",
        )
    normalized["evidence_refs"] = validate_evidence_references(payload["evidence_refs"])
    normalized["policy_version"] = _require_text(
        payload["policy_version"],
        "policy_version",
    )
    return normalized


def validate_subject_pin(
    *,
    subject_doctype: str,
    subject_name: str,
    subject_revision: int,
    subject_payload_hash: str,
    subject_snapshot: dict[str, Any],
) -> dict[str, Any]:
    if subject_doctype not in REVIEW_SUBJECT_DOCTYPES:
        raise ReviewDTOValidationError("subject_doctype is not reviewable")
    name = _require_text(subject_name, "subject_name")
    revision = _require_positive_revision(subject_revision, "subject_revision")
    digest = _require_hash(subject_payload_hash, "subject_payload_hash")
    if not isinstance(subject_snapshot, dict):
        raise ReviewDTOValidationError("subject_snapshot must be an object")
    if subject_snapshot.get("doctype") != subject_doctype:
        raise ReviewDTOValidationError("subject snapshot doctype does not match subject_doctype")
    if subject_snapshot.get("name") != name:
        raise ReviewDTOValidationError("subject snapshot name does not match subject_name")
    if subject_snapshot.get("revision") != revision:
        raise ReviewDTOValidationError("subject snapshot revision does not match subject_revision")
    if canonical_payload_hash(subject_snapshot) != digest:
        raise ReviewDTOValidationError("subject snapshot hash does not match subject_payload_hash")
    return dict(subject_snapshot)
