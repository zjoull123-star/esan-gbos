from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from services.action_guard.email_send import (
    EMAIL_COMMAND_EXECUTOR_AUDIENCE,
    EMAIL_SEND_EXECUTE_SCOPE,
    EmailParticipantBinding,
    EmailSendAuthorityReceipt,
    EmailSendVerificationError,
    verify_email_send_command,
    verify_email_send_post_result,
)
from services.action_guard.models import VerifiedEmailSendCommand

ROOT = Path(__file__).parents[2]
EXAMPLE = ROOT / "contracts" / "email_gateway" / "examples" / "email-send-approved-command-v2.json"
NOW = datetime(2026, 8, 13, 13, 5, tzinfo=UTC)


def _payload_digest(command: dict[str, Any]) -> str:
    payload = {key: value for key, value in command.items() if key != "payload_sha256"}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def command() -> dict[str, Any]:
    value = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    value["payload_sha256"] = _payload_digest(value)
    return value


def participants(value: dict[str, Any]) -> tuple[EmailParticipantBinding, ...]:
    return tuple(
        EmailParticipantBinding(
            address_role=item["address_role"],
            opaque_address_ref=item["opaque_address_ref"],
            identity_mapping_ref=item.get("identity_mapping_ref"),
            identity_mapping_revision=item.get("identity_mapping_revision"),
        )
        for item in value["participants"]
    )


def authority(value: dict[str, Any]) -> EmailSendAuthorityReceipt:
    return EmailSendAuthorityReceipt(
        audience=EMAIL_COMMAND_EXECUTOR_AUDIENCE,
        granted_scopes=(EMAIL_SEND_EXECUTE_SCOPE,),
        site_id=value["site_id"],
        processing_purpose=value["processing_purpose"],
        team_ref=value["team_ref"],
        authenticated_actor_user_ref=value["actor_user_ref"],
        delegated_approver_user_ref=value["delegated_approver_user_ref"],
        review_case_ref=value["review_case_ref"],
        review_case_revision=value["review_case_revision"],
        review_policy_version=value["review_policy_version"],
        mailbox_ref=value["mailbox_ref"],
        mailbox_config_revision=value["mailbox_config_revision"],
        inbox_item_ref=value["inbox_item_ref"],
        inbox_item_revision=value["inbox_item_revision"],
        conversation_ref=value["conversation_ref"],
        conversation_revision=value["conversation_revision"],
        reply_draft_ref=value["reply_draft_ref"],
        reply_draft_revision=value["reply_draft_revision"],
        reply_draft_digest=value["reply_draft_digest"],
        participants=participants(value),
        party_ref=value["party_ref"],
        party_revision=value["party_revision"],
        team_revision=value["team_revision"],
        owner_user_ref=value["owner_user_ref"],
        owner_eligibility_revision=value["owner_eligibility_revision"],
        final_mime_evidence_ref=value["final_mime_evidence_ref"],
        final_mime_digest=value["final_mime_digest"],
        evidence_refs=tuple(value["evidence_refs"]),
        request_id=value["request_id"],
        idempotency_key=value["idempotency_key"],
        stable_client_request_id=value["stable_client_request_id"],
        replay_payload_sha256=None,
        emergency_stop_active=False,
        external_send_enabled=True,
    )


def authority_with(
    receipt: EmailSendAuthorityReceipt,
    changes: dict[str, object],
) -> EmailSendAuthorityReceipt:
    untyped_replace = cast(Any, replace)
    return cast(EmailSendAuthorityReceipt, untyped_replace(receipt, **changes))


def _with_command_change(
    value: dict[str, Any],
    field: str,
    changed: object,
) -> dict[str, Any]:
    changed_command = copy.deepcopy(value)
    changed_command[field] = changed
    changed_command["payload_sha256"] = _payload_digest(changed_command)
    return changed_command


def _assert_rejected(
    value: dict[str, Any],
    receipt: EmailSendAuthorityReceipt,
    reason: str,
) -> None:
    with pytest.raises(EmailSendVerificationError) as caught:
        verify_email_send_command(value, authority=receipt, now=NOW)
    assert caught.value.reason_code == reason


def test_closed_command_and_live_authority_create_minimal_verified_command() -> None:
    value = command()

    verified = verify_email_send_command(value, authority=authority(value), now=NOW)

    assert verified == VerifiedEmailSendCommand(
        command_ref=value["command_id"],
        idempotency_key=value["idempotency_key"],
        stable_client_request_id=value["stable_client_request_id"],
        payload_digest=value["payload_sha256"],
        policy_version="email_send_owner_v1",
    )
    assert not hasattr(verified, "approved")


@pytest.mark.parametrize(
    ("field", "changed", "reason"),
    [
        ("schema_version", "1.0", "contract_version_mismatch"),
        ("command_type", "external.message.send", "command_type_mismatch"),
        ("review_policy_version", "generic_human_review", "policy_version_mismatch"),
    ],
)
def test_wrong_contract_command_or_policy_is_rejected(
    field: str,
    changed: object,
    reason: str,
) -> None:
    value = command()
    _assert_rejected(_with_command_change(value, field, changed), authority(value), reason)


def test_closed_schema_rejects_unknown_or_missing_fields() -> None:
    value = command()
    extra = {**value, "approved": True}
    extra["payload_sha256"] = _payload_digest(extra)
    _assert_rejected(extra, authority(value), "command_contract_invalid")

    missing = copy.deepcopy(value)
    missing.pop("review_case_ref")
    missing["payload_sha256"] = _payload_digest(missing)
    _assert_rejected(missing, authority(value), "command_contract_invalid")


@pytest.mark.parametrize(
    ("authority_change", "reason"),
    [
        ({"audience": "gbos-services"}, "audience_mismatch"),
        ({"site_id": "other.localhost"}, "site_mismatch"),
        ({"team_ref": "TEM-01ARZ3NDEKTSV4RRFFQ69G5FAX"}, "team_mismatch"),
        ({"processing_purpose": "sales_follow_up"}, "purpose_mismatch"),
        (
            {"authenticated_actor_user_ref": "USR-01ARZ3NDEKTSV4RRFFQ69G5FAX"},
            "actor_mismatch",
        ),
        (
            {"delegated_approver_user_ref": "USR-01ARZ3NDEKTSV4RRFFQ69G5FAX"},
            "delegation_mismatch",
        ),
        ({"owner_user_ref": "USR-01ARZ3NDEKTSV4RRFFQ69G5FAX"}, "owner_mismatch"),
    ],
)
def test_request_authority_identity_must_match_live_receipts(
    authority_change: dict[str, object],
    reason: str,
) -> None:
    value = command()
    _assert_rejected(value, authority_with(authority(value), authority_change), reason)


def test_delegated_approver_must_still_be_authenticated_current_owner() -> None:
    value = command()
    changed = _with_command_change(
        value,
        "delegated_approver_user_ref",
        "USR-01ARZ3NDEKTSV4RRFFQ69G5FAX",
    )
    _assert_rejected(changed, authority(changed), "delegated_approver_not_current_owner")


def test_expired_command_is_rejected() -> None:
    value = command()
    expired = _with_command_change(
        value,
        "approval_expires_at",
        (NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
    )
    _assert_rejected(expired, authority(expired), "approval_expired")


@pytest.mark.parametrize(
    ("issued_at", "expires_at"),
    [
        (NOW + timedelta(seconds=1), NOW + timedelta(hours=1)),
        (NOW - timedelta(hours=1), NOW - timedelta(hours=1)),
    ],
)
def test_command_time_window_must_be_ordered_and_already_issued(
    issued_at: datetime,
    expires_at: datetime,
) -> None:
    value = command()
    value["issued_at"] = issued_at.isoformat().replace("+00:00", "Z")
    value["approval_expires_at"] = expires_at.isoformat().replace("+00:00", "Z")
    value["payload_sha256"] = _payload_digest(value)

    _assert_rejected(value, authority(value), "command_time_invalid")


@pytest.mark.parametrize(
    ("receipt_field", "changed", "reason"),
    [
        ("review_case_revision", 4, "review_case_revision_drift"),
        ("mailbox_config_revision", 8, "mailbox_config_revision_drift"),
        ("inbox_item_revision", 12, "inbox_item_revision_drift"),
        ("conversation_revision", 6, "conversation_revision_drift"),
        ("reply_draft_revision", 5, "reply_draft_revision_drift"),
        (
            "reply_draft_digest",
            "sha256:" + "9" * 64,
            "reply_draft_digest_drift",
        ),
        ("party_revision", 9, "party_revision_drift"),
        ("team_revision", 7, "team_revision_drift"),
        (
            "owner_eligibility_revision",
            "sha256:" + "9" * 64,
            "owner_eligibility_revision_drift",
        ),
    ],
)
def test_every_pinned_authority_revision_and_digest_is_live(
    receipt_field: str,
    changed: object,
    reason: str,
) -> None:
    value = command()
    _assert_rejected(
        value,
        authority_with(authority(value), {receipt_field: changed}),
        reason,
    )


@pytest.mark.parametrize(
    ("receipt_field", "changed", "reason"),
    [
        ("review_case_ref", "RVC-01ARZ3NDEKTSV4RRFFQ69G5FAX", "review_case_mismatch"),
        ("mailbox_ref", "MBX-01ARZ3NDEKTSV4RRFFQ69G5FAX", "mailbox_mismatch"),
        ("inbox_item_ref", "INB-01ARZ3NDEKTSV4RRFFQ69G5FAX", "inbox_item_mismatch"),
        ("conversation_ref", "CNV-01ARZ3NDEKTSV4RRFFQ69G5FAX", "conversation_mismatch"),
        ("reply_draft_ref", "DRF-01ARZ3NDEKTSV4RRFFQ69G5FAX", "reply_draft_mismatch"),
        ("party_ref", "PTY-01ARZ3NDEKTSV4RRFFQ69G5FAX", "party_mismatch"),
    ],
)
def test_every_pinned_authority_reference_is_live(
    receipt_field: str,
    changed: object,
    reason: str,
) -> None:
    value = command()
    _assert_rejected(
        value,
        authority_with(authority(value), {receipt_field: changed}),
        reason,
    )


def test_recipient_envelope_and_mapping_revisions_are_bound() -> None:
    value = command()
    live = list(participants(value))
    live[1] = replace(live[1], identity_mapping_revision=10)
    _assert_rejected(
        value,
        replace(authority(value), participants=tuple(live)),
        "participant_binding_drift",
    )

    live = list(participants(value))
    live[1] = replace(
        live[1],
        opaque_address_ref="extid:v1:email:" + "D" * 43,
    )
    _assert_rejected(
        value,
        replace(authority(value), participants=tuple(live)),
        "participant_binding_drift",
    )


@pytest.mark.parametrize(
    ("receipt_field", "changed", "reason"),
    [
        (
            "final_mime_evidence_ref",
            "EVR-01ARZ3NDEKTSV4RRFFQ69G5FAX",
            "final_mime_evidence_drift",
        ),
        ("final_mime_digest", "sha256:" + "9" * 64, "final_mime_digest_drift"),
    ],
)
def test_final_mime_evidence_and_digest_are_live(
    receipt_field: str,
    changed: object,
    reason: str,
) -> None:
    value = command()
    _assert_rejected(
        value,
        authority_with(authority(value), {receipt_field: changed}),
        reason,
    )


def test_evidence_is_required_unique_and_live() -> None:
    value = command()
    missing_mime = copy.deepcopy(value)
    missing_mime["evidence_refs"].remove(missing_mime["final_mime_evidence_ref"])
    missing_mime["payload_sha256"] = _payload_digest(missing_mime)
    _assert_rejected(missing_mime, authority(missing_mime), "final_mime_evidence_missing")

    duplicate = copy.deepcopy(value)
    duplicate["evidence_refs"].append(duplicate["evidence_refs"][0])
    duplicate["payload_sha256"] = _payload_digest(duplicate)
    _assert_rejected(duplicate, authority(value), "command_contract_invalid")

    _assert_rejected(
        value,
        replace(authority(value), evidence_refs=(value["evidence_refs"][0],)),
        "evidence_mismatch",
    )


@pytest.mark.parametrize(
    ("receipt_change", "reason"),
    [
        ({"request_id": "REQ-01ARZ3NDEKTSV4RRFFQ69G5FAX"}, "request_replay_drift"),
        (
            {"idempotency_key": "idem:v2:" + "9" * 64},
            "idempotency_replay_drift",
        ),
        (
            {"stable_client_request_id": "CLI-01ARZ3NDEKTSV4RRFFQ69G5FAX"},
            "stable_request_replay_drift",
        ),
        ({"replay_payload_sha256": "9" * 64}, "payload_replay_drift"),
    ],
)
def test_command_replay_must_preserve_all_stable_bindings(
    receipt_change: dict[str, object],
    reason: str,
) -> None:
    value = command()
    _assert_rejected(value, authority_with(authority(value), receipt_change), reason)


def test_payload_hash_is_recomputed_not_trusted() -> None:
    value = command()
    value["reply_draft_revision"] += 1
    _assert_rejected(value, authority(command()), "payload_hash_mismatch")


@pytest.mark.parametrize(
    ("receipt_change", "reason"),
    [
        ({"granted_scopes": ()}, "required_scope_missing"),
        ({"emergency_stop_active": True}, "emergency_stop_active"),
        ({"external_send_enabled": False}, "external_send_disabled"),
    ],
)
def test_runtime_scope_and_safety_switches_fail_closed(
    receipt_change: dict[str, object],
    reason: str,
) -> None:
    value = command()
    _assert_rejected(value, authority_with(authority(value), receipt_change), reason)


def test_post_result_accepts_only_closed_immutable_send_outbox_receipt() -> None:
    value = command()
    verified = verify_email_send_command(value, authority=authority(value), now=NOW)
    receipt = {
        "command_receipt_ref": "ECR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "send_outbox_ref": "SOB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "payload_digest": verified.payload_digest,
    }

    accepted = verify_email_send_post_result(verified, receipt)

    assert accepted.command_receipt_ref == receipt["command_receipt_ref"]
    assert accepted.send_outbox_ref == receipt["send_outbox_ref"]
    assert accepted.payload_digest == verified.payload_digest


@pytest.mark.parametrize(
    "change",
    [
        {"payload_digest": "9" * 64},
        {"provider_receipt": {"provider_id": "provider-1"}},
        {"execution": {"sent": True}},
        {"external_send": True},
    ],
)
def test_post_result_rejects_drift_and_provider_or_execution_payloads(
    change: dict[str, object],
) -> None:
    value = command()
    verified = verify_email_send_command(value, authority=authority(value), now=NOW)
    receipt: dict[str, object] = {
        "command_receipt_ref": "ECR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "send_outbox_ref": "SOB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "payload_digest": verified.payload_digest,
    }
    receipt.update(change)

    with pytest.raises(EmailSendVerificationError) as caught:
        verify_email_send_post_result(verified, receipt)

    assert caught.value.reason_code == "outbox_receipt_invalid"
