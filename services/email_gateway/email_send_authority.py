"""Server-derived authority for governed email-send review requests."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from .mailboxes import MailboxRegistry
from .models import Conversation, Draft, InboxItem, TenantScope, canonical_digest
from .repository import (
    IdentityProjectionRepository,
    ParticipantAuthorityBindingReader,
)
from .security import GatewayAuthorizationIssuer, validate_participant_authority_binding

_OPAQUE_EMAIL = re.compile(r"^extid:v1:email:[A-Za-z0-9_-]{43}$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_PARTICIPANT_ROLES_DIGEST = canonical_digest(
    {"sender": "mailbox_owner", "recipients": ["original_sender"]}
)
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_USER_REF_DOMAIN = b"gbos-user-ref-v1\x1f"
_SNAPSHOT_FIELDS = frozenset(
    {
        "site_id",
        "processing_purpose",
        "team_ref",
        "assignee_user_name",
        "mailbox_ref",
        "mailbox_config_revision",
        "inbox_item_ref",
        "inbox_item_revision",
        "conversation_ref",
        "conversation_revision",
        "party_ref",
        "owner_user_name",
        "reply_draft_ref",
        "reply_draft_revision",
        "reply_draft_digest",
    }
)


class EmailSendAuthorityConflict(ValueError):
    """Current Gateway state cannot authorize the requested review action."""


class _WorkflowReader(Protocol):
    def get_inbox(self, scope: TenantScope, inbox_ref: str) -> InboxItem | None: ...

    def get_conversation_for(self, scope: TenantScope, inbox_ref: str) -> Conversation | None: ...

    def get_draft(self, scope: TenantScope, draft_ref: str) -> Draft | None: ...


class EmailSendAuthority:
    """Derive and revalidate review authority without trusting browser snapshots."""

    def __init__(
        self,
        *,
        mailboxes: MailboxRegistry,
        workflow: _WorkflowReader,
        identities: IdentityProjectionRepository,
        binding_reader: ParticipantAuthorityBindingReader,
        authorization_issuer: GatewayAuthorizationIssuer,
    ) -> None:
        self._mailboxes = mailboxes
        self._workflow = workflow
        self._identities = identities
        self._binding_reader = binding_reader
        self._authorization_issuer = authorization_issuer

    def __repr__(self) -> str:
        return "EmailSendAuthority(dependencies=<redacted>)"

    def authorize(
        self,
        scope: TenantScope,
        *,
        actor_ref: str,
        inbox_item_ref: str,
        draft_ref: str,
        expected_inbox_revision: int,
        expected_draft_revision: int,
        participant_roles_digest: str,
    ) -> dict[str, object]:
        if (
            _DIGEST.fullmatch(participant_roles_digest) is None
            or participant_roles_digest != _PARTICIPANT_ROLES_DIGEST
        ):
            raise EmailSendAuthorityConflict("participant_roles_invalid")
        snapshot, inbox, draft = self._current_snapshot(
            scope,
            actor_ref=actor_ref,
            inbox_item_ref=inbox_item_ref,
            draft_ref=draft_ref,
        )
        if inbox.revision != expected_inbox_revision or draft.revision != expected_draft_revision:
            raise EmailSendAuthorityConflict("gateway_authority_drift")
        binding_value = self._binding_reader.load_participant_authority_binding(
            scope,
            inbox_item_ref=inbox.inbox_item_ref,
        )
        try:
            binding = validate_participant_authority_binding(
                binding_value,
                inbox_item_ref=inbox.inbox_item_ref,
            )
        except ValueError as error:
            raise EmailSendAuthorityConflict("participant_authority_unavailable") from error
        if (
            binding["message_ref"] != inbox.message_ref
            or binding["mailbox_ref"] != inbox.mailbox_ref
            or binding["mailbox_config_revision"] != snapshot["mailbox_config_revision"]
        ):
            raise EmailSendAuthorityConflict("participant_authority_drift")
        authorization = self._authorization_issuer.issue_draft(
            site_id=scope.site_id,
            actor_ref=actor_ref,
            team_ref=inbox.team_ref,
            inbox_item_ref=inbox.inbox_item_ref,
            draft_ref=draft.draft_ref,
            draft_revision=draft.revision,
            request_digest=draft.content_digest,
            participant_authority_binding=binding,
            participant_roles_digest=participant_roles_digest,
        )
        return {
            "gateway_snapshot": snapshot,
            "draft_authorization": authorization,
            "draft_evidence_ref": draft.content_evidence_ref,
        }

    def validate(
        self,
        scope: TenantScope,
        *,
        actor_ref: str,
        expected_gateway_snapshot: object,
        participant_projection: object,
    ) -> dict[str, object]:
        expected = _validate_snapshot(expected_gateway_snapshot)
        current, _inbox, _draft = self._current_snapshot(
            scope,
            actor_ref=actor_ref,
            inbox_item_ref=str(expected["inbox_item_ref"]),
            draft_ref=str(expected["reply_draft_ref"]),
        )
        if current != expected:
            raise EmailSendAuthorityConflict("gateway_authority_drift")
        participants = _validate_participant_projection(participant_projection)
        recipient = participants[1]
        projection = self._identities.get(
            scope,
            str(recipient["opaque_address_ref"]),
        )
        if (
            projection is None
            or projection.status != "confirmed"
            or projection.identity_type != "Party"
            or projection.team_ref != current["team_ref"]
            or projection.processing_purpose != scope.processing_purpose
        ):
            raise EmailSendAuthorityConflict("recipient_identity_unavailable")
        return {
            "gateway_snapshot": current,
            "participants": [
                participants[0],
                {
                    **recipient,
                    "identity_mapping_ref": projection.external_identity_ref,
                    "identity_mapping_revision": projection.external_identity_revision,
                },
            ],
        }

    def validate_command(
        self,
        scope: TenantScope,
        *,
        command: Mapping[str, Any],
    ) -> dict[str, object]:
        """Reload Gateway-owned send bindings and require an outbound-capable mailbox."""

        actor_ref = command.get("actor_user_ref")
        inbox_ref = command.get("inbox_item_ref")
        draft_ref = command.get("reply_draft_ref")
        if not all(isinstance(value, str) for value in (actor_ref, inbox_ref, draft_ref)):
            raise EmailSendAuthorityConflict("gateway_authority_drift")
        candidate_inbox = self._workflow.get_inbox(scope, str(inbox_ref))
        actor_name = None if candidate_inbox is None else candidate_inbox.assignee_user_ref
        if actor_name is None or _protected_user_ref(scope.site_id, actor_name) != actor_ref:
            raise EmailSendAuthorityConflict("inbox_authority_unavailable")
        current, inbox, draft = self._current_snapshot(
            scope,
            actor_ref=actor_name,
            inbox_item_ref=str(inbox_ref),
            draft_ref=str(draft_ref),
            require_outbound=True,
        )
        expected = {
            "mailbox_ref": current["mailbox_ref"],
            "mailbox_config_revision": current["mailbox_config_revision"],
            "inbox_item_ref": current["inbox_item_ref"],
            "inbox_item_revision": current["inbox_item_revision"],
            "conversation_ref": current["conversation_ref"],
            "conversation_revision": current["conversation_revision"],
            "reply_draft_ref": current["reply_draft_ref"],
            "reply_draft_revision": current["reply_draft_revision"],
            "reply_draft_digest": current["reply_draft_digest"],
        }
        if any(command.get(key) != value for key, value in expected.items()):
            raise EmailSendAuthorityConflict("gateway_authority_drift")
        if (
            command.get("team_ref") != current["team_ref"]
            or command.get("party_ref") != current["party_ref"]
            or command.get("owner_user_ref") != actor_ref
            or command.get("delegated_approver_user_ref") != actor_ref
        ):
            raise EmailSendAuthorityConflict("gateway_authority_drift")

        mailbox = self._mailboxes.get(scope, inbox.mailbox_ref)
        if mailbox is None or mailbox.mailbox_address_identity_ref is None:
            raise EmailSendAuthorityConflict("mailbox_authority_unavailable")
        participants = command.get("participants")
        if not isinstance(participants, Sequence) or isinstance(participants, (str, bytes)):
            raise EmailSendAuthorityConflict("participant_projection_invalid")
        resolved: list[dict[str, object]] = []
        for index, item in enumerate(participants):
            if not isinstance(item, Mapping):
                raise EmailSendAuthorityConflict("participant_projection_invalid")
            role = item.get("address_role")
            opaque_ref = item.get("opaque_address_ref")
            if (
                not isinstance(role, str)
                or not isinstance(opaque_ref, str)
                or _OPAQUE_EMAIL.fullmatch(opaque_ref) is None
            ):
                raise EmailSendAuthorityConflict("participant_projection_invalid")
            if index == 0:
                if (
                    role != "sender"
                    or opaque_ref != mailbox.mailbox_address_identity_ref
                    or set(item) != {"address_role", "opaque_address_ref"}
                ):
                    raise EmailSendAuthorityConflict("participant_projection_invalid")
                resolved.append(
                    {
                        "address_role": role,
                        "opaque_address_ref": opaque_ref,
                        "identity_mapping_ref": None,
                        "identity_mapping_revision": None,
                    }
                )
                continue
            if role not in {"to", "cc", "bcc"} or set(item) != {
                "address_role",
                "opaque_address_ref",
                "identity_mapping_ref",
                "identity_mapping_revision",
            }:
                raise EmailSendAuthorityConflict("participant_projection_invalid")
            projection = self._identities.get(scope, opaque_ref)
            if (
                projection is None
                or projection.status != "confirmed"
                or projection.identity_type != "Party"
                or projection.team_ref != current["team_ref"]
                or projection.processing_purpose != scope.processing_purpose
                or item.get("identity_mapping_ref") != projection.external_identity_ref
                or item.get("identity_mapping_revision") != projection.external_identity_revision
            ):
                raise EmailSendAuthorityConflict("recipient_identity_unavailable")
            resolved.append(
                {
                    "address_role": role,
                    "opaque_address_ref": opaque_ref,
                    "identity_mapping_ref": projection.external_identity_ref,
                    "identity_mapping_revision": projection.external_identity_revision,
                }
            )
        if len(resolved) < 2 or draft.content_digest != command.get("reply_draft_digest"):
            raise EmailSendAuthorityConflict("gateway_authority_drift")
        return {**expected, "participants": tuple(resolved)}

    def _current_snapshot(
        self,
        scope: TenantScope,
        *,
        actor_ref: str,
        inbox_item_ref: str,
        draft_ref: str,
        require_outbound: bool = False,
    ) -> tuple[dict[str, object], InboxItem, Draft]:
        inbox = self._workflow.get_inbox(scope, inbox_item_ref)
        if (
            inbox is None
            or inbox.site_id != scope.site_id
            or inbox.state not in {"assigned", "draft"}
            or inbox.assignee_user_ref != actor_ref
            or inbox.conversation_ref is None
        ):
            raise EmailSendAuthorityConflict("inbox_authority_unavailable")
        mailbox = self._mailboxes.get(scope, inbox.mailbox_ref)
        if (
            mailbox is None
            or mailbox.business_purpose != scope.processing_purpose
            or mailbox.status != "active"
            or not (mailbox.outbound_enabled if require_outbound else mailbox.inbound_enabled)
            or mailbox.default_team_ref != inbox.team_ref
        ):
            raise EmailSendAuthorityConflict("mailbox_authority_unavailable")
        conversation = self._workflow.get_conversation_for(scope, inbox.inbox_item_ref)
        if (
            conversation is None
            or conversation.conversation_ref != inbox.conversation_ref
            or conversation.lifecycle_state != "open"
            or conversation.team_ref != inbox.team_ref
            or conversation.party_ref is None
            or conversation.owner_user_ref != actor_ref
            or inbox.inbox_item_ref not in conversation.inbox_item_refs
            or inbox.message_ref not in conversation.message_refs
        ):
            raise EmailSendAuthorityConflict("conversation_authority_unavailable")
        draft = self._workflow.get_draft(scope, draft_ref)
        if (
            draft is None
            or draft.state != "editable"
            or draft.inbox_item_ref != inbox.inbox_item_ref
            or draft.conversation_ref != conversation.conversation_ref
        ):
            raise EmailSendAuthorityConflict("draft_authority_unavailable")
        return (
            {
                "site_id": scope.site_id,
                "processing_purpose": scope.processing_purpose,
                "team_ref": inbox.team_ref,
                "assignee_user_name": actor_ref,
                "mailbox_ref": mailbox.mailbox_ref,
                "mailbox_config_revision": mailbox.config_revision,
                "inbox_item_ref": inbox.inbox_item_ref,
                "inbox_item_revision": inbox.revision,
                "conversation_ref": conversation.conversation_ref,
                "conversation_revision": conversation.revision,
                "party_ref": conversation.party_ref,
                "owner_user_name": actor_ref,
                "reply_draft_ref": draft.draft_ref,
                "reply_draft_revision": draft.revision,
                "reply_draft_digest": draft.content_digest,
            },
            inbox,
            draft,
        )


def _validate_snapshot(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != _SNAPSHOT_FIELDS:
        raise EmailSendAuthorityConflict("gateway_authority_drift")
    result = dict(value)
    for field in _SNAPSHOT_FIELDS - {
        "mailbox_config_revision",
        "inbox_item_revision",
        "conversation_revision",
        "reply_draft_revision",
    }:
        candidate = result[field]
        if not isinstance(candidate, str) or not candidate or candidate != candidate.strip():
            raise EmailSendAuthorityConflict("gateway_authority_drift")
    for field in {
        "mailbox_config_revision",
        "inbox_item_revision",
        "conversation_revision",
        "reply_draft_revision",
    }:
        candidate = result[field]
        if isinstance(candidate, bool) or not isinstance(candidate, int) or candidate < 1:
            raise EmailSendAuthorityConflict("gateway_authority_drift")
    if _DIGEST.fullmatch(str(result["reply_draft_digest"])) is None:
        raise EmailSendAuthorityConflict("gateway_authority_drift")
    return result


def _protected_user_ref(site_id: str, user_name: str) -> str:
    digest = hashlib.sha256(
        _USER_REF_DOMAIN + site_id.encode() + b"\x1f" + user_name.encode()
    ).digest()
    value = int.from_bytes(digest[:16], "big")
    encoded = ["0"] * 26
    for index in range(25, -1, -1):
        value, remainder = divmod(value, 32)
        encoded[index] = _CROCKFORD[remainder]
    return "USR-" + "".join(encoded)


def _validate_participant_projection(value: object) -> list[dict[str, str]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 2:
        raise EmailSendAuthorityConflict("participant_projection_invalid")
    result: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping) or set(item) != {"address_role", "opaque_address_ref"}:
            raise EmailSendAuthorityConflict("participant_projection_invalid")
        role = item["address_role"]
        opaque_ref = item["opaque_address_ref"]
        if role != ("sender" if index == 0 else "to"):
            raise EmailSendAuthorityConflict("participant_projection_invalid")
        if not isinstance(opaque_ref, str) or _OPAQUE_EMAIL.fullmatch(opaque_ref) is None:
            raise EmailSendAuthorityConflict("participant_projection_invalid")
        result.append({"address_role": role, "opaque_address_ref": opaque_ref})
    if result[0]["opaque_address_ref"] == result[1]["opaque_address_ref"]:
        raise EmailSendAuthorityConflict("participant_projection_invalid")
    return result


__all__ = ["EmailSendAuthority", "EmailSendAuthorityConflict"]
