from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

EMAIL_SEND_REVIEW_POLICY = "email_send_owner_v1"
_EMAIL_SEND_PARTICIPANT_ROLES = {
    "sender": "mailbox_owner",
    "recipients": ["original_sender"],
}
EMAIL_SEND_PARTICIPANT_ROLES_DIGEST = (
    "sha256:"
    + hashlib.sha256(
        json.dumps(
            _EMAIL_SEND_PARTICIPANT_ROLES,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
)

_SNAPSHOT_FIELDS = frozenset(
    {
        "schema_version",
        "site_id",
        "processing_purpose",
        "team_ref",
        "assignee_user_ref",
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
        "stable_client_request_id",
    }
)
_LIVE_SNAPSHOT_FIELDS = (_SNAPSHOT_FIELDS - {"assignee_user_ref", "owner_user_ref"}) | {
    "assignee_user_name",
    "owner_user_name",
}
_PURPOSES = frozenset(
    {
        "business_operations",
        "customer_service",
        "sales_follow_up",
        "procurement_coordination",
        "product_sample_management",
    }
)
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@~-]{0,255}$")
_USER_NAME = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")
_SITE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,139}$")
_PREFIXED_REF = re.compile(r"^[A-Z]{3}-[0-9A-HJKMNP-TV-Z]{26}$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_OPAQUE_ADDRESS = re.compile(r"^extid:v1:email:[A-Za-z0-9_-]{43}$")
_PARTICIPANT_FIELDS = frozenset(
    {"address_role", "opaque_address_ref", "identity_mapping_ref", "identity_mapping_revision"}
)
_SERVICE_MARKERS = ("service:", "ai:")
_SERVICE_SUFFIX = "@localhost.invalid"
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_USER_REF_DOMAIN = b"gbos-user-ref-v1\x1f"


class EmailSendReviewPolicyError(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class EmailSendOwnerApproval:
    actor_user_ref: str
    team_ref: str
    processing_purpose: str
    policy_version: str
    expires_at: datetime


def email_send_participant_roles() -> dict[str, object]:
    """Return the one frozen reply role binding without exposing mutable module state."""

    return copy.deepcopy(_EMAIL_SEND_PARTICIPANT_ROLES)


def protected_user_ref(site_id: object, frappe_user_name: object) -> str:
    site = _site(site_id, "site_id")
    if (
        not isinstance(frappe_user_name, str)
        or _USER_NAME.fullmatch(frappe_user_name) is None
        or frappe_user_name != frappe_user_name.strip()
    ):
        raise EmailSendReviewPolicyError("invalid_snapshot", "frappe_user_name is invalid")
    user = frappe_user_name
    digest = hashlib.sha256(
        _USER_REF_DOMAIN + site.encode("utf-8") + b"\x1f" + user.encode("utf-8")
    ).digest()
    value = int.from_bytes(digest[:16], "big")
    encoded = ["0"] * 26
    for index in range(25, -1, -1):
        value, remainder = divmod(value, 32)
        encoded[index] = _CROCKFORD[remainder]
    return "USR-" + "".join(encoded)


def protect_live_email_send_snapshot(
    value: object,
    *,
    site_id: object,
    authenticated_user_name: object,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _LIVE_SNAPSHOT_FIELDS:
        raise EmailSendReviewPolicyError("invalid_snapshot", "live snapshot has invalid fields")
    active_site = _site(site_id, "site_id")
    if value.get("site_id") != active_site:
        raise EmailSendReviewPolicyError("site_mismatch", "live snapshot site differs")
    if (
        not isinstance(authenticated_user_name, str)
        or _USER_NAME.fullmatch(authenticated_user_name) is None
        or authenticated_user_name != authenticated_user_name.strip()
    ):
        raise EmailSendReviewPolicyError("actor_ineligible", "authenticated actor is invalid")
    if _actor_ineligible(authenticated_user_name):
        raise EmailSendReviewPolicyError("actor_ineligible", "authenticated actor is ineligible")
    if (
        value.get("assignee_user_name") != authenticated_user_name
        or value.get("owner_user_name") != authenticated_user_name
    ):
        raise EmailSendReviewPolicyError(
            "actor_not_current_owner", "authenticated actor is not current owner"
        )
    protected = copy.deepcopy(value)
    protected.pop("assignee_user_name")
    protected.pop("owner_user_name")
    protected_ref = protected_user_ref(active_site, authenticated_user_name)
    protected["assignee_user_ref"] = protected_ref
    protected["owner_user_ref"] = protected_ref
    return validate_email_send_approval_snapshot(protected)


def validate_email_send_approval_snapshot(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EmailSendReviewPolicyError("invalid_snapshot", "snapshot must be an object")
    supplied = frozenset(value)
    missing = _SNAPSHOT_FIELDS - supplied
    unexpected = supplied - _SNAPSHOT_FIELDS
    if missing:
        raise EmailSendReviewPolicyError(
            "invalid_snapshot", f"missing required fields: {', '.join(sorted(missing))}"
        )
    if unexpected:
        raise EmailSendReviewPolicyError(
            "invalid_snapshot", f"unexpected fields: {', '.join(sorted(unexpected))}"
        )

    result = copy.deepcopy(value)
    if result["schema_version"] != "1.0":
        raise EmailSendReviewPolicyError("invalid_snapshot", "schema_version must be 1.0")
    result["site_id"] = _site(result["site_id"], "site_id")
    purpose = _text(result["processing_purpose"], "processing_purpose", 25)
    if purpose not in _PURPOSES:
        raise EmailSendReviewPolicyError(
            "invalid_snapshot", "processing_purpose is not approved for email send"
        )
    result["processing_purpose"] = purpose
    for field, prefix in (
        ("team_ref", "TEM"),
        ("mailbox_ref", "MBX"),
        ("inbox_item_ref", "INB"),
        ("conversation_ref", "CNV"),
        ("reply_draft_ref", "DRF"),
        ("party_ref", "PTY"),
        ("final_mime_evidence_ref", "EVR"),
        ("stable_client_request_id", "CLI"),
    ):
        result[field] = _prefixed(result[field], field, prefix)
    for field in ("assignee_user_ref", "owner_user_ref"):
        result[field] = _ref(result[field], field)
    for field in (
        "mailbox_config_revision",
        "inbox_item_revision",
        "conversation_revision",
        "reply_draft_revision",
        "party_revision",
        "team_revision",
    ):
        result[field] = _positive_integer(result[field], field)
    for field in (
        "reply_draft_digest",
        "owner_eligibility_revision",
        "final_mime_digest",
    ):
        result[field] = _digest(result[field], field)
    result["approval_expires_at"] = (
        _timestamp(result["approval_expires_at"], "approval_expires_at")
        .isoformat()
        .replace("+00:00", "Z")
    )
    result["participants"] = _participants(result["participants"])
    result["evidence_refs"] = _evidence_refs(result["evidence_refs"])
    if result["assignee_user_ref"] != result["owner_user_ref"]:
        raise EmailSendReviewPolicyError(
            "invalid_snapshot", "assignee_user_ref must equal owner_user_ref"
        )
    return result


def authorize_email_send_owner(
    pinned_snapshot: object,
    *,
    live_snapshot: object,
    actor_user_ref: str,
    assigned_reviewer: str,
    case_team_ref: str,
    case_policy_version: str,
    now: datetime,
) -> EmailSendOwnerApproval:
    pinned = validate_email_send_approval_snapshot(pinned_snapshot)
    live = validate_email_send_approval_snapshot(live_snapshot)
    actor = _ref(actor_user_ref, "actor_user_ref")
    if _actor_ineligible(actor):
        raise EmailSendReviewPolicyError("actor_ineligible", "actor is not eligible")
    if actor != pinned["assignee_user_ref"] or assigned_reviewer != actor:
        raise EmailSendReviewPolicyError(
            "actor_not_assigned_owner", "actor is not the explicitly assigned current owner"
        )
    if case_team_ref != pinned["team_ref"]:
        raise EmailSendReviewPolicyError("team_mismatch", "review team differs from approval")
    if case_policy_version != EMAIL_SEND_REVIEW_POLICY:
        raise EmailSendReviewPolicyError("policy_mismatch", "review policy is not specialized")
    if live != pinned:
        raise EmailSendReviewPolicyError("live_authority_drift", "live authority differs")
    current = _aware(now, "now")
    expires = _timestamp(pinned["approval_expires_at"], "approval_expires_at")
    if expires <= current:
        raise EmailSendReviewPolicyError("approval_expired", "approval has expired")
    return EmailSendOwnerApproval(
        actor_user_ref=actor,
        team_ref=pinned["team_ref"],
        processing_purpose=pinned["processing_purpose"],
        policy_version=EMAIL_SEND_REVIEW_POLICY,
        expires_at=expires,
    )


def _participants(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 2 <= len(value) <= 256:
        raise EmailSendReviewPolicyError(
            "invalid_snapshot", "participants must contain 2 to 256 entries"
        )
    normalized: list[dict[str, Any]] = []
    senders = 0
    recipients = 0
    for index, item in enumerate(value):
        if not isinstance(item, dict) or not set(item) <= _PARTICIPANT_FIELDS:
            raise EmailSendReviewPolicyError(
                "invalid_snapshot", f"participants[{index}] has unexpected fields"
            )
        role = item.get("address_role")
        if role not in {"sender", "to", "cc", "bcc"}:
            raise EmailSendReviewPolicyError(
                "invalid_snapshot", f"participants[{index}].address_role is invalid"
            )
        opaque = item.get("opaque_address_ref")
        if not isinstance(opaque, str) or _OPAQUE_ADDRESS.fullmatch(opaque) is None:
            raise EmailSendReviewPolicyError(
                "invalid_snapshot", f"participants[{index}].opaque_address_ref is invalid"
            )
        normalized_item: dict[str, Any] = {
            "address_role": role,
            "opaque_address_ref": opaque,
        }
        if role == "sender":
            senders += 1
            if set(item) != {"address_role", "opaque_address_ref"}:
                raise EmailSendReviewPolicyError(
                    "invalid_snapshot", f"participants[{index}] sender mapping is forbidden"
                )
        else:
            recipients += 1
            if set(item) != _PARTICIPANT_FIELDS:
                raise EmailSendReviewPolicyError(
                    "invalid_snapshot", f"participants[{index}] recipient mapping is required"
                )
            normalized_item["identity_mapping_ref"] = _prefixed(
                item.get("identity_mapping_ref"),
                f"participants[{index}].identity_mapping_ref",
                "EID",
            )
            normalized_item["identity_mapping_revision"] = _positive_integer(
                item.get("identity_mapping_revision"),
                f"participants[{index}].identity_mapping_revision",
            )
        normalized.append(normalized_item)
    if senders != 1 or recipients < 1:
        raise EmailSendReviewPolicyError(
            "invalid_snapshot", "participants require one sender and at least one recipient"
        )
    if len({repr(item) for item in normalized}) != len(normalized):
        raise EmailSendReviewPolicyError("invalid_snapshot", "participants contain duplicates")
    return normalized


def _evidence_refs(value: object) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 256:
        raise EmailSendReviewPolicyError(
            "invalid_snapshot", "evidence_refs must contain 1 to 256 references"
        )
    result = [_prefixed(item, f"evidence_refs[{index}]", "EVR") for index, item in enumerate(value)]
    if len(set(result)) != len(result):
        raise EmailSendReviewPolicyError("invalid_snapshot", "evidence_refs contain duplicates")
    return result


def _actor_ineligible(value: str) -> bool:
    lowered = value.casefold()
    return (
        value in {"Guest", "Administrator"}
        or lowered.startswith(_SERVICE_MARKERS)
        or lowered.endswith(_SERVICE_SUFFIX)
    )


def _text(value: object, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or _REF.fullmatch(value) is None
    ):
        raise EmailSendReviewPolicyError("invalid_snapshot", f"{field} is invalid")
    return value


def _ref(value: object, field: str) -> str:
    return _text(value, field, 256)


def _site(value: object, field: str) -> str:
    if not isinstance(value, str) or _SITE.fullmatch(value) is None:
        raise EmailSendReviewPolicyError("invalid_snapshot", f"{field} is invalid")
    return value


def _prefixed(value: object, field: str, prefix: str) -> str:
    if (
        not isinstance(value, str)
        or _PREFIXED_REF.fullmatch(value) is None
        or not value.startswith(prefix + "-")
    ):
        raise EmailSendReviewPolicyError("invalid_snapshot", f"{field} is invalid")
    return value


def _positive_integer(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 2_147_483_647:
        raise EmailSendReviewPolicyError("invalid_snapshot", f"{field} must be a positive integer")
    return value


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise EmailSendReviewPolicyError("invalid_snapshot", f"{field} is invalid")
    return value


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise EmailSendReviewPolicyError("invalid_snapshot", f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise EmailSendReviewPolicyError("invalid_snapshot", f"{field} is invalid") from None
    return _aware(parsed, field)


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise EmailSendReviewPolicyError("invalid_snapshot", f"{field} must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "EMAIL_SEND_REVIEW_POLICY",
    "EMAIL_SEND_PARTICIPANT_ROLES_DIGEST",
    "EmailSendOwnerApproval",
    "EmailSendReviewPolicyError",
    "authorize_email_send_owner",
    "email_send_participant_roles",
    "protect_live_email_send_snapshot",
    "protected_user_ref",
    "validate_email_send_approval_snapshot",
]
