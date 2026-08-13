from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from esan_gbos.domain.email_review_policy import (
    EMAIL_SEND_REVIEW_POLICY,
    EmailSendOwnerApproval,
    EmailSendReviewPolicyError,
    authorize_email_send_owner,
    protect_live_email_send_snapshot,
    protected_user_ref,
    validate_email_send_approval_snapshot,
)

ROOT = Path(__file__).parents[2]
EXAMPLE = ROOT / "contracts" / "email_gateway" / "examples" / "email-send-approved-command-v2.json"
NOW = datetime(2026, 8, 13, 13, 5, tzinfo=UTC)


def _command() -> dict[str, Any]:
    value = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def snapshot() -> dict[str, Any]:
    value = _command()
    return {
        "schema_version": "1.0",
        "site_id": value["site_id"],
        "processing_purpose": value["processing_purpose"],
        "team_ref": value["team_ref"],
        "assignee_user_ref": value["owner_user_ref"],
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


def live_snapshot(raw_user: str = "Owner.Name+Email@example.invalid") -> dict[str, Any]:
    value = snapshot()
    value.pop("assignee_user_ref")
    value.pop("owner_user_ref")
    value["assignee_user_name"] = raw_user
    value["owner_user_name"] = raw_user
    return value


def test_protected_user_ref_is_deterministic_site_separated_and_never_contains_raw_user() -> None:
    first = protected_user_ref("gbos.localhost", "Owner.Name+Email@example.invalid")
    same = protected_user_ref("gbos.localhost", "Owner.Name+Email@example.invalid")
    other_site = protected_user_ref("other.localhost", "Owner.Name+Email@example.invalid")

    assert first == same
    assert first != other_site
    assert first.startswith("USR-") and len(first) == 30
    assert "owner" not in first.casefold()
    assert "example" not in first.casefold()
    assert protected_user_ref(
        "gbos.localhost", "Owner.Name+Email@example.invalid"
    ) != protected_user_ref("gbos.localhost", "owner.name+email@example.invalid")


def test_protected_user_ref_has_a_fixed_domain_separated_crockford_vector() -> None:
    assert (
        protected_user_ref("gbos.localhost", "Owner.Name+Email@example.invalid")
        == "USR-5QTXPW9YKWAR9ZM64KEWD2DA92"
    )


@pytest.mark.parametrize(
    ("site_id", "user_name"),
    (("", "owner@example.invalid"), ("gbos.localhost", ""), ("bad site", "owner")),
)
def test_protected_user_ref_rejects_invalid_input_without_echoing_it(
    site_id: str,
    user_name: str,
) -> None:
    with pytest.raises(EmailSendReviewPolicyError) as caught:
        protected_user_ref(site_id, user_name)

    assert site_id not in str(caught.value) or not site_id
    assert user_name not in str(caught.value) or not user_name


def test_live_gateway_owner_is_compared_raw_then_replaced_by_only_protected_ref() -> None:
    raw_user = "Owner.Name+Email@example.invalid"

    protected = protect_live_email_send_snapshot(
        live_snapshot(raw_user),
        site_id="gbos.localhost",
        authenticated_user_name=raw_user,
    )

    user_ref = protected_user_ref("gbos.localhost", raw_user)
    assert protected["assignee_user_ref"] == protected["owner_user_ref"] == user_ref
    assert raw_user not in repr(protected)
    assert "assignee_user_name" not in protected
    assert "owner_user_name" not in protected


def test_live_gateway_owner_must_equal_authenticated_frappe_user_exactly() -> None:
    raw_user = "Owner.Name+Email@example.invalid"

    with pytest.raises(EmailSendReviewPolicyError) as caught:
        protect_live_email_send_snapshot(
            live_snapshot(raw_user),
            site_id="gbos.localhost",
            authenticated_user_name=raw_user.casefold(),
        )

    assert caught.value.reason_code == "actor_not_current_owner"


@pytest.mark.parametrize(
    "raw_user",
    (
        "Guest",
        "Administrator",
        "ai:reply-agent",
        "service:email-publication",
        "email-command-publication@localhost.invalid",
    ),
)
def test_live_gateway_owner_rejects_guest_system_ai_and_service_users_before_protection(
    raw_user: str,
) -> None:
    with pytest.raises(EmailSendReviewPolicyError) as caught:
        protect_live_email_send_snapshot(
            live_snapshot(raw_user),
            site_id="gbos.localhost",
            authenticated_user_name=raw_user,
        )

    assert caught.value.reason_code == "actor_ineligible"


def test_email_send_approval_snapshot_is_closed_opaque_and_revision_pinned() -> None:
    value = validate_email_send_approval_snapshot(snapshot())

    assert value == snapshot()
    serialized = repr(value).casefold()
    for forbidden in (
        "recipient_email",
        "sender_email",
        "subject_line",
        "body_html",
        "body_text",
        "mime_bytes",
        "provider_payload",
        "send_state",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    ("field", "changed"),
    (
        ("mailbox_config_revision", 0),
        ("reply_draft_digest", "sha256:" + "A" * 64),
        ("assignee_user_ref", ""),
        ("participants", []),
        ("evidence_refs", []),
        ("approval_expires_at", "not-a-timestamp"),
    ),
)
def test_snapshot_rejects_missing_or_invalid_authority_pins(field: str, changed: object) -> None:
    value = snapshot()
    value[field] = changed

    with pytest.raises(EmailSendReviewPolicyError, match=field):
        validate_email_send_approval_snapshot(value)


def test_snapshot_rejects_unknown_fields_even_when_they_look_useful() -> None:
    value = {**snapshot(), "recipient_email": "customer@example.invalid"}

    with pytest.raises(EmailSendReviewPolicyError, match="unexpected fields"):
        validate_email_send_approval_snapshot(value)


def test_only_exact_assigned_current_owner_can_authorize_the_pinned_case() -> None:
    pinned = snapshot()
    actor = pinned["assignee_user_ref"]

    result = authorize_email_send_owner(
        pinned,
        live_snapshot=copy.deepcopy(pinned),
        actor_user_ref=actor,
        assigned_reviewer=actor,
        case_team_ref=pinned["team_ref"],
        case_policy_version=EMAIL_SEND_REVIEW_POLICY,
        now=NOW,
    )

    assert result == EmailSendOwnerApproval(
        actor_user_ref=actor,
        team_ref=pinned["team_ref"],
        processing_purpose=pinned["processing_purpose"],
        policy_version=EMAIL_SEND_REVIEW_POLICY,
        expires_at=datetime(2026, 8, 13, 13, 15, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("argument", "changed", "reason"),
    (
        ("actor_user_ref", "USR-01ARZ3NDEKTSV4RRFFQ69G5FAX", "actor_not_assigned_owner"),
        ("assigned_reviewer", "USR-01ARZ3NDEKTSV4RRFFQ69G5FAX", "actor_not_assigned_owner"),
        ("case_team_ref", "TEM-01ARZ3NDEKTSV4RRFFQ69G5FAX", "team_mismatch"),
        ("case_policy_version", "generic_review_v1", "policy_mismatch"),
    ),
)
def test_manager_reviewer_or_admin_cannot_substitute_for_owner(
    argument: str,
    changed: str,
    reason: str,
) -> None:
    pinned = snapshot()
    actor = pinned["assignee_user_ref"]
    arguments: dict[str, Any] = {
        "live_snapshot": copy.deepcopy(pinned),
        "actor_user_ref": actor,
        "assigned_reviewer": actor,
        "case_team_ref": pinned["team_ref"],
        "case_policy_version": EMAIL_SEND_REVIEW_POLICY,
        "now": NOW,
    }
    arguments[argument] = changed

    with pytest.raises(EmailSendReviewPolicyError) as caught:
        authorize_email_send_owner(pinned, **arguments)

    assert caught.value.reason_code == reason


@pytest.mark.parametrize(
    "actor",
    (
        "Guest",
        "Administrator",
        "ai:reply-agent",
        "service:email-publication",
        "email-command-publication@localhost.invalid",
    ),
)
def test_guest_ai_system_and_service_actors_are_never_delegated_owners(actor: str) -> None:
    pinned = snapshot()
    pinned["assignee_user_ref"] = actor
    pinned["owner_user_ref"] = actor

    with pytest.raises(EmailSendReviewPolicyError) as caught:
        authorize_email_send_owner(
            pinned,
            live_snapshot=copy.deepcopy(pinned),
            actor_user_ref=actor,
            assigned_reviewer=actor,
            case_team_ref=pinned["team_ref"],
            case_policy_version=EMAIL_SEND_REVIEW_POLICY,
            now=NOW,
        )

    assert caught.value.reason_code == "actor_ineligible"


def test_expired_review_and_any_live_authority_drift_fail_closed() -> None:
    pinned = snapshot()
    actor = pinned["assignee_user_ref"]
    expired = copy.deepcopy(pinned)
    expired["approval_expires_at"] = (NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")

    with pytest.raises(EmailSendReviewPolicyError) as caught:
        authorize_email_send_owner(
            expired,
            live_snapshot=copy.deepcopy(expired),
            actor_user_ref=actor,
            assigned_reviewer=actor,
            case_team_ref=expired["team_ref"],
            case_policy_version=EMAIL_SEND_REVIEW_POLICY,
            now=NOW,
        )
    assert caught.value.reason_code == "approval_expired"

    stale = copy.deepcopy(pinned)
    stale["participants"][1]["identity_mapping_revision"] += 1
    with pytest.raises(EmailSendReviewPolicyError) as caught:
        authorize_email_send_owner(
            pinned,
            live_snapshot=stale,
            actor_user_ref=actor,
            assigned_reviewer=actor,
            case_team_ref=pinned["team_ref"],
            case_policy_version=EMAIL_SEND_REVIEW_POLICY,
            now=NOW,
        )
    assert caught.value.reason_code == "live_authority_drift"
