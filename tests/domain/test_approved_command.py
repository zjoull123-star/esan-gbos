from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from esan_gbos.domain.approved_command import (
    ApprovedCommandValidationError,
    build_email_send_approved_command,
    command_payload_digest,
    validate_email_send_approved_command,
)

from tests.domain.test_email_send_review_policy import snapshot

ROOT = Path(__file__).parents[2]
EXAMPLE = ROOT / "contracts" / "email_gateway" / "examples" / "email-send-approved-command-v2.json"
EMAIL_SEND_API = ROOT / "apps" / "esan_gbos" / "esan_gbos" / "api" / "v5" / "email_send.py"


def _example() -> dict[str, Any]:
    value = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    value["payload_sha256"] = command_payload_digest(value)
    return value


def test_builder_issues_the_exact_v2_closed_command_from_frozen_subject() -> None:
    value = _example()

    command = build_email_send_approved_command(
        snapshot(),
        command_id=value["command_id"],
        actor_user_ref=value["actor_user_ref"],
        review_case_ref=value["review_case_ref"],
        review_case_revision=value["review_case_revision"],
        request_id=value["request_id"],
        idempotency_key=value["idempotency_key"],
        issued_at=datetime(2026, 8, 13, 13, 0, tzinfo=UTC),
    )

    assert command == value
    assert command["delegated_approver_user_ref"] == command["owner_user_ref"]
    assert command["payload_sha256"] == command_payload_digest(command)
    assert set(command) == set(value)


def test_command_validator_rejects_extra_raw_or_execution_fields() -> None:
    value = {**_example(), "provider_payload": {"to": "customer@example.invalid"}}

    with pytest.raises(ApprovedCommandValidationError, match="unexpected fields"):
        validate_email_send_approved_command(value)


@pytest.mark.parametrize(
    ("field", "changed"),
    (
        ("schema_version", "1.0"),
        ("command_type", "external.message.send"),
        ("review_policy_version", "generic_review_v1"),
        ("review_case_revision", 0),
        ("actor_user_ref", "USR-INVALID"),
        ("issued_at", "not-a-time"),
    ),
)
def test_command_validator_rejects_wrong_contract_policy_pins_or_actor(
    field: str,
    changed: object,
) -> None:
    value = _example()
    value[field] = changed
    value["payload_sha256"] = command_payload_digest(value)

    with pytest.raises(ApprovedCommandValidationError, match=field):
        validate_email_send_approved_command(value)


def test_command_payload_hash_detects_any_post_approval_mutation() -> None:
    value = _example()
    drift = copy.deepcopy(value)
    drift["reply_draft_revision"] += 1

    with pytest.raises(ApprovedCommandValidationError, match="payload_sha256"):
        validate_email_send_approved_command(drift)


def test_command_requires_actor_delegated_approver_and_current_owner_to_be_identical() -> None:
    value = _example()
    value["delegated_approver_user_ref"] = "USR-01ARZ3NDEKTSV4RRFFQ69G5FAX"
    value["payload_sha256"] = command_payload_digest(value)

    with pytest.raises(ApprovedCommandValidationError, match="delegated_approver_user_ref"):
        validate_email_send_approved_command(value)


def test_payload_digest_is_canonical_and_excludes_only_the_digest_field() -> None:
    value = _example()
    reordered = dict(reversed(list(value.items())))

    assert command_payload_digest(value) == command_payload_digest(reordered)
    assert command_payload_digest(value) == value["payload_sha256"]


def test_raw_authenticated_frappe_user_never_enters_approval_command_or_publication_shape() -> None:
    raw_user = "Owner.Name+Email@example.invalid"
    value = _example()
    serialized = json.dumps(
        {
            "approval": snapshot(),
            "command": value,
            "publication": {
                "command": value,
                "payload_digest": "sha256:" + value["payload_sha256"],
            },
        },
        sort_keys=True,
    )

    assert raw_user not in serialized
    assert raw_user not in repr(value)


def test_specialized_api_locks_and_atomically_issues_decision_command_and_publication() -> None:
    source = EMAIL_SEND_API.read_text(encoding="utf-8")

    assert "def submit_for_review(" in source
    assert "def approve(" in source
    assert source.count("for_update=True") >= 2
    for doctype in (
        "GBOS Review Decision",
        "GBOS Approved Command",
        "GBOS Command Publication",
    ):
        assert doctype in source
    assert "run_idempotent(" in source
    assert "frappe.db.commit" not in source
    assert "protect_live_email_send_snapshot" in source
    assert "protected_user_ref" in source


def test_specialized_api_never_persists_raw_user_or_message_material_fields() -> None:
    source = EMAIL_SEND_API.read_text(encoding="utf-8")

    for forbidden_field in (
        '"sender_address"',
        '"recipient_address"',
        '"body_html"',
        '"body_text"',
        '"mime_bytes"',
        '"provider_payload"',
        '"send_state"',
    ):
        assert forbidden_field not in source
