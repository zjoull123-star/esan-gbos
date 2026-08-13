from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from esan_gbos.domain.email_review_policy import (
    EMAIL_SEND_REVIEW_POLICY,
    EmailSendReviewPolicyError,
    validate_email_send_approval_snapshot,
)

_COMMAND_FIELDS = frozenset(
    {
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
)
_PREFIXED = re.compile(r"^(?P<prefix>[A-Z]{3})-[0-9A-HJKMNP-TV-Z]{26}$")
_USER_REF = re.compile(r"^USR-[0-9A-HJKMNP-TV-Z]{26}$")
_IDEMPOTENCY = re.compile(r"^idem:v2:[a-f0-9]{64}$")
_HASH = re.compile(r"^[a-f0-9]{64}$")


class ApprovedCommandValidationError(ValueError):
    pass


def command_payload_digest(command: dict[str, Any]) -> str:
    payload = {key: value for key, value in command.items() if key != "payload_sha256"}
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    except TypeError, ValueError:
        raise ApprovedCommandValidationError("command must be canonical JSON") from None
    return hashlib.sha256(encoded).hexdigest()


def build_email_send_approved_command(
    approval_snapshot: object,
    *,
    command_id: str,
    actor_user_ref: str,
    review_case_ref: str,
    review_case_revision: int,
    request_id: str,
    idempotency_key: str,
    issued_at: datetime,
) -> dict[str, Any]:
    try:
        snapshot = validate_email_send_approval_snapshot(approval_snapshot)
    except EmailSendReviewPolicyError as error:
        raise ApprovedCommandValidationError(str(error)) from error
    issued = _aware(issued_at, "issued_at").isoformat().replace("+00:00", "Z")
    command = {
        "schema_version": "2.0",
        "command_id": command_id,
        "command_type": "email.send.approved",
        "site_id": snapshot["site_id"],
        "processing_purpose": snapshot["processing_purpose"],
        "team_ref": snapshot["team_ref"],
        "actor_user_ref": actor_user_ref,
        "delegated_approver_user_ref": snapshot["assignee_user_ref"],
        "review_case_ref": review_case_ref,
        "review_case_revision": review_case_revision,
        "review_policy_version": EMAIL_SEND_REVIEW_POLICY,
        "approval_expires_at": snapshot["approval_expires_at"],
        "mailbox_ref": snapshot["mailbox_ref"],
        "mailbox_config_revision": snapshot["mailbox_config_revision"],
        "inbox_item_ref": snapshot["inbox_item_ref"],
        "inbox_item_revision": snapshot["inbox_item_revision"],
        "conversation_ref": snapshot["conversation_ref"],
        "conversation_revision": snapshot["conversation_revision"],
        "reply_draft_ref": snapshot["reply_draft_ref"],
        "reply_draft_revision": snapshot["reply_draft_revision"],
        "reply_draft_digest": snapshot["reply_draft_digest"],
        "participants": snapshot["participants"],
        "party_ref": snapshot["party_ref"],
        "party_revision": snapshot["party_revision"],
        "team_revision": snapshot["team_revision"],
        "owner_user_ref": snapshot["owner_user_ref"],
        "owner_eligibility_revision": snapshot["owner_eligibility_revision"],
        "final_mime_evidence_ref": snapshot["final_mime_evidence_ref"],
        "final_mime_digest": snapshot["final_mime_digest"],
        "evidence_refs": snapshot["evidence_refs"],
        "request_id": request_id,
        "idempotency_key": idempotency_key,
        "stable_client_request_id": snapshot["stable_client_request_id"],
        "issued_at": issued,
    }
    command["payload_sha256"] = command_payload_digest(command)
    return validate_email_send_approved_command(command)


def validate_email_send_approved_command(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ApprovedCommandValidationError("command must be an object")
    supplied = frozenset(value)
    missing = _COMMAND_FIELDS - supplied
    unexpected = supplied - _COMMAND_FIELDS
    if missing:
        raise ApprovedCommandValidationError(
            f"missing required fields: {', '.join(sorted(missing))}"
        )
    if unexpected:
        unexpected_fields = ", ".join(sorted(unexpected))
        raise ApprovedCommandValidationError(f"unexpected fields: {unexpected_fields}")
    if value["schema_version"] != "2.0":
        raise ApprovedCommandValidationError("schema_version must be 2.0")
    if value["command_type"] != "email.send.approved":
        raise ApprovedCommandValidationError("command_type is invalid")
    if value["review_policy_version"] != EMAIL_SEND_REVIEW_POLICY:
        raise ApprovedCommandValidationError("review_policy_version is invalid")
    for field, prefix in (
        ("command_id", "CMD"),
        ("review_case_ref", "RVC"),
        ("request_id", "REQ"),
    ):
        _prefixed(value[field], field, prefix)
    for field in ("actor_user_ref", "delegated_approver_user_ref", "owner_user_ref"):
        if not isinstance(value[field], str) or _USER_REF.fullmatch(value[field]) is None:
            raise ApprovedCommandValidationError(f"{field} is invalid")
    if not (
        value["actor_user_ref"] == value["delegated_approver_user_ref"] == value["owner_user_ref"]
    ):
        raise ApprovedCommandValidationError(
            "delegated_approver_user_ref must equal actor_user_ref and owner_user_ref"
        )
    if (
        not isinstance(value["review_case_revision"], int)
        or isinstance(value["review_case_revision"], bool)
        or value["review_case_revision"] < 1
    ):
        raise ApprovedCommandValidationError("review_case_revision must be a positive integer")
    if (
        not isinstance(value["idempotency_key"], str)
        or _IDEMPOTENCY.fullmatch(value["idempotency_key"]) is None
    ):
        raise ApprovedCommandValidationError("idempotency_key is invalid")
    _aware_timestamp(value["issued_at"], "issued_at")
    _aware_timestamp(value["approval_expires_at"], "approval_expires_at")
    try:
        validate_email_send_approval_snapshot(
            {
                "schema_version": "1.0",
                "site_id": value["site_id"],
                "processing_purpose": value["processing_purpose"],
                "team_ref": value["team_ref"],
                "assignee_user_ref": value["delegated_approver_user_ref"],
                "approval_expires_at": value["approval_expires_at"],
                "mailbox_ref": value["mailbox_ref"],
                "mailbox_config_revision": value["mailbox_config_revision"],
                "inbox_item_ref": value["inbox_item_ref"],
                "inbox_item_revision": value["inbox_item_revision"],
                "conversation_ref": value["conversation_ref"],
                "conversation_revision": value["conversation_revision"],
                "reply_draft_ref": value["reply_draft_ref"],
                "reply_draft_revision": value["reply_draft_revision"],
                "reply_draft_digest": value["reply_draft_digest"],
                "participants": value["participants"],
                "party_ref": value["party_ref"],
                "party_revision": value["party_revision"],
                "team_revision": value["team_revision"],
                "owner_user_ref": value["owner_user_ref"],
                "owner_eligibility_revision": value["owner_eligibility_revision"],
                "final_mime_evidence_ref": value["final_mime_evidence_ref"],
                "final_mime_digest": value["final_mime_digest"],
                "evidence_refs": value["evidence_refs"],
                "stable_client_request_id": value["stable_client_request_id"],
            }
        )
    except EmailSendReviewPolicyError as error:
        raise ApprovedCommandValidationError(str(error)) from error
    if (
        not isinstance(value["payload_sha256"], str)
        or _HASH.fullmatch(value["payload_sha256"]) is None
    ):
        raise ApprovedCommandValidationError("payload_sha256 is invalid")
    if value["payload_sha256"] != command_payload_digest(value):
        raise ApprovedCommandValidationError("payload_sha256 does not match command")
    return {key: value[key] for key in _COMMAND_FIELDS}


def _prefixed(value: object, field: str, prefix: str) -> str:
    if not isinstance(value, str):
        raise ApprovedCommandValidationError(f"{field} is invalid")
    match = _PREFIXED.fullmatch(value)
    if match is None or match.group("prefix") != prefix:
        raise ApprovedCommandValidationError(f"{field} is invalid")
    return value


def _aware_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ApprovedCommandValidationError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ApprovedCommandValidationError(f"{field} is invalid") from None
    return _aware(parsed, field)


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ApprovedCommandValidationError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "ApprovedCommandValidationError",
    "build_email_send_approved_command",
    "command_payload_digest",
    "validate_email_send_approved_command",
]
