"""Composite live authority for one fenced approved email-send command."""

from __future__ import annotations

import hmac
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol, cast

from services.action_guard.email_send import EmailParticipantBinding, EmailSendAuthorityReceipt

from .email_send_authority import EmailSendAuthority
from .models import TenantScope
from .outbound import CommandPublication

_DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
_FRAPPE_FIELDS = frozenset(
    {
        "audience",
        "granted_scopes",
        "site_id",
        "processing_purpose",
        "team_ref",
        "authenticated_actor_user_ref",
        "delegated_approver_user_ref",
        "review_case_ref",
        "review_case_revision",
        "review_policy_version",
        "party_ref",
        "party_revision",
        "team_revision",
        "owner_user_ref",
        "owner_eligibility_revision",
        "participants",
        "final_mime_evidence_ref",
        "final_mime_digest",
        "evidence_refs",
        "request_id",
        "idempotency_key",
        "stable_client_request_id",
        "replay_payload_sha256",
    }
)
_GATEWAY_FIELDS = frozenset(
    {
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
    }
)


class EmailCommandAuthorityConflict(ValueError):
    """A safe, fail-closed live authority refusal."""


class FrappeCommandAuthorityClient(Protocol):
    def resolve(
        self,
        scope: TenantScope,
        publication: CommandPublication,
        command: Mapping[str, Any],
    ) -> Mapping[str, object]: ...


class EmailCommandAuthorityResolver:
    def __init__(
        self,
        *,
        frappe: FrappeCommandAuthorityClient,
        gateway: EmailSendAuthority,
        emergency_stop_reader: Callable[[], bool],
        external_send_reader: Callable[[], bool],
    ) -> None:
        self._frappe = frappe
        self._gateway = gateway
        self._emergency_stop_reader = emergency_stop_reader
        self._external_send_reader = external_send_reader

    def __repr__(self) -> str:
        return "EmailCommandAuthorityResolver(dependencies=<redacted>)"

    def __call__(
        self,
        scope: TenantScope,
        publication: CommandPublication,
        command: Mapping[str, Any],
    ) -> EmailSendAuthorityReceipt:
        try:
            frappe = self._frappe.resolve(scope, publication, command)
            gateway = self._gateway.validate_command(scope, command=command)
            stopped = self._emergency_stop_reader()
            external_send = self._external_send_reader()
            if not isinstance(stopped, bool) or not isinstance(external_send, bool):
                raise EmailCommandAuthorityConflict("runtime_authority_unavailable")
            _validate_mapping_shape(frappe, _FRAPPE_FIELDS)
            _validate_mapping_shape(gateway, _GATEWAY_FIELDS)
            participants = _participants(frappe["participants"])
            gateway_participants = _participants(gateway["participants"])
            if participants != gateway_participants:
                raise EmailCommandAuthorityConflict("participant_authority_drift")
            _match_frappe_command(frappe, command)
            _match_gateway_command(gateway, command)
            replay = frappe["replay_payload_sha256"]
            if not isinstance(replay, str) or not hmac.compare_digest(
                replay, str(command.get("payload_sha256", ""))
            ):
                raise EmailCommandAuthorityConflict("frappe_authority_drift")
            return EmailSendAuthorityReceipt(
                audience=_text(frappe, "audience"),
                granted_scopes=_string_tuple(frappe, "granted_scopes"),
                site_id=_text(frappe, "site_id"),
                processing_purpose=_text(frappe, "processing_purpose"),
                team_ref=_text(frappe, "team_ref"),
                authenticated_actor_user_ref=_text(frappe, "authenticated_actor_user_ref"),
                delegated_approver_user_ref=_text(frappe, "delegated_approver_user_ref"),
                review_case_ref=_text(frappe, "review_case_ref"),
                review_case_revision=_positive_int(frappe, "review_case_revision"),
                review_policy_version=_text(frappe, "review_policy_version"),
                mailbox_ref=_text(gateway, "mailbox_ref"),
                mailbox_config_revision=_positive_int(gateway, "mailbox_config_revision"),
                inbox_item_ref=_text(gateway, "inbox_item_ref"),
                inbox_item_revision=_positive_int(gateway, "inbox_item_revision"),
                conversation_ref=_text(gateway, "conversation_ref"),
                conversation_revision=_positive_int(gateway, "conversation_revision"),
                reply_draft_ref=_text(gateway, "reply_draft_ref"),
                reply_draft_revision=_positive_int(gateway, "reply_draft_revision"),
                reply_draft_digest=_digest(gateway, "reply_draft_digest"),
                participants=participants,
                party_ref=_text(frappe, "party_ref"),
                party_revision=_positive_int(frappe, "party_revision"),
                team_revision=_positive_int(frappe, "team_revision"),
                owner_user_ref=_text(frappe, "owner_user_ref"),
                owner_eligibility_revision=_digest(frappe, "owner_eligibility_revision"),
                final_mime_evidence_ref=_text(frappe, "final_mime_evidence_ref"),
                final_mime_digest=_digest(frappe, "final_mime_digest"),
                evidence_refs=_string_tuple(frappe, "evidence_refs"),
                request_id=_text(frappe, "request_id"),
                idempotency_key=_text(frappe, "idempotency_key"),
                stable_client_request_id=_text(frappe, "stable_client_request_id"),
                replay_payload_sha256=replay,
                emergency_stop_active=stopped,
                external_send_enabled=external_send,
            )
        except EmailCommandAuthorityConflict:
            raise
        except Exception as error:
            raise EmailCommandAuthorityConflict("email_command_authority_unavailable") from error


def _validate_mapping_shape(value: object, fields: frozenset[str]) -> None:
    if not isinstance(value, Mapping) or frozenset(value) != fields:
        raise EmailCommandAuthorityConflict("email_command_authority_shape_invalid")


def _match_frappe_command(authority: Mapping[str, object], command: Mapping[str, Any]) -> None:
    bindings = {
        "site_id": "site_id",
        "processing_purpose": "processing_purpose",
        "team_ref": "team_ref",
        "authenticated_actor_user_ref": "actor_user_ref",
        "delegated_approver_user_ref": "delegated_approver_user_ref",
        "review_case_ref": "review_case_ref",
        "review_case_revision": "review_case_revision",
        "review_policy_version": "review_policy_version",
        "party_ref": "party_ref",
        "party_revision": "party_revision",
        "team_revision": "team_revision",
        "owner_user_ref": "owner_user_ref",
        "owner_eligibility_revision": "owner_eligibility_revision",
        "final_mime_evidence_ref": "final_mime_evidence_ref",
        "final_mime_digest": "final_mime_digest",
        "request_id": "request_id",
        "idempotency_key": "idempotency_key",
        "stable_client_request_id": "stable_client_request_id",
    }
    if any(authority[left] != command.get(right) for left, right in bindings.items()):
        raise EmailCommandAuthorityConflict("frappe_authority_drift")
    evidence_refs = authority["evidence_refs"]
    if not isinstance(evidence_refs, Sequence) or isinstance(evidence_refs, (str, bytes)):
        raise EmailCommandAuthorityConflict("frappe_authority_drift")
    if tuple(evidence_refs) != tuple(command.get("evidence_refs", ())):
        raise EmailCommandAuthorityConflict("frappe_authority_drift")


def _match_gateway_command(authority: Mapping[str, object], command: Mapping[str, Any]) -> None:
    if any(authority[key] != command.get(key) for key in _GATEWAY_FIELDS - {"participants"}):
        raise EmailCommandAuthorityConflict("gateway_authority_drift")


def _participants(value: object) -> tuple[EmailParticipantBinding, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) < 2:
        raise EmailCommandAuthorityConflict("participant_authority_invalid")
    result: list[EmailParticipantBinding] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise EmailCommandAuthorityConflict("participant_authority_invalid")
        required = {"address_role", "opaque_address_ref"}
        if index:
            required |= {"identity_mapping_ref", "identity_mapping_revision"}
        allowed = required | (
            {"identity_mapping_ref", "identity_mapping_revision"} if not index else set()
        )
        if not required.issubset(item) or not set(item).issubset(allowed):
            raise EmailCommandAuthorityConflict("participant_authority_invalid")
        role = item["address_role"]
        opaque = item["opaque_address_ref"]
        mapping_ref = item.get("identity_mapping_ref")
        mapping_revision = item.get("identity_mapping_revision")
        if (
            not isinstance(role, str)
            or not isinstance(opaque, str)
            or (
                index == 0
                and (role != "sender" or mapping_ref is not None or mapping_revision is not None)
            )
            or (
                index > 0
                and (
                    role not in {"to", "cc", "bcc"}
                    or not isinstance(mapping_ref, str)
                    or not isinstance(mapping_revision, int)
                    or isinstance(mapping_revision, bool)
                    or mapping_revision < 1
                )
            )
        ):
            raise EmailCommandAuthorityConflict("participant_authority_invalid")
        result.append(
            EmailParticipantBinding(
                address_role=role,
                opaque_address_ref=opaque,
                identity_mapping_ref=cast(str | None, mapping_ref),
                identity_mapping_revision=cast(int | None, mapping_revision),
            )
        )
    return tuple(result)


def _text(value: Mapping[str, object], field: str) -> str:
    candidate = value[field]
    if not isinstance(candidate, str) or not candidate or candidate != candidate.strip():
        raise EmailCommandAuthorityConflict("email_command_authority_shape_invalid")
    return candidate


def _digest(value: Mapping[str, object], field: str) -> str:
    candidate = _text(value, field)
    if _DIGEST.fullmatch(candidate) is None:
        raise EmailCommandAuthorityConflict("email_command_authority_shape_invalid")
    return candidate


def _positive_int(value: Mapping[str, object], field: str) -> int:
    candidate = value[field]
    if not isinstance(candidate, int) or isinstance(candidate, bool) or candidate < 1:
        raise EmailCommandAuthorityConflict("email_command_authority_shape_invalid")
    return candidate


def _string_tuple(value: Mapping[str, object], field: str) -> tuple[str, ...]:
    candidate = value[field]
    if (
        not isinstance(candidate, Sequence)
        or isinstance(candidate, (str, bytes))
        or not all(isinstance(item, str) and item for item in candidate)
    ):
        raise EmailCommandAuthorityConflict("email_command_authority_shape_invalid")
    return tuple(candidate)


__all__ = ["EmailCommandAuthorityConflict", "EmailCommandAuthorityResolver"]
