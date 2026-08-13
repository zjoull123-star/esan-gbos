from __future__ import annotations

import hashlib
import json
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from services.action_guard.email_send import (
    EMAIL_COMMAND_EXECUTOR_AUDIENCE,
    EMAIL_SEND_EXECUTE_SCOPE,
    EmailParticipantBinding,
    EmailSendAuthorityReceipt,
)
from services.email_gateway.provider import (
    ProviderSubmission,
    ProviderSubmissionResult,
    ProviderSubmissionUncertain,
)

ROOT = Path(__file__).parents[3]
NOW = datetime(2026, 8, 13, 13, 5, tzinfo=UTC)


def closed_command() -> dict[str, Any]:
    value = json.loads(
        (
            ROOT
            / "contracts"
            / "email_gateway"
            / "examples"
            / "email-send-approved-command-v2.json"
        ).read_text(encoding="utf-8")
    )
    material = {key: item for key, item in value.items() if key != "payload_sha256"}
    value["payload_sha256"] = hashlib.sha256(
        json.dumps(material, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    return value


def authority_for(
    command: dict[str, Any],
    *,
    external_send_enabled: bool = True,
    emergency_stop_active: bool = False,
) -> EmailSendAuthorityReceipt:
    return EmailSendAuthorityReceipt(
        audience=EMAIL_COMMAND_EXECUTOR_AUDIENCE,
        granted_scopes=(EMAIL_SEND_EXECUTE_SCOPE,),
        site_id=command["site_id"],
        processing_purpose=command["processing_purpose"],
        team_ref=command["team_ref"],
        authenticated_actor_user_ref=command["actor_user_ref"],
        delegated_approver_user_ref=command["delegated_approver_user_ref"],
        review_case_ref=command["review_case_ref"],
        review_case_revision=command["review_case_revision"],
        review_policy_version=command["review_policy_version"],
        mailbox_ref=command["mailbox_ref"],
        mailbox_config_revision=command["mailbox_config_revision"],
        inbox_item_ref=command["inbox_item_ref"],
        inbox_item_revision=command["inbox_item_revision"],
        conversation_ref=command["conversation_ref"],
        conversation_revision=command["conversation_revision"],
        reply_draft_ref=command["reply_draft_ref"],
        reply_draft_revision=command["reply_draft_revision"],
        reply_draft_digest=command["reply_draft_digest"],
        participants=tuple(
            EmailParticipantBinding(
                address_role=item["address_role"],
                opaque_address_ref=item["opaque_address_ref"],
                identity_mapping_ref=item.get("identity_mapping_ref"),
                identity_mapping_revision=item.get("identity_mapping_revision"),
            )
            for item in command["participants"]
        ),
        party_ref=command["party_ref"],
        party_revision=command["party_revision"],
        team_revision=command["team_revision"],
        owner_user_ref=command["owner_user_ref"],
        owner_eligibility_revision=command["owner_eligibility_revision"],
        final_mime_evidence_ref=command["final_mime_evidence_ref"],
        final_mime_digest=command["final_mime_digest"],
        evidence_refs=tuple(command["evidence_refs"]),
        request_id=command["request_id"],
        idempotency_key=command["idempotency_key"],
        stable_client_request_id=command["stable_client_request_id"],
        replay_payload_sha256=None,
        emergency_stop_active=emergency_stop_active,
        external_send_enabled=external_send_enabled,
    )


class FakeEmailProvider:
    """Deterministic injected provider; it never performs I/O."""

    def __init__(self, *results: ProviderSubmissionResult | BaseException) -> None:
        self._results = deque(results)
        self.submissions: list[ProviderSubmission] = []
        self.lookups: list[str] = []

    def submit(self, submission: ProviderSubmission) -> ProviderSubmissionResult:
        self.submissions.append(submission)
        result = self._results.popleft()
        if isinstance(result, BaseException):
            raise result
        return result

    def lookup(self, stable_provider_request_id: str) -> ProviderSubmissionResult:
        self.lookups.append(stable_provider_request_id)
        result = self._results.popleft()
        if isinstance(result, BaseException):
            raise result
        if not isinstance(result, ProviderSubmissionResult):
            raise ProviderSubmissionUncertain("provider lookup is uncertain")
        return result
