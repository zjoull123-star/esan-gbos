from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

CONTRACTS_DIR = Path(__file__).parents[2] / "contracts"
SCHEMA_FILES = (
    "canonical-observation-event.schema.json",
    "evidence-ref.schema.json",
    "extracted-fact.schema.json",
    "draft-mutation.schema.json",
    "approved-command.schema.json",
    "connector-checkpoint.schema.json",
)


def load_validator(filename: str) -> Draft202012Validator:
    schema = json.loads((CONTRACTS_DIR / filename).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


@pytest.mark.parametrize("filename", SCHEMA_FILES)
def test_contract_is_valid_json_schema_2020_12(filename: str) -> None:
    validator = load_validator(filename)
    assert validator.META_SCHEMA["$id"].endswith("/draft/2020-12/schema")


VALID_CONTRACTS: tuple[tuple[str, dict[str, Any]], ...] = (
    (
        "canonical-observation-event.schema.json",
        {
            "schema_version": "1.0",
            "event_id": "01K20B8BV5C6P4YFAT8YQ3D4S5",
            "site_id": "gbos.localhost",
            "connector": "email",
            "channel": "email",
            "provider_event_id": "mail-SYNTH-001",
            "occurred_at": "2026-08-06T01:00:00Z",
            "ingested_at": "2026-08-06T01:00:03Z",
            "original_language": "en",
            "participants": [
                {
                    "role": "external",
                    "identity_ref": "identity-SYNTH-001",
                    "display_name": "Synthetic Contact",
                }
            ],
            "evidence_refs": ["evidence-SYNTH-001"],
            "raw_sha256": "a" * 64,
            "consent_basis": "contract",
            "data_classification": "Restricted",
            "retention_class": "business-communication",
            "correlation_id": "corr-SYNTH-001",
        },
    ),
    (
        "evidence-ref.schema.json",
        {
            "schema_version": "1.0",
            "evidence_id": "evidence-SYNTH-001",
            "observation_event_id": "01K20B8BV5C6P4YFAT8YQ3D4S5",
            "raw_sha256": "b" * 64,
            "object_ref": "cos://synthetic/evidence-SYNTH-001",
            "media_type": "text/plain",
            "locator": {"message_start": 0, "message_end": 42},
            "created_at": "2026-08-06T01:00:03Z",
        },
    ),
    (
        "extracted-fact.schema.json",
        {
            "schema_version": "1.0",
            "fact_id": "fact-SYNTH-001",
            "subject_ref": "party-SYNTH-001",
            "predicate": "requested_quantity",
            "value": {"type": "number", "number": 1000, "unit": "pcs"},
            "confidence": 0.96,
            "evidence_refs": ["evidence-SYNTH-001"],
            "model": {"provider": "synthetic", "model": "fixture", "prompt_version": "v1"},
            "status": "proposed",
            "extracted_at": "2026-08-06T01:00:04Z",
        },
    ),
    (
        "draft-mutation.schema.json",
        {
            "schema_version": "1.0",
            "mutation_id": "mutation-SYNTH-001",
            "site_id": "gbos.localhost",
            "target_doctype": "GBOS Product Brief",
            "operation": "update",
            "target_name": "GBPB-SYNTH-001",
            "target_review_status": "AI Draft",
            "expected_revision": 2,
            "patch": [{"op": "replace", "path": "/requested_quantity", "value": 1000}],
            "idempotency_key": "draft-SYNTH-001",
            "evidence_refs": ["evidence-SYNTH-001"],
            "policy_version": "ai-draft-v1",
            "confidence": 0.96,
            "created_at": "2026-08-06T01:00:05Z",
        },
    ),
    (
        "approved-command.schema.json",
        {
            "schema_version": "1.0",
            "command_id": "command-SYNTH-001",
            "site_id": "gbos.localhost",
            "command_type": "work_item.transition",
            "actor": "reviewer@example.invalid",
            "review_case": "GBRC-SYNTH-001",
            "target_doctype": "GBOS Work Item",
            "target_name": "GBWI-SYNTH-001",
            "expected_revision": 3,
            "payload_sha256": "c" * 64,
            "idempotency_key": "command-key-SYNTH-001",
            "before_status": "Open",
            "after_status": "In Progress",
            "issued_at": "2026-08-06T01:00:06Z",
        },
    ),
    (
        "connector-checkpoint.schema.json",
        {
            "schema_version": "1.0",
            "checkpoint_id": "checkpoint-SYNTH-001",
            "site_id": "gbos.localhost",
            "connector": "email",
            "cursor": "cursor-SYNTH-001",
            "replay_window_seconds": 3600,
            "lease_owner": "observer-SYNTH-001",
            "lease_expires_at": "2026-08-06T01:10:00Z",
            "last_success_at": "2026-08-06T01:00:00Z",
            "status": "healthy",
            "updated_at": "2026-08-06T01:00:06Z",
        },
    ),
)


@pytest.mark.parametrize(("filename", "instance"), VALID_CONTRACTS)
def test_contract_accepts_canonical_example(filename: str, instance: dict[str, Any]) -> None:
    load_validator(filename).validate(instance)


def test_observation_event_requires_provider_id_or_raw_hash() -> None:
    instance = dict(VALID_CONTRACTS[0][1])
    instance.pop("provider_event_id")
    instance.pop("raw_sha256")

    with pytest.raises(ValidationError):
        load_validator("canonical-observation-event.schema.json").validate(instance)


def test_observation_event_rejects_invalid_timestamp() -> None:
    instance = dict(VALID_CONTRACTS[0][1])
    instance["occurred_at"] = "not-a-timestamp"

    with pytest.raises(ValidationError):
        load_validator("canonical-observation-event.schema.json").validate(instance)


def test_extracted_fact_rejects_confidence_above_one() -> None:
    instance = dict(VALID_CONTRACTS[2][1])
    instance["confidence"] = 1.01

    with pytest.raises(ValidationError):
        load_validator("extracted-fact.schema.json").validate(instance)


@pytest.mark.parametrize(
    "path",
    (
        "/deal_stage",
        "/business_status",
        "/review_status",
        "/won_lost",
        "/price",
        "/unit_price",
        "/formal_price",
        "/discount",
        "/payment_terms",
        "/delivery_date",
        "/selected_supplier",
        "/outbound_message",
        "/sales_order",
        "/purchase_order",
    ),
)
def test_draft_mutation_rejects_formal_business_paths(path: str) -> None:
    instance = dict(VALID_CONTRACTS[3][1])
    instance["patch"] = [{"op": "replace", "path": path, "value": "forbidden"}]

    with pytest.raises(ValidationError):
        load_validator("draft-mutation.schema.json").validate(instance)


def test_draft_mutation_cannot_claim_human_approval() -> None:
    instance = dict(VALID_CONTRACTS[3][1])
    instance["target_review_status"] = "Approved"

    with pytest.raises(ValidationError):
        load_validator("draft-mutation.schema.json").validate(instance)


def test_approved_command_requires_human_actor_and_review_case() -> None:
    instance = dict(VALID_CONTRACTS[4][1])
    instance.pop("actor")
    instance.pop("review_case")

    with pytest.raises(ValidationError):
        load_validator("approved-command.schema.json").validate(instance)


def test_approved_command_cannot_address_kingdee_write() -> None:
    instance = dict(VALID_CONTRACTS[4][1])
    instance["command_type"] = "kingdee.save_bill"

    with pytest.raises(ValidationError):
        load_validator("approved-command.schema.json").validate(instance)


def test_connector_checkpoint_rejects_negative_replay_window() -> None:
    instance = dict(VALID_CONTRACTS[5][1])
    instance["replay_window_seconds"] = -1

    with pytest.raises(ValidationError):
        load_validator("connector-checkpoint.schema.json").validate(instance)
