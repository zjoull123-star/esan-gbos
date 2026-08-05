from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class DTOValidationError(ValueError):
    """Raised when an endpoint payload violates its fixed DTO."""


@dataclass(frozen=True)
class DTOSchema:
    required: frozenset[str]
    optional: frozenset[str] = frozenset()


_SCHEMAS = {
    "sample.create_project": DTOSchema(
        required=frozenset({"team", "title", "expected_revision", "idempotency_key"}),
        optional=frozenset({"party_profile", "product_brief", "deal", "origin"}),
    ),
    "sample.record_feedback": DTOSchema(
        required=frozenset(
            {
                "project",
                "summary",
                "expected_revision",
                "idempotency_key",
            }
        ),
        optional=frozenset({"rating", "received_on"}),
    ),
    "sourcing.create_from_demand": DTOSchema(
        required=frozenset({"demand", "expected_revision", "idempotency_key"}),
    ),
    "work_item.transition": DTOSchema(
        required=frozenset({"name", "to_status", "expected_revision", "idempotency_key"}),
        optional=frozenset({"reason"}),
    ),
}


def validate_payload(command: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        schema = _SCHEMAS[command]
    except KeyError as error:
        raise DTOValidationError(f"unknown command: {command}") from error

    supplied = frozenset(payload)
    missing = schema.required - supplied
    if missing:
        raise DTOValidationError(f"missing required fields: {', '.join(sorted(missing))}")
    unexpected = supplied - schema.required - schema.optional
    if unexpected:
        raise DTOValidationError(f"unexpected fields: {', '.join(sorted(unexpected))}")
    revision = payload["expected_revision"]
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise DTOValidationError("expected_revision must be a non-negative integer")
    idempotency_key = payload["idempotency_key"]
    if (
        not isinstance(idempotency_key, str)
        or not 8 <= len(idempotency_key) <= 256
        or idempotency_key != idempotency_key.strip()
    ):
        raise DTOValidationError(
            "idempotency_key must contain 8 to 256 non-whitespace-bound characters"
        )
    if command == "sample.create_project" and payload.get("origin", "Manual") != "Manual":
        raise DTOValidationError("origin must be Manual for human commands")
    return dict(payload)
