from __future__ import annotations

import hashlib
import importlib
import json
import sys
from collections.abc import Generator
from copy import deepcopy
from datetime import datetime
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from esan_gbos.domain.approved_command import command_payload_digest
from esan_gbos.domain.email_review_policy import protected_user_ref
from esan_gbos.domain.review_dto import canonical_payload_hash

ROLE = "Email Gateway Authority Consumer"
USER = "email-gateway-authority@localhost.invalid"
AUTH_REF = "email-gateway-authority-v1"
MAPPING = "EID-01ARZ3NDEKTSV4RRFFQ69G5FAV"
PARTY = "PTY-01ARZ3NDEKTSV4RRFFQ69G5FAV"
TEAM = "TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV"
OWNER = "owner@example.invalid"
OWNER_REF = protected_user_ref("gbos.localhost", OWNER)
PUBLICATION = "PUB-01ARZ3NDEKTSV4RRFFQ69G5FAV"
COMMAND = "CMD-01ARZ3NDEKTSV4RRFFQ69G5FAV"
CASE = "REV-01ARZ3NDEKTSV4RRFFQ69G5FAV"
CASE_REF = "RVC-01ARZ3NDEKTSV4RRFFQ69G5FAV"
APPROVAL = "ESA-01ARZ3NDEKTSV4RRFFQ69G5FAV"
REQUEST = "REQ-01ARZ3NDEKTSV4RRFFQ69G5FAV"
MAILBOX = "MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV"
INBOX = "INB-01ARZ3NDEKTSV4RRFFQ69G5FAV"
CONVERSATION = "CNV-01ARZ3NDEKTSV4RRFFQ69G5FAV"
DRAFT = "DRF-01ARZ3NDEKTSV4RRFFQ69G5FAV"
EVIDENCE = "EVR-01ARZ3NDEKTSV4RRFFQ69G5FAV"
CLIENT_REQUEST = "CLI-01ARZ3NDEKTSV4RRFFQ69G5FAV"
FENCE = "FNC-01ARZ3NDEKTSV4RRFFQ69G5FAV"
ISSUED_AT = "2026-08-13T00:00:00Z"
EXPIRES_AT = "2099-08-13T00:10:00Z"
IDEMPOTENCY_KEY = "idem:v2:" + "a" * 64
_OWNER_REVISION_PAYLOAD = {
    "schema_version": "owner-eligibility-v1",
    "party_ref": PARTY,
    "party_revision": 2,
    "team_ref": TEAM,
    "team_revision": 3,
    "owner_user_ref": OWNER,
    "owner_enabled": 1,
    "owner_user_type": "System User",
    "membership_ref": "TM-0001",
    "membership_parent": TEAM,
    "membership_user": OWNER,
    "membership_enabled": 1,
    "membership_modified": "2026-08-13T00:00:00Z",
}
OWNER_REVISION = (
    "sha256:"
    + hashlib.sha256(
        json.dumps(
            _OWNER_REVISION_PAYLOAD,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
)
_PROJECT_REQUEST_KEYS = {
    "site_id",
    "processing_purpose",
    "request_id",
    "auth_ref",
    "mapping_ref",
    "expected_mapping_revision",
    "expected_team_ref",
}


class _PermissionError(Exception):
    pass


class _DoesNotExistError(Exception):
    pass


class _Database:
    def __init__(self) -> None:
        self.mapping_rows: list[dict[str, Any]] = []
        self.route_rows: list[dict[str, Any]] = []
        self.route_rows_by_mapping: dict[str, list[dict[str, Any]]] = {}
        self.rollbacks = 0
        self.raise_on_sql: Exception | None = None
        self.queries: list[str] = []

    def sql(
        self,
        query: str,
        values: dict[str, Any],
        *,
        as_dict: bool,
    ) -> list[dict[str, Any]]:
        assert as_dict is True
        self.queries.append(query)
        if self.raise_on_sql is not None:
            raise self.raise_on_sql
        if "owner_user_ref" in query:
            rows = self.route_rows_by_mapping.get(values["mapping_ref"], self.route_rows)
        else:
            rows = self.mapping_rows
        return deepcopy(rows)

    def rollback(self) -> None:
        self.rollbacks += 1


class _Frappe(ModuleType):
    def __init__(self) -> None:
        super().__init__("frappe")
        self.PermissionError = _PermissionError
        self.DoesNotExistError = _DoesNotExistError
        self.session = SimpleNamespace(user=USER)
        self.local = SimpleNamespace(
            site="gbos.localhost",
            response={},
            request=SimpleNamespace(
                method="POST",
                headers={
                    "Authorization": "token authority-key:authority-secret",
                    "X-Site-ID": "gbos.localhost",
                    "X-Processing-Purpose": "email_gateway_authority",
                    "X-Request-ID": "gateway-authority-0001",
                    "X-GBOS-Frappe-Auth-Ref": AUTH_REF,
                },
            ),
        )
        self.conf = {
            "gbos_email_gateway_authority_identities": {
                AUTH_REF: {
                    "user": USER,
                    "site_id": "gbos.localhost",
                    "processing_purposes": ["email_gateway_authority"],
                }
            }
        }
        self.roles = {USER: {ROLE}}
        self.db = _Database()
        self.documents: dict[tuple[str, str], Any] = {}
        self.document_reads: list[tuple[str, str, bool]] = []

    def whitelist(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs

        def decorate(function: Any) -> Any:
            return function

        return decorate

    def get_roles(self, user: str | None = None) -> list[str]:
        return sorted(self.roles.get(user or self.session.user, set()))

    def parse_json(self, value: str) -> Any:
        import json

        return json.loads(value)

    def get_doc(self, doctype: str, name: str, *, for_update: bool = False) -> Any:
        self.document_reads.append((doctype, name, for_update))
        key = (doctype, name)
        if key not in self.documents:
            raise self.DoesNotExistError("sensitive-missing-document@example.invalid")
        return deepcopy(self.documents[key])


@pytest.fixture
def authority_api() -> Generator[tuple[Any, _Frappe]]:
    fake = _Frappe()
    names = (
        "frappe",
        "esan_gbos.email_gateway_authority_access",
        "esan_gbos.api.internal.email_gateway_authority",
        "esan_gbos.domain.external_identity_projection",
        "esan_gbos.permissions",
    )
    originals = {name: sys.modules.get(name) for name in names}
    sys.modules["frappe"] = fake
    for name in names[1:]:
        sys.modules.pop(name, None)
    module = importlib.import_module("esan_gbos.api.internal.email_gateway_authority")
    yield module, fake
    for name, original in originals.items():
        sys.modules.pop(name, None)
        if original is not None:
            sys.modules[name] = original


def _mapping_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "mapping_ref": MAPPING,
        "mapping_revision": 4,
        "team_ref": TEAM,
        "target_type": "Party",
        "user_ref": None,
        "party_ref": PARTY,
        "review_status": "Approved",
        "business_status": "Active",
        "target_eligible": 1,
        "resolved_at": "2026-08-13T00:00:00Z",
        "external_subject": "raw-address-must-not-escape@example.invalid",
        "contact_email": "contact-must-not-escape@example.invalid",
    }
    row.update(overrides)
    return row


def _route_row(**overrides: Any) -> dict[str, Any]:
    row = {
        **_mapping_row(),
        "party_revision": 2,
        "party_status": "Active",
        "party_review_status": "Approved",
        "team_revision": 3,
        "team_status": "Active",
        "team_review_status": "Approved",
        "owner_user_ref": OWNER,
        "owner_enabled": 1,
        "owner_user_type": "System User",
        "membership_ref": "TM-0001",
        "membership_parent": TEAM,
        "membership_user": OWNER,
        "membership_enabled": 1,
        "membership_modified": "2026-08-13T00:00:00Z",
        "owner_eligibility_revision": OWNER_REVISION,
        "document_owner": "forbidden-doc-owner@example.invalid",
        "contact_owner": "forbidden-contact@example.invalid",
        "cc_owner": "forbidden-cc@example.invalid",
        "gateway_prior_owner": "forbidden-prior@example.invalid",
        "deal_owner": "forbidden-deal@example.invalid",
    }
    row.update(overrides)
    return row


def _payload(**overrides: Any) -> dict[str, Any]:
    value = {
        "site_id": "gbos.localhost",
        "processing_purpose": "email_gateway_authority",
        "request_id": "gateway-authority-0001",
        "auth_ref": AUTH_REF,
        "mapping_ref": MAPPING,
        "expected_mapping_revision": 4,
        "expected_team_ref": TEAM,
        "expected_party_revision": 2,
        "expected_team_revision": 3,
        "expected_owner_eligibility_revision": OWNER_REVISION,
    }
    value.update(overrides)
    return value


def _command_payload(**overrides: Any) -> dict[str, Any]:
    command = {
        "schema_version": "2.0",
        "command_id": COMMAND,
        "command_type": "email.send.approved",
        "site_id": "gbos.localhost",
        "processing_purpose": "customer_service",
        "team_ref": TEAM,
        "actor_user_ref": OWNER_REF,
        "delegated_approver_user_ref": OWNER_REF,
        "review_case_ref": CASE_REF,
        "review_case_revision": 2,
        "review_policy_version": "email_send_owner_v1",
        "approval_expires_at": EXPIRES_AT,
        "mailbox_ref": MAILBOX,
        "mailbox_config_revision": 4,
        "inbox_item_ref": INBOX,
        "inbox_item_revision": 5,
        "conversation_ref": CONVERSATION,
        "conversation_revision": 6,
        "reply_draft_ref": DRAFT,
        "reply_draft_revision": 7,
        "reply_draft_digest": "sha256:" + "b" * 64,
        "participants": [
            {
                "address_role": "sender",
                "opaque_address_ref": "extid:v1:email:" + "A" * 43,
            },
            {
                "address_role": "to",
                "opaque_address_ref": "extid:v1:email:" + "B" * 43,
                "identity_mapping_ref": MAPPING,
                "identity_mapping_revision": 4,
            },
        ],
        "party_ref": PARTY,
        "party_revision": 2,
        "team_revision": 3,
        "owner_user_ref": OWNER_REF,
        "owner_eligibility_revision": OWNER_REVISION,
        "final_mime_evidence_ref": EVIDENCE,
        "final_mime_digest": "sha256:" + "c" * 64,
        "evidence_refs": [EVIDENCE],
        "request_id": REQUEST,
        "idempotency_key": IDEMPOTENCY_KEY,
        "stable_client_request_id": CLIENT_REQUEST,
        "issued_at": ISSUED_AT,
    }
    command.update(overrides)
    command["payload_sha256"] = command_payload_digest(command)
    return command


def _approval_snapshot(command: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "site_id": command["site_id"],
        "processing_purpose": command["processing_purpose"],
        "team_ref": command["team_ref"],
        "assignee_user_ref": command["delegated_approver_user_ref"],
        "approval_expires_at": command["approval_expires_at"],
        "mailbox_ref": command["mailbox_ref"],
        "mailbox_config_revision": command["mailbox_config_revision"],
        "inbox_item_ref": command["inbox_item_ref"],
        "inbox_item_revision": command["inbox_item_revision"],
        "conversation_ref": command["conversation_ref"],
        "conversation_revision": command["conversation_revision"],
        "reply_draft_ref": command["reply_draft_ref"],
        "reply_draft_revision": command["reply_draft_revision"],
        "reply_draft_digest": command["reply_draft_digest"],
        "participants": command["participants"],
        "party_ref": command["party_ref"],
        "party_revision": command["party_revision"],
        "team_revision": command["team_revision"],
        "owner_user_ref": command["owner_user_ref"],
        "owner_eligibility_revision": command["owner_eligibility_revision"],
        "final_mime_evidence_ref": command["final_mime_evidence_ref"],
        "final_mime_digest": command["final_mime_digest"],
        "evidence_refs": command["evidence_refs"],
        "stable_client_request_id": command["stable_client_request_id"],
    }


def _approval_subject_snapshot(approval: Any, *, revision: int) -> dict[str, Any]:
    return {
        "doctype": "GBOS Email Send Approval",
        "name": approval.name,
        "revision": revision,
        "site_id": approval.site_id,
        "processing_purpose": approval.processing_purpose,
        "team": approval.team,
        "assignee_user_ref": approval.assignee_user_ref,
        "approval_expires_at": approval.approval_expires_at,
        "mailbox_ref": approval.mailbox_ref,
        "mailbox_config_revision": approval.mailbox_config_revision,
        "inbox_item_ref": approval.inbox_item_ref,
        "inbox_item_revision": approval.inbox_item_revision,
        "conversation_ref": approval.conversation_ref,
        "conversation_revision": approval.conversation_revision,
        "reply_draft_ref": approval.reply_draft_ref,
        "reply_draft_revision": approval.reply_draft_revision,
        "reply_draft_digest": approval.reply_draft_digest,
        "participants": json.loads(approval.participants),
        "party_ref": approval.party_ref,
        "party_revision": approval.party_revision,
        "team_revision": approval.team_revision,
        "owner_user_ref": approval.owner_user_ref,
        "owner_eligibility_revision": approval.owner_eligibility_revision,
        "final_mime_evidence_ref": approval.final_mime_evidence_ref,
        "final_mime_digest": approval.final_mime_digest,
        "evidence_refs": json.loads(approval.evidence_refs),
        "stable_client_request_id": approval.stable_client_request_id,
        "payload_sha256": approval.payload_sha256,
    }


def _install_command_state(fake: _Frappe, command: dict[str, Any] | None = None) -> dict[str, Any]:
    current = deepcopy(command or _command_payload())
    approval_snapshot = _approval_snapshot(current)
    approval_hash = canonical_payload_hash(approval_snapshot)
    approval = SimpleNamespace(
        doctype="GBOS Email Send Approval",
        name=APPROVAL,
        site_id=current["site_id"],
        processing_purpose=current["processing_purpose"],
        team=current["team_ref"],
        assignee_user_ref=current["delegated_approver_user_ref"],
        approval_expires_at=current["approval_expires_at"],
        mailbox_ref=current["mailbox_ref"],
        mailbox_config_revision=current["mailbox_config_revision"],
        inbox_item_ref=current["inbox_item_ref"],
        inbox_item_revision=current["inbox_item_revision"],
        conversation_ref=current["conversation_ref"],
        conversation_revision=current["conversation_revision"],
        reply_draft_ref=current["reply_draft_ref"],
        reply_draft_revision=current["reply_draft_revision"],
        reply_draft_digest=current["reply_draft_digest"],
        participants=json.dumps(current["participants"], separators=(",", ":"), sort_keys=True),
        party_ref=current["party_ref"],
        party_revision=current["party_revision"],
        team_revision=current["team_revision"],
        owner_user_ref=current["owner_user_ref"],
        owner_eligibility_revision=current["owner_eligibility_revision"],
        final_mime_evidence_ref=current["final_mime_evidence_ref"],
        final_mime_digest=current["final_mime_digest"],
        evidence_refs=json.dumps(current["evidence_refs"], separators=(",", ":"), sort_keys=True),
        stable_client_request_id=current["stable_client_request_id"],
        payload_sha256=approval_hash,
        business_status="Approved",
        review_status="Approved",
        revision=2,
        last_request_id=current["request_id"],
    )
    subject_snapshot = _approval_subject_snapshot(approval, revision=1)
    case_payload = {
        "title": "Email send approval",
        "team": current["team_ref"],
        "assigned_reviewer": OWNER,
        "subject_doctype": approval.doctype,
        "subject_name": APPROVAL,
        "subject_revision": 1,
        "subject_payload_sha256": canonical_payload_hash(subject_snapshot),
        "subject_snapshot": subject_snapshot,
        "evidence_refs": current["evidence_refs"],
        "policy_version": current["review_policy_version"],
        "approval_expires_at": current["approval_expires_at"],
    }
    case = SimpleNamespace(
        doctype="GBOS Review Case",
        name=CASE,
        title=case_payload["title"],
        team=case_payload["team"],
        assigned_reviewer=OWNER,
        subject_doctype=approval.doctype,
        subject_name=APPROVAL,
        subject_revision=1,
        subject_payload_sha256=case_payload["subject_payload_sha256"],
        subject_snapshot=json.dumps(subject_snapshot, separators=(",", ":"), sort_keys=True),
        case_payload_sha256=canonical_payload_hash(case_payload),
        evidence_refs=json.dumps(current["evidence_refs"], separators=(",", ":"), sort_keys=True),
        policy_version=current["review_policy_version"],
        approval_expires_at=current["approval_expires_at"],
        business_status="Approved",
        review_status="Approved",
        revision=current["review_case_revision"],
        approved_command=COMMAND,
        command_publication=PUBLICATION,
        decided_by=OWNER,
        last_request_id=current["request_id"],
    )
    approved = SimpleNamespace(
        doctype="GBOS Approved Command",
        name=COMMAND,
        review_case=CASE,
        email_send_approval=APPROVAL,
        actor_user_ref=current["actor_user_ref"],
        policy_version=current["review_policy_version"],
        command_type=current["command_type"],
        command_payload=json.dumps(current, separators=(",", ":"), sort_keys=True),
        payload_sha256=current["payload_sha256"],
        idempotency_key=current["idempotency_key"],
        stable_client_request_id=current["stable_client_request_id"],
        issued_at=current["issued_at"],
        expires_at=current["approval_expires_at"],
    )
    publication = SimpleNamespace(
        doctype="GBOS Command Publication",
        name=PUBLICATION,
        approved_command=COMMAND,
        command_payload=approved.command_payload,
        payload_digest="sha256:" + current["payload_sha256"],
        publication_status="Claimed",
        attempt=2,
        generation=3,
        fence_token=FENCE,
        lease_expires_at=EXPIRES_AT,
    )
    fake.documents = {
        (publication.doctype, publication.name): publication,
        (approved.doctype, approved.name): approved,
        (case.doctype, case.name): case,
        (approval.doctype, approval.name): approval,
    }
    fake.db.route_rows_by_mapping[MAPPING] = [_route_row()]
    return current


def _command_authority_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "site_id": "gbos.localhost",
        "processing_purpose": "email_gateway_authority",
        "request_id": "gateway-authority-0001",
        "auth_ref": AUTH_REF,
        "publication_ref": PUBLICATION,
        "attempt": 2,
        "generation": 3,
        "fence_token": FENCE,
        "command_ref": COMMAND,
        "payload_digest": "sha256:" + _command_payload()["payload_sha256"],
    }
    payload.update(overrides)
    return payload


def test_email_send_command_authority_is_rebuilt_from_current_canonical_frappe_state(
    authority_api: tuple[Any, _Frappe],
) -> None:
    api, fake = authority_api
    command = _install_command_state(fake)

    response = api.resolve_email_send_command(_command_authority_payload())

    assert response == {
        "email_send_authority": {
            "audience": "email-command-executor",
            "granted_scopes": ["email-send-execute"],
            "site_id": "gbos.localhost",
            "processing_purpose": "customer_service",
            "team_ref": TEAM,
            "authenticated_actor_user_ref": OWNER_REF,
            "delegated_approver_user_ref": OWNER_REF,
            "review_case_ref": CASE_REF,
            "review_case_revision": 2,
            "review_policy_version": "email_send_owner_v1",
            "party_ref": PARTY,
            "party_revision": 2,
            "team_revision": 3,
            "owner_user_ref": OWNER_REF,
            "owner_eligibility_revision": OWNER_REVISION,
            "participants": command["participants"],
            "final_mime_evidence_ref": EVIDENCE,
            "final_mime_digest": "sha256:" + "c" * 64,
            "evidence_refs": [EVIDENCE],
            "request_id": REQUEST,
            "idempotency_key": IDEMPOTENCY_KEY,
            "stable_client_request_id": CLIENT_REQUEST,
            "replay_payload_sha256": command["payload_sha256"],
        }
    }
    assert fake.local.response["headers"]["Cache-Control"] == "no-store"
    assert all(for_update is True for _, _, for_update in fake.document_reads)
    assert OWNER not in repr(response)
    assert "mailbox_ref" not in repr(response)
    assert "reply_draft_ref" not in repr(response)


def test_email_send_command_rejects_canonical_command_from_another_site(
    authority_api: tuple[Any, _Frappe],
) -> None:
    api, fake = authority_api
    other_owner_ref = protected_user_ref("other.localhost", OWNER)
    command = _command_payload(
        site_id="other.localhost",
        actor_user_ref=other_owner_ref,
        delegated_approver_user_ref=other_owner_ref,
        owner_user_ref=other_owner_ref,
    )
    command = _install_command_state(fake, command)

    response = api.resolve_email_send_command(
        _command_authority_payload(payload_digest="sha256:" + command["payload_sha256"])
    )

    assert response == {"error": {"code": "email_send_authority_unavailable"}}
    assert fake.local.response["http_status_code"] == 409


def test_email_send_command_locks_and_reads_current_team_status_for_each_recipient(
    authority_api: tuple[Any, _Frappe],
) -> None:
    api, fake = authority_api
    _install_command_state(fake)

    response = api.resolve_email_send_command(_command_authority_payload())

    assert "email_send_authority" in response
    route_query = next(query for query in fake.db.queries if "owner_user_ref" in query)
    assert "team.`business_status` as `team_status`" in route_query
    assert "team.`review_status` as `team_review_status`" in route_query
    assert "for update" in route_query.casefold()


def test_email_send_command_missing_chain_document_returns_only_safe_conflict(
    authority_api: tuple[Any, _Frappe],
) -> None:
    api, fake = authority_api

    response = api.resolve_email_send_command(_command_authority_payload())

    assert response == {"error": {"code": "email_send_authority_unavailable"}}
    assert fake.local.response["http_status_code"] == 409
    assert "sensitive-missing-document" not in repr(response)
    assert fake.local.response["headers"]["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    "override",
    (
        {"publication_ref": "CMD-01ARZ3NDEKTSV4RRFFQ69G5FAV"},
        {"command_ref": "PUB-01ARZ3NDEKTSV4RRFFQ69G5FAV"},
        {"fence_token": "PUB-01ARZ3NDEKTSV4RRFFQ69G5FAV"},
    ),
)
def test_email_send_command_rejects_wrongly_prefixed_request_refs_before_db_reads(
    authority_api: tuple[Any, _Frappe],
    override: dict[str, Any],
) -> None:
    api, fake = authority_api

    response = api.resolve_email_send_command(_command_authority_payload(**override))

    assert response == {"error": {"code": "invalid_authority_request"}}
    assert fake.local.response["http_status_code"] == 422
    assert fake.document_reads == []


def test_email_send_command_corrupt_persisted_snapshot_returns_only_safe_conflict(
    authority_api: tuple[Any, _Frappe],
) -> None:
    api, fake = authority_api
    _install_command_state(fake)
    fake.documents[
        ("GBOS Review Case", CASE)
    ].subject_snapshot = '{"forbidden":"raw-content@example.invalid"'

    response = api.resolve_email_send_command(_command_authority_payload())

    assert response == {"error": {"code": "email_send_authority_unavailable"}}
    assert fake.local.response["http_status_code"] == 409
    assert "raw-content" not in repr(response)


@pytest.mark.parametrize(
    ("doctype", "field"),
    (
        ("GBOS Review Case", "last_request_id"),
        ("GBOS Email Send Approval", "last_request_id"),
    ),
)
def test_email_send_command_rejects_approval_chain_request_pin_drift(
    authority_api: tuple[Any, _Frappe],
    doctype: str,
    field: str,
) -> None:
    api, fake = authority_api
    _install_command_state(fake)
    name = CASE if doctype == "GBOS Review Case" else APPROVAL
    setattr(fake.documents[(doctype, name)], field, "REQ-00000000000000000000000000")

    response = api.resolve_email_send_command(_command_authority_payload())

    assert response == {"error": {"code": "email_send_authority_unavailable"}}
    assert fake.local.response["http_status_code"] == 409


def test_email_send_command_rejects_canonical_payload_with_different_command_identity(
    authority_api: tuple[Any, _Frappe],
) -> None:
    api, fake = authority_api
    command = _install_command_state(
        fake, _command_payload(command_id="CMD-00000000000000000000000000")
    )

    response = api.resolve_email_send_command(
        _command_authority_payload(payload_digest="sha256:" + command["payload_sha256"])
    )

    assert response == {"error": {"code": "email_send_authority_unavailable"}}
    assert fake.local.response["http_status_code"] == 409


def test_email_send_command_corrupt_persisted_approved_timestamp_is_safe_conflict(
    authority_api: tuple[Any, _Frappe],
) -> None:
    api, fake = authority_api
    _install_command_state(fake)
    fake.documents[("GBOS Approved Command", COMMAND)].expires_at = "raw-expiry@example.invalid"

    response = api.resolve_email_send_command(_command_authority_payload())

    assert response == {"error": {"code": "email_send_authority_unavailable"}}
    assert fake.local.response["http_status_code"] == 409
    assert "raw-expiry" not in repr(response)


def test_email_send_command_corrupt_current_assigned_reviewer_is_safe_conflict(
    authority_api: tuple[Any, _Frappe],
) -> None:
    api, fake = authority_api
    _install_command_state(fake)
    fake.documents[("GBOS Review Case", CASE)].assigned_reviewer = "raw\nuser@example.invalid"
    fake.documents[("GBOS Review Case", CASE)].decided_by = "raw\nuser@example.invalid"

    response = api.resolve_email_send_command(_command_authority_payload())

    assert response == {"error": {"code": "email_send_authority_unavailable"}}
    assert fake.local.response["http_status_code"] == 409
    assert "raw" not in repr(response)


def test_email_send_command_treats_persisted_naive_frappe_datetimes_as_utc(
    authority_api: tuple[Any, _Frappe],
) -> None:
    api, fake = authority_api
    _install_command_state(fake)
    approved = fake.documents[("GBOS Approved Command", COMMAND)]
    approved.issued_at = datetime(2026, 8, 13)
    approved.expires_at = datetime(2099, 8, 13, 0, 10)
    fake.documents[("GBOS Command Publication", PUBLICATION)].lease_expires_at = datetime(
        2099, 8, 13, 0, 10
    )

    response = api.resolve_email_send_command(_command_authority_payload())

    assert "email_send_authority" in response


def test_projection_returns_only_mapping_revision_status_type_and_team(
    authority_api: tuple[Any, _Frappe],
) -> None:
    api, fake = authority_api
    fake.db.mapping_rows = [_mapping_row()]

    response = api.project(
        {key: value for key, value in _payload().items() if key in _PROJECT_REQUEST_KEYS}
    )

    assert response == {
        "identity_projection": {
            "mapping_ref": MAPPING,
            "mapping_revision": 4,
            "status": "confirmed",
            "target_type": "Party",
            "team_ref": TEAM,
        }
    }
    assert fake.local.response["headers"]["Cache-Control"] == "no-store"
    assert "raw-address" not in repr(response)
    assert "contact-must-not-escape" not in repr(response)


@pytest.mark.parametrize(
    "row",
    (
        _mapping_row(target_type="Party", party_ref=None),
        _mapping_row(target_type="Party", user_ref=OWNER),
        _mapping_row(target_type="User", user_ref=None, party_ref=None),
    ),
)
def test_projection_fails_closed_for_invalid_target_shape_without_leakage(
    authority_api: tuple[Any, _Frappe],
    row: dict[str, Any],
) -> None:
    api, fake = authority_api
    fake.db.mapping_rows = [row]

    response = api.project(
        {key: value for key, value in _payload().items() if key in _PROJECT_REQUEST_KEYS}
    )

    assert response == {"error": {"code": "mapping_not_resolved"}}
    assert OWNER not in repr(response)


def test_route_returns_assigned_only_for_exact_current_eligible_owner(
    authority_api: tuple[Any, _Frappe],
) -> None:
    api, fake = authority_api
    fake.db.route_rows = [_route_row()]

    response = api.resolve_route(_payload())

    assert response["route_authority"] == {
        "route_status": "assigned",
        "party_ref": PARTY,
        "party_revision": 2,
        "team_ref": TEAM,
        "team_revision": 3,
        "owner_user_ref": OWNER,
        "owner_eligibility_revision": OWNER_REVISION,
        "resolved_at": "2026-08-13T00:00:00Z",
    }


@pytest.mark.parametrize(
    "rows",
    (
        [],
        [_route_row(), _route_row()],
        [_route_row(owner_user_ref=None)],
        [_route_row(owner_enabled=0)],
        [_route_row(owner_user_type="Website User")],
        [_route_row(membership_enabled=0)],
        [_route_row(membership_parent="TEM-CROSS")],
        [_route_row(party_status="Inactive")],
        [_route_row(party_review_status="Pending")],
        [_route_row(party_revision=None)],
        [_route_row(team_revision=None)],
        [_route_row(resolved_at="not-a-timestamp")],
        [_route_row(review_status="Pending")],
        [_route_row(target_type="User", party_ref=None, user_ref=OWNER)],
    ),
)
def test_missing_ambiguous_cross_team_disabled_or_unapproved_routes_are_unassigned(
    authority_api: tuple[Any, _Frappe],
    rows: list[dict[str, Any]],
) -> None:
    api, fake = authority_api
    fake.db.route_rows = rows

    response = api.resolve_route(_payload())

    assert response["route_authority"]["route_status"] == "unassigned"
    assert response["route_authority"]["safe_reason_code"] == "owner_unavailable"
    assert OWNER not in repr(response)


@pytest.mark.parametrize(
    "override",
    (
        {"expected_mapping_revision": 3},
        {"expected_team_ref": "TEM-CROSS"},
        {"expected_party_revision": 1},
        {"expected_team_revision": 2},
        {"expected_owner_eligibility_revision": "sha256:" + "b" * 64},
    ),
)
def test_stale_expected_revisions_fail_closed_without_owner_leakage(
    authority_api: tuple[Any, _Frappe],
    override: dict[str, Any],
) -> None:
    api, fake = authority_api
    fake.db.route_rows = [_route_row()]

    response = api.resolve_route(_payload(**override))

    assert response["route_authority"]["route_status"] == "unassigned"
    assert OWNER not in repr(response)


def test_route_never_infers_from_document_contact_cc_prior_gateway_or_deal(
    authority_api: tuple[Any, _Frappe],
) -> None:
    api, fake = authority_api
    fake.db.route_rows = [_route_row(owner_user_ref=None)]

    response = api.resolve_route(_payload())

    assert response["route_authority"]["route_status"] == "unassigned"
    for forbidden in (
        "forbidden-doc-owner",
        "forbidden-contact",
        "forbidden-cc",
        "forbidden-prior",
        "forbidden-deal",
    ):
        assert forbidden not in repr(response)


def test_exact_service_identity_and_request_scope_are_fail_closed_and_cleaned(
    authority_api: tuple[Any, _Frappe],
) -> None:
    api, fake = authority_api
    access = importlib.import_module("esan_gbos.email_gateway_authority_access")
    fake.db.raise_on_sql = RuntimeError("sensitive-user@example.invalid")

    response = api.resolve_route(_payload())

    assert response == {"error": {"code": "internal_error"}}
    assert fake.local.response["http_status_code"] == 500
    assert fake.local.response["headers"]["Cache-Control"] == "no-store"
    assert not access.email_gateway_authority_scope_active()
