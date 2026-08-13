from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

ROOT = Path(__file__).parents[2]
CONTRACTS = ROOT / "contracts"
EMAIL_GATEWAY = CONTRACTS / "email_gateway"
SCHEMA_PATH = EMAIL_GATEWAY / "email-send-approved-command-v2.0.schema.json"
EXAMPLE_PATH = EMAIL_GATEWAY / "examples" / "email-send-approved-command-v2.json"
V1_SCHEMA_PATH = CONTRACTS / "approved-command.schema.json"
EVOLUTION_MATRIX = CONTRACTS / "gate2" / "contract-evolution-matrix.json"


def _load(path: Path) -> dict[str, Any]:
    assert path.exists(), f"missing required contract artifact: {path.relative_to(ROOT)}"
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _schema() -> dict[str, Any]:
    return _load(SCHEMA_PATH)


def _example() -> dict[str, Any]:
    return _load(EXAMPLE_PATH)


def _validator() -> Draft202012Validator:
    schema = _schema()
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _walk_schema(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_schema(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_schema(value)


def test_v2_contract_and_example_are_valid_closed_json_schema() -> None:
    schema = _schema()
    example = _example()

    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"].endswith("/email-gateway/v2.0/email-send-approved-command.schema.json")
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    _validator().validate(example)

    for node in _walk_schema(schema):
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, node
        if node.get("type") == "string":
            assert node.get("maxLength", 0) > 0, node


def test_v2_command_freezes_every_approved_send_binding() -> None:
    schema = _schema()
    properties = schema["properties"]

    assert set(properties) == {
        "schema_version",
        "command_id",
        "command_type",
        "site_id",
        "processing_purpose",
        "team_ref",
        "actor_user_ref",
        "delegated_approver_user_ref",
        "review_case_ref",
        "review_case_revision",
        "review_policy_version",
        "approval_expires_at",
        "mailbox_ref",
        "mailbox_config_revision",
        "inbox_item_ref",
        "inbox_item_revision",
        "conversation_ref",
        "conversation_revision",
        "reply_draft_ref",
        "reply_draft_revision",
        "reply_draft_digest",
        "participants",
        "party_ref",
        "party_revision",
        "team_revision",
        "owner_user_ref",
        "owner_eligibility_revision",
        "final_mime_evidence_ref",
        "final_mime_digest",
        "evidence_refs",
        "request_id",
        "idempotency_key",
        "stable_client_request_id",
        "payload_sha256",
        "issued_at",
    }
    assert properties["schema_version"]["const"] == "2.0"
    assert properties["command_type"]["const"] == "email.send.approved"
    assert properties["review_policy_version"]["const"] == "email_send_owner_v1"


def test_participant_envelope_is_opaque_role_tagged_and_mapping_pinned() -> None:
    schema = _schema()
    participants = schema["properties"]["participants"]
    item = participants["items"]

    assert participants["uniqueItems"] is True
    assert participants["minItems"] >= 2
    assert item["additionalProperties"] is False
    assert set(item["properties"]) == {
        "address_role",
        "opaque_address_ref",
        "identity_mapping_ref",
        "identity_mapping_revision",
    }
    assert item["properties"]["address_role"]["enum"] == ["sender", "to", "cc", "bcc"]
    assert item["properties"]["opaque_address_ref"]["pattern"].startswith("^extid:v1:email:")
    assert participants["allOf"]


@pytest.mark.parametrize(
    "revision_path",
    (
        ("review_case_revision",),
        ("mailbox_config_revision",),
        ("inbox_item_revision",),
        ("conversation_revision",),
        ("reply_draft_revision",),
        ("party_revision",),
        ("team_revision",),
        ("participants", 1, "identity_mapping_revision"),
    ),
)
def test_command_rejects_zero_or_stale_revision_placeholders(
    revision_path: tuple[str | int, ...],
) -> None:
    invalid = copy.deepcopy(_example())
    target: Any = invalid
    for key in revision_path[:-1]:
        target = target[key]
    target[revision_path[-1]] = 0

    with pytest.raises(ValidationError):
        _validator().validate(invalid)


def test_command_rejects_extra_fields_raw_addresses_and_unmapped_recipients() -> None:
    validator = _validator()
    valid = _example()

    extra = {**valid, "recipient_email": "customer@example.invalid"}
    with pytest.raises(ValidationError):
        validator.validate(extra)

    raw_value = copy.deepcopy(valid)
    raw_value["participants"][1]["opaque_address_ref"] = "customer@example.invalid"
    with pytest.raises(ValidationError):
        validator.validate(raw_value)

    unmapped = copy.deepcopy(valid)
    unmapped["participants"][1].pop("identity_mapping_ref")
    with pytest.raises(ValidationError):
        validator.validate(unmapped)


def test_command_rejects_duplicate_participants_and_evidence() -> None:
    validator = _validator()
    valid = _example()

    duplicate_recipient = copy.deepcopy(valid)
    duplicate_recipient["participants"].append(
        copy.deepcopy(duplicate_recipient["participants"][1])
    )
    with pytest.raises(ValidationError):
        validator.validate(duplicate_recipient)

    duplicate_evidence = copy.deepcopy(valid)
    duplicate_evidence["evidence_refs"].append(duplicate_evidence["evidence_refs"][0])
    with pytest.raises(ValidationError):
        validator.validate(duplicate_evidence)


def test_schema_freezes_action_guard_dynamic_rejections() -> None:
    assert _schema()["x-gbos-action-guard"] == {
        "live_recheck_required": True,
        "reject_if": [
            "authenticated_actor_mismatch",
            "delegated_approver_not_current_owner",
            "approval_expired",
            "site_team_or_purpose_mismatch",
            "review_case_or_policy_mismatch",
            "mailbox_inbox_conversation_or_draft_revision_drift",
            "recipient_mapping_revision_or_envelope_drift",
            "party_team_or_owner_authority_drift",
            "final_mime_evidence_or_digest_drift",
            "evidence_missing_or_duplicate",
            "request_id_idempotency_or_payload_hash_drift",
            "emergency_stop_active",
            "external_send_disabled",
        ],
    }


def test_evolution_matrix_adds_purpose_specific_v2_without_changing_v1() -> None:
    matrix = _load(EVOLUTION_MATRIX)
    by_concept = {entry["concept"]: entry for entry in matrix["entries"]}
    assert by_concept["Approved Internal Command"] == {
        "concept": "Approved Internal Command",
        "contract": "ApprovedCommand",
        "strategy": "reuse",
        "rationale": (
            "The frozen v1 command remains the human-authorized internal execution boundary."
        ),
    }
    assert by_concept["Approved Email Send Command"] == {
        "concept": "Approved Email Send Command",
        "contract": "EmailSendApprovedCommand v2.0",
        "strategy": "new",
        "semantically_distinct_from": ["ApprovedCommand v1.0"],
        "adapter_required_for_generic_command_paths": True,
        "rationale": (
            "A purpose-specific closed command freezes delegated owner approval, every email "
            "authority revision, the opaque participant envelope, and final MIME evidence."
        ),
    }

    v1_schema = _load(V1_SCHEMA_PATH)
    assert v1_schema["properties"]["schema_version"]["const"] == "1.0"
    Draft202012Validator.check_schema(v1_schema)
