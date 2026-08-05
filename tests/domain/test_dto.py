from __future__ import annotations

import pytest
from esan_gbos.domain.dto import DTOValidationError, validate_payload


def test_command_dto_accepts_only_declared_fields() -> None:
    value = validate_payload(
        "work_item.transition",
        {
            "name": "WRK-01",
            "to_status": "Done",
            "expected_revision": 3,
            "idempotency_key": "request-key",
        },
    )

    assert value["expected_revision"] == 3


def test_command_dto_rejects_arbitrary_fields() -> None:
    with pytest.raises(DTOValidationError, match="unexpected fields: doctype"):
        validate_payload(
            "work_item.transition",
            {
                "name": "WRK-01",
                "to_status": "Done",
                "expected_revision": 3,
                "idempotency_key": "request-key",
                "doctype": "Sales Order",
            },
        )


def test_command_dto_requires_revision_and_idempotency_key() -> None:
    with pytest.raises(DTOValidationError, match="missing required fields"):
        validate_payload("sample.record_feedback", {"project": "SAM-01"})


def test_create_project_accepts_deal_only_as_a_scoped_relationship_check() -> None:
    value = validate_payload(
        "sample.create_project",
        {
            "team": "TEM-01",
            "title": "Bottle sample",
            "product_brief": "PRB-01",
            "deal": "CRM-DEAL-01",
            "expected_revision": 0,
            "idempotency_key": "create-1",
        },
    )

    assert value["deal"] == "CRM-DEAL-01"


@pytest.mark.parametrize("key", ("short", "x" * 257))
def test_idempotency_key_must_match_the_openapi_bounds(key: str) -> None:
    with pytest.raises(DTOValidationError, match="idempotency_key"):
        validate_payload(
            "work_item.transition",
            {
                "name": "WRK-01",
                "to_status": "Done",
                "expected_revision": 3,
                "idempotency_key": key,
            },
        )


@pytest.mark.parametrize("revision", (-1, True))
def test_revision_must_be_a_non_negative_integer(revision: object) -> None:
    with pytest.raises(DTOValidationError, match="expected_revision"):
        validate_payload(
            "work_item.transition",
            {
                "name": "WRK-01",
                "to_status": "Done",
                "expected_revision": revision,
                "idempotency_key": "request-key",
            },
        )


def test_human_sample_command_cannot_claim_ai_or_integration_origin() -> None:
    with pytest.raises(DTOValidationError, match="origin"):
        validate_payload(
            "sample.create_project",
            {
                "team": "TEM-01",
                "title": "Bottle sample",
                "origin": "AI",
                "expected_revision": 0,
                "idempotency_key": "create-1",
            },
        )
