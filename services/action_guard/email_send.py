from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, NoReturn

from jsonschema import Draft202012Validator, FormatChecker

from .models import VerifiedEmailSendCommand, VerifiedEmailSendOutboxReceipt

EMAIL_SEND_POLICY_VERSION = "email_send_owner_v1"
EMAIL_SEND_SCHEMA_VERSION = "2.0"
EMAIL_SEND_COMMAND_TYPE = "email.send.approved"
EMAIL_COMMAND_EXECUTOR_AUDIENCE = "email-command-executor"
EMAIL_SEND_EXECUTE_SCOPE = "email-send-execute"

_SCHEMA_PATH = (
    Path(__file__).parents[2]
    / "contracts"
    / "email_gateway"
    / "email-send-approved-command-v2.0.schema.json"
)
_OUTBOX_RECEIPT_KEYS = frozenset({"command_receipt_ref", "send_outbox_ref", "payload_digest"})
_COMMAND_RECEIPT_REF = re.compile(r"^ECR-[0-9A-HJKMNP-TV-Z]{26}$")
_SEND_OUTBOX_REF = re.compile(r"^SOB-[0-9A-HJKMNP-TV-Z]{26}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class EmailSendVerificationError(ValueError):
    """Fail-closed email-send rejection with a safe, stable reason code."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True, slots=True, kw_only=True)
class EmailParticipantBinding:
    address_role: str
    opaque_address_ref: str
    identity_mapping_ref: str | None = None
    identity_mapping_revision: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class EmailSendAuthorityReceipt:
    """Authenticated live state used to recheck every frozen command binding."""

    audience: str
    granted_scopes: tuple[str, ...]
    site_id: str
    processing_purpose: str
    team_ref: str
    authenticated_actor_user_ref: str
    delegated_approver_user_ref: str
    review_case_ref: str
    review_case_revision: int
    review_policy_version: str
    mailbox_ref: str
    mailbox_config_revision: int
    inbox_item_ref: str
    inbox_item_revision: int
    conversation_ref: str
    conversation_revision: int
    reply_draft_ref: str
    reply_draft_revision: int
    reply_draft_digest: str
    participants: tuple[EmailParticipantBinding, ...]
    party_ref: str
    party_revision: int
    team_revision: int
    owner_user_ref: str
    owner_eligibility_revision: str
    final_mime_evidence_ref: str
    final_mime_digest: str
    evidence_refs: tuple[str, ...]
    request_id: str
    idempotency_key: str
    stable_client_request_id: str
    replay_payload_sha256: str | None
    emergency_stop_active: bool
    external_send_enabled: bool


def verify_email_send_command(
    command: Mapping[str, Any],
    *,
    authority: EmailSendAuthorityReceipt,
    now: datetime,
) -> VerifiedEmailSendCommand:
    """Convert one closed ApprovedCommand into the minimal executable capability."""

    _validate_contract(command)
    payload_digest = _canonical_payload_digest(command)
    if command["payload_sha256"] != payload_digest:
        _reject("payload_hash_mismatch")

    if authority.audience != EMAIL_COMMAND_EXECUTOR_AUDIENCE:
        _reject("audience_mismatch")
    if EMAIL_SEND_EXECUTE_SCOPE not in authority.granted_scopes:
        _reject("required_scope_missing")
    if authority.emergency_stop_active:
        _reject("emergency_stop_active")
    if not authority.external_send_enabled:
        _reject("external_send_disabled")

    if now.tzinfo is None or now.utcoffset() is None:
        _reject("verification_time_invalid")
    issued_at = _parse_datetime(command["issued_at"])
    expires_at = _parse_datetime(command["approval_expires_at"])
    if issued_at > now or expires_at <= issued_at:
        _reject("command_time_invalid")
    if expires_at <= now:
        _reject("approval_expired")

    _match(command, "site_id", authority.site_id, "site_mismatch")
    _match(
        command,
        "processing_purpose",
        authority.processing_purpose,
        "purpose_mismatch",
    )
    _match(command, "team_ref", authority.team_ref, "team_mismatch")
    _match(
        command,
        "actor_user_ref",
        authority.authenticated_actor_user_ref,
        "actor_mismatch",
    )
    _match(
        command,
        "delegated_approver_user_ref",
        authority.delegated_approver_user_ref,
        "delegation_mismatch",
    )
    _match(command, "owner_user_ref", authority.owner_user_ref, "owner_mismatch")
    if command["delegated_approver_user_ref"] != authority.owner_user_ref:
        _reject("delegated_approver_not_current_owner")
    if command["actor_user_ref"] != command["delegated_approver_user_ref"]:
        _reject("actor_mismatch")

    _match(command, "review_case_ref", authority.review_case_ref, "review_case_mismatch")
    _match(
        command,
        "review_case_revision",
        authority.review_case_revision,
        "review_case_revision_drift",
    )
    _match(
        command,
        "review_policy_version",
        authority.review_policy_version,
        "policy_version_mismatch",
    )
    _match(command, "mailbox_ref", authority.mailbox_ref, "mailbox_mismatch")
    _match(
        command,
        "mailbox_config_revision",
        authority.mailbox_config_revision,
        "mailbox_config_revision_drift",
    )
    _match(command, "inbox_item_ref", authority.inbox_item_ref, "inbox_item_mismatch")
    _match(
        command,
        "inbox_item_revision",
        authority.inbox_item_revision,
        "inbox_item_revision_drift",
    )
    _match(
        command,
        "conversation_ref",
        authority.conversation_ref,
        "conversation_mismatch",
    )
    _match(
        command,
        "conversation_revision",
        authority.conversation_revision,
        "conversation_revision_drift",
    )
    _match(command, "reply_draft_ref", authority.reply_draft_ref, "reply_draft_mismatch")
    _match(
        command,
        "reply_draft_revision",
        authority.reply_draft_revision,
        "reply_draft_revision_drift",
    )
    _match(
        command,
        "reply_draft_digest",
        authority.reply_draft_digest,
        "reply_draft_digest_drift",
    )
    _match(command, "party_ref", authority.party_ref, "party_mismatch")
    _match(command, "party_revision", authority.party_revision, "party_revision_drift")
    _match(command, "team_revision", authority.team_revision, "team_revision_drift")
    _match(
        command,
        "owner_eligibility_revision",
        authority.owner_eligibility_revision,
        "owner_eligibility_revision_drift",
    )

    command_participants = _participant_bindings(command)
    if not _recipient_bindings_are_unique(command_participants):
        _reject("participant_binding_invalid")
    if command_participants != authority.participants:
        _reject("participant_binding_drift")

    _match(
        command,
        "final_mime_evidence_ref",
        authority.final_mime_evidence_ref,
        "final_mime_evidence_drift",
    )
    _match(
        command,
        "final_mime_digest",
        authority.final_mime_digest,
        "final_mime_digest_drift",
    )
    command_evidence = tuple(command["evidence_refs"])
    if command["final_mime_evidence_ref"] not in command_evidence:
        _reject("final_mime_evidence_missing")
    if len(authority.evidence_refs) != len(set(authority.evidence_refs)):
        _reject("evidence_mismatch")
    if command_evidence != authority.evidence_refs:
        _reject("evidence_mismatch")

    _match(command, "request_id", authority.request_id, "request_replay_drift")
    _match(
        command,
        "idempotency_key",
        authority.idempotency_key,
        "idempotency_replay_drift",
    )
    _match(
        command,
        "stable_client_request_id",
        authority.stable_client_request_id,
        "stable_request_replay_drift",
    )
    if (
        authority.replay_payload_sha256 is not None
        and authority.replay_payload_sha256 != payload_digest
    ):
        _reject("payload_replay_drift")

    return VerifiedEmailSendCommand(
        command_ref=command["command_id"],
        idempotency_key=command["idempotency_key"],
        stable_client_request_id=command["stable_client_request_id"],
        payload_digest=payload_digest,
        policy_version=command["review_policy_version"],
    )


def verify_email_send_post_result(
    verified: VerifiedEmailSendCommand,
    result_payload: Mapping[str, Any],
) -> VerifiedEmailSendOutboxReceipt:
    """Accept only the immutable command-receipt plus Send Outbox creation result."""

    if frozenset(result_payload) != _OUTBOX_RECEIPT_KEYS:
        _reject("outbox_receipt_invalid")
    command_receipt_ref = result_payload.get("command_receipt_ref")
    send_outbox_ref = result_payload.get("send_outbox_ref")
    payload_digest = result_payload.get("payload_digest")
    if not isinstance(command_receipt_ref, str):
        _reject("outbox_receipt_invalid")
    if _COMMAND_RECEIPT_REF.fullmatch(command_receipt_ref) is None:
        _reject("outbox_receipt_invalid")
    if not isinstance(send_outbox_ref, str):
        _reject("outbox_receipt_invalid")
    if _SEND_OUTBOX_REF.fullmatch(send_outbox_ref) is None:
        _reject("outbox_receipt_invalid")
    if not isinstance(payload_digest, str):
        _reject("outbox_receipt_invalid")
    if _SHA256.fullmatch(payload_digest) is None or payload_digest != verified.payload_digest:
        _reject("outbox_receipt_invalid")
    return VerifiedEmailSendOutboxReceipt(
        command_receipt_ref=command_receipt_ref,
        send_outbox_ref=send_outbox_ref,
        payload_digest=payload_digest,
    )


def _validate_contract(command: Mapping[str, Any]) -> None:
    if command.get("schema_version") != EMAIL_SEND_SCHEMA_VERSION:
        _reject("contract_version_mismatch")
    if command.get("command_type") != EMAIL_SEND_COMMAND_TYPE:
        _reject("command_type_mismatch")
    if command.get("review_policy_version") != EMAIL_SEND_POLICY_VERSION:
        _reject("policy_version_mismatch")
    if next(_validator().iter_errors(dict(command)), None) is not None:
        _reject("command_contract_invalid")


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(schema, dict):
        raise RuntimeError("email send command schema must be an object")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _canonical_payload_digest(command: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in command.items() if key != "payload_sha256"}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _participant_bindings(command: Mapping[str, Any]) -> tuple[EmailParticipantBinding, ...]:
    items = command["participants"]
    return tuple(
        EmailParticipantBinding(
            address_role=item["address_role"],
            opaque_address_ref=item["opaque_address_ref"],
            identity_mapping_ref=item.get("identity_mapping_ref"),
            identity_mapping_revision=item.get("identity_mapping_revision"),
        )
        for item in items
    )


def _recipient_bindings_are_unique(
    bindings: tuple[EmailParticipantBinding, ...],
) -> bool:
    recipients = tuple(
        binding.opaque_address_ref for binding in bindings if binding.address_role != "sender"
    )
    mapping_refs = tuple(
        binding.identity_mapping_ref for binding in bindings if binding.address_role != "sender"
    )
    return len(recipients) == len(set(recipients)) and len(mapping_refs) == len(set(mapping_refs))


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        _reject("command_contract_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _reject("command_contract_invalid")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _reject("command_contract_invalid")
    return parsed


def _match(
    command: Mapping[str, Any],
    field: str,
    live_value: object,
    reason_code: str,
) -> None:
    if command[field] != live_value:
        _reject(reason_code)


def _reject(reason_code: str) -> NoReturn:
    raise EmailSendVerificationError(reason_code)
