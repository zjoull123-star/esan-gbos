from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from services.action_guard.email_send import EmailSendVerificationError
from services.action_guard.policy import ActionGuard
from services.email_gateway.command_authority import (
    EmailCommandAuthorityConflict,
    EmailCommandAuthorityResolver,
)
from services.email_gateway.models import TenantScope
from services.email_gateway.outbound import (
    CommandIngestService,
    CommandPublication,
    InMemoryOutboundRepository,
)
from tests.email_gateway.fakes.provider import NOW, closed_command


def _publication(command: dict[str, Any]) -> CommandPublication:
    return CommandPublication(
        publication_ref="PUB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        attempt=2,
        generation=3,
        fence_token="FNC-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        payload_digest="sha256:" + command["payload_sha256"],
    )


def _frappe_snapshot(command: dict[str, Any]) -> dict[str, object]:
    return {
        "audience": "email-command-executor",
        "granted_scopes": ["email-send-execute"],
        "site_id": command["site_id"],
        "processing_purpose": command["processing_purpose"],
        "team_ref": command["team_ref"],
        "authenticated_actor_user_ref": command["actor_user_ref"],
        "delegated_approver_user_ref": command["delegated_approver_user_ref"],
        "review_case_ref": command["review_case_ref"],
        "review_case_revision": command["review_case_revision"],
        "review_policy_version": command["review_policy_version"],
        "party_ref": command["party_ref"],
        "party_revision": command["party_revision"],
        "team_revision": command["team_revision"],
        "owner_user_ref": command["owner_user_ref"],
        "owner_eligibility_revision": command["owner_eligibility_revision"],
        "participants": deepcopy(command["participants"]),
        "final_mime_evidence_ref": command["final_mime_evidence_ref"],
        "final_mime_digest": command["final_mime_digest"],
        "evidence_refs": deepcopy(command["evidence_refs"]),
        "request_id": command["request_id"],
        "idempotency_key": command["idempotency_key"],
        "stable_client_request_id": command["stable_client_request_id"],
        "replay_payload_sha256": command["payload_sha256"],
    }


def _gateway_snapshot(command: dict[str, Any]) -> dict[str, object]:
    return {
        key: deepcopy(command[key])
        for key in (
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
        )
    }


class _Frappe:
    def __init__(self, snapshot: dict[str, object]) -> None:
        self.snapshot = snapshot
        self.calls: list[tuple[TenantScope, CommandPublication, dict[str, Any]]] = []

    def resolve(
        self,
        scope: TenantScope,
        publication: CommandPublication,
        command: dict[str, Any],
    ) -> dict[str, object]:
        self.calls.append((scope, publication, command))
        return deepcopy(self.snapshot)

    def __repr__(self) -> str:
        return "_Frappe(secret=<redacted>)"


class _Gateway:
    def __init__(self, snapshot: dict[str, object]) -> None:
        self.snapshot = snapshot

    def validate_command(self, scope: TenantScope, *, command: dict[str, Any]) -> dict[str, object]:
        del scope, command
        return deepcopy(self.snapshot)


def test_resolver_combines_fenced_frappe_and_current_gateway_authority() -> None:
    command = closed_command()
    scope = TenantScope(command["site_id"], command["processing_purpose"])
    publication = _publication(command)
    frappe = _Frappe(_frappe_snapshot(command))
    resolver = EmailCommandAuthorityResolver(
        frappe=frappe,
        gateway=_Gateway(_gateway_snapshot(command)),  # type: ignore[arg-type]
        emergency_stop_reader=lambda: False,
        external_send_reader=lambda: False,
    )

    receipt = resolver(scope, publication, command)

    assert frappe.calls == [(scope, publication, command)]
    assert receipt.mailbox_ref == command["mailbox_ref"]
    assert (
        receipt.participants[1].identity_mapping_ref
        == (command["participants"][1]["identity_mapping_ref"])
    )
    assert receipt.external_send_enabled is False
    assert receipt.emergency_stop_active is False
    assert "@" not in repr(resolver)


def test_external_send_false_is_local_and_prevents_outbox_creation() -> None:
    command = closed_command()
    scope = TenantScope(command["site_id"], command["processing_purpose"])
    repository = InMemoryOutboundRepository()
    resolver = EmailCommandAuthorityResolver(
        frappe=_Frappe(_frappe_snapshot(command)),
        gateway=_Gateway(_gateway_snapshot(command)),  # type: ignore[arg-type]
        emergency_stop_reader=lambda: False,
        external_send_reader=lambda: False,
    )
    intake = CommandIngestService(
        repository=repository,
        action_guard=ActionGuard(),
        authority_resolver=resolver,
        clock=lambda: NOW,
    )

    with pytest.raises(EmailSendVerificationError, match="external_send_disabled"):
        intake.accept(scope, publication=_publication(command), command=command)

    assert repository.command_receipt_count(scope) == 0
    assert repository.outbox_count(scope) == 0


@pytest.mark.parametrize("source", ["frappe", "gateway"])
def test_shape_or_snapshot_drift_fails_closed_without_sensitive_values(source: str) -> None:
    command = closed_command()
    frappe = _frappe_snapshot(command)
    gateway = _gateway_snapshot(command)
    if source == "frappe":
        frappe["participants"] = []
    else:
        gateway["reply_draft_digest"] = "sha256:" + "9" * 64
    resolver = EmailCommandAuthorityResolver(
        frappe=_Frappe(frappe),
        gateway=_Gateway(gateway),  # type: ignore[arg-type]
        emergency_stop_reader=lambda: False,
        external_send_reader=lambda: False,
    )

    with pytest.raises(EmailCommandAuthorityConflict) as caught:
        resolver(
            TenantScope(command["site_id"], command["processing_purpose"]),
            _publication(command),
            command,
        )

    assert "@" not in str(caught.value)
    assert command["final_mime_digest"] not in str(caught.value)
