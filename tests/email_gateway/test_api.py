from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient

from services.email_gateway.api import create_email_gateway_app
from services.email_gateway.mailboxes import MailboxRegistry
from services.email_gateway.models import Mailbox, TenantScope
from services.email_gateway.phase1_read import (
    ConnectorHealth,
    Phase1InboxItem,
    Phase1Mailbox,
)
from services.email_gateway.repositories.mailboxes import InMemoryMailboxRepository
from services.email_gateway.repositories.phase1_read import InMemoryPhase1ReadRepository
from services.email_gateway.security import GatewayAuthorizationIssuer

SITE = "alpha.example"
BFF_TOKEN = "bff-secret"
PUBLICATION_TOKEN = "publication-secret"
ADMIN_HEADERS = {
    "Authorization": f"Bearer {BFF_TOKEN}",
    "X-GBOS-Local-Auth-Ref": "email-gateway-bff-v1",
    "X-Site-ID": SITE,
    "X-Processing-Purpose": "email_mailbox_read",
    "X-Request-ID": "request-01",
}


class _Intake:
    def accept(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("publication intake must not be used by BFF routes")


class _Health:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Phase1Mailbox, ...]]] = []

    def read(
        self, site_id: str, mailboxes: tuple[Phase1Mailbox, ...]
    ) -> tuple[ConnectorHealth, ...]:
        self.calls.append((site_id, mailboxes))
        return tuple(
            ConnectorHealth(
                mailbox_ref=item.mailbox_ref,
                mailbox_label=item.display_label,
                status="healthy",
                freshness="fresh",
                backlog=0,
                last_success_at=None,
                safe_error_code=None,
            )
            for item in mailboxes
        )


class _RecordingRead(InMemoryPhase1ReadRepository):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.accesses = 0

    def list_mailboxes(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        self.accesses += 1
        return super().list_mailboxes(*args, **kwargs)

    def list_inbox(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        self.accesses += 1
        return super().list_inbox(*args, **kwargs)


def _mailbox_projection(
    ref: str = "MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
) -> Phase1Mailbox:
    return Phase1Mailbox(
        mailbox_ref=ref,
        observer_connector_instance_ref="OCI-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        display_label="Gulf Sales",
        provider_kind="fake",
        business_mode="primary",
        business_purpose="sales_follow_up",
        default_team_ref="TEM-01",
        account_owner_user_ref="owner-01",
        inbound_enabled=True,
        outbound_enabled=False,
        status="active",
        config_revision=1,
    )


def _inbox_projection(
    *, team_ref: str = "TEM-01", ref: str = "INB-01ARZ3NDEKTSV4RRFFQ69G5FAV"
) -> Phase1InboxItem:
    return Phase1InboxItem(
        inbox_item_ref=ref,
        mailbox_label="Gulf Sales",
        mailbox_role="primary",
        received_at=datetime(2026, 8, 13, 9, tzinfo=UTC),
        state="identity_pending",
        safe_summary="New enquiry",
        team_ref=team_ref,
        assignee_user_ref=None,
        identity_state="unknown",
        revision=1,
    )


def _app(
    read_repository: InMemoryPhase1ReadRepository | None = None,
    mailbox_repository: InMemoryMailboxRepository | None = None,
    health: _Health | None = None,
):
    read_repository = read_repository or InMemoryPhase1ReadRepository(
        mailboxes=(_mailbox_projection(),), inbox_items=(_inbox_projection(),)
    )
    mailbox_repository = mailbox_repository or InMemoryMailboxRepository()
    health = health or _Health()
    app = create_email_gateway_app(
        intake=_Intake(),  # type: ignore[arg-type]
        publication_bearer_token=PUBLICATION_TOKEN,
        publication_auth_ref="observer-email-publication-v1",
        bff_bearer_token=BFF_TOKEN,
        bff_auth_ref="email-gateway-bff-v1",
        mailbox_registry=MailboxRegistry(mailbox_repository),
        read_repository=read_repository,
        connector_health_reader=health,
    )
    return TestClient(app), mailbox_repository, health


def _scope_payload(*, roles: list[str], teams: list[str]) -> dict[str, object]:
    return {
        "actor_ref": "actor-01",
        "actor_roles": roles,
        "allowed_team_refs": teams,
    }


def test_bff_route_set_is_exactly_the_frozen_seventeen_operations() -> None:
    client, _, _ = _app()
    paths = {
        route.path for route in client.app.routes if route.path.startswith("/internal/v1/bff/")
    }

    assert paths == {
        "/internal/v1/bff/email-admin/mailboxes/list",
        "/internal/v1/bff/email-admin/mailboxes/get",
        "/internal/v1/bff/email-admin/rules/list",
        "/internal/v1/bff/email-admin/connector-health/get",
        "/internal/v1/bff/email-admin/mailboxes/upsert",
        "/internal/v1/bff/email-admin/mailboxes/status",
        "/internal/v1/bff/email-admin/rules/upsert",
        "/internal/v1/bff/email-inbox/list",
        "/internal/v1/bff/email-inbox/get",
        "/internal/v1/bff/email-inbox/claim",
        "/internal/v1/bff/email-inbox/reassign",
        "/internal/v1/bff/email-inbox/transition",
        "/internal/v1/bff/email-inbox/merge",
        "/internal/v1/bff/email-inbox/split",
        "/internal/v1/bff/email-inbox/link-business",
        "/internal/v1/bff/email-inbox/save-draft",
        "/internal/v1/bff/email-inbox/reveal",
    }


def test_mailbox_list_is_bff_shaped_no_store_and_keeps_multiple_primary() -> None:
    read = InMemoryPhase1ReadRepository(
        mailboxes=(
            _mailbox_projection(),
            replace(
                _mailbox_projection("MBX-01ARZ3NDEKTSV4RRFFQ69G5FAW"),
                display_label="China Sales",
            ),
        )
    )
    client, _, _ = _app(read_repository=read)
    response = client.post(
        "/internal/v1/bff/email-admin/mailboxes/list",
        headers=ADMIN_HEADERS,
        json={
            **_scope_payload(roles=["Integration Admin"], teams=[]),
            "page_size": 25,
        },
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "site_id": SITE,
        "data": {
            "mailboxes": [item.to_wire() for item in read.mailboxes],
            "next_cursor": None,
        },
    }
    assert all(item["business_mode"] == "primary" for item in response.json()["data"]["mailboxes"])
    assert not {
        "provider_account_ref",
        "observer_connector_instance_ref",
        "credential_ref",
    } & set(response.json()["data"]["mailboxes"][0])


def test_wrong_valid_auth_ref_and_wrong_role_fail_before_repository_access() -> None:
    read = _RecordingRead(mailboxes=(_mailbox_projection(),))
    client, _, _ = _app(read_repository=read)
    wrong_ref = client.post(
        "/internal/v1/bff/email-admin/mailboxes/list",
        headers={**ADMIN_HEADERS, "X-GBOS-Local-Auth-Ref": "other-valid-auth-v1"},
        json={**_scope_payload(roles=["Integration Admin"], teams=[]), "page_size": 25},
    )
    wrong_role = client.post(
        "/internal/v1/bff/email-admin/mailboxes/list",
        headers=ADMIN_HEADERS,
        json={**_scope_payload(roles=["Sales User"], teams=["TEM-01"]), "page_size": 25},
    )

    assert wrong_ref.status_code == 403
    assert wrong_role.status_code == 403
    assert read.accesses == 0


def test_inbox_denies_standalone_integration_admin_and_cross_team_before_limit() -> None:
    read = _RecordingRead(
        inbox_items=(
            _inbox_projection(team_ref="TEM-OTHER"),
            _inbox_projection(team_ref="TEM-01", ref="INB-01ARZ3NDEKTSV4RRFFQ69G5FAW"),
        )
    )
    client, _, _ = _app(read_repository=read)
    headers = {**ADMIN_HEADERS, "X-Processing-Purpose": "email_inbox_read"}
    denied = client.post(
        "/internal/v1/bff/email-inbox/list",
        headers=headers,
        json={
            **_scope_payload(roles=["Integration Admin"], teams=["TEM-01"]),
            "page_size": 1,
        },
    )
    allowed = client.post(
        "/internal/v1/bff/email-inbox/list",
        headers=headers,
        json={
            **_scope_payload(roles=["Sales User"], teams=["TEM-01"]),
            "page_size": 1,
        },
    )

    assert denied.status_code == 403
    assert [item["team_ref"] for item in allowed.json()["data"]["inbox_items"]] == ["TEM-01"]
    assert read.accesses == 1


def test_mailbox_and_inbox_get_return_closed_safe_rows() -> None:
    client, _, _ = _app()
    mailbox = client.post(
        "/internal/v1/bff/email-admin/mailboxes/get",
        headers=ADMIN_HEADERS,
        json={
            **_scope_payload(roles=["Integration Admin"], teams=[]),
            "mailbox_ref": "MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        },
    )
    inbox = client.post(
        "/internal/v1/bff/email-inbox/get",
        headers={**ADMIN_HEADERS, "X-Processing-Purpose": "email_inbox_read"},
        json={
            **_scope_payload(roles=["Sales User"], teams=["TEM-01"]),
            "inbox_item_ref": "INB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        },
    )

    assert mailbox.status_code == inbox.status_code == 200
    assert "observer_connector_instance_ref" not in mailbox.json()["data"]["mailbox"]
    assert set(inbox.json()["data"]["inbox_item"]) == {
        "inbox_item_ref",
        "mailbox_label",
        "mailbox_role",
        "received_at",
        "state",
        "safe_summary",
        "team_ref",
        "assignee_user_ref",
        "identity_state",
        "revision",
    }
    assert not {
        "raw_body",
        "participants",
        "provider",
        "message_ref",
        "evidence_refs",
    } & set(inbox.json()["data"]["inbox_item"])


def test_ceo_wildcard_reads_all_teams_but_sales_user_wildcard_is_rejected() -> None:
    read = _RecordingRead(inbox_items=(_inbox_projection(team_ref="TEM-OTHER"),))
    client, _, _ = _app(read_repository=read)
    headers = {**ADMIN_HEADERS, "X-Processing-Purpose": "email_inbox_read"}
    ceo = client.post(
        "/internal/v1/bff/email-inbox/list",
        headers=headers,
        json={**_scope_payload(roles=["CEO"], teams=["*"]), "page_size": 25},
    )
    sales = client.post(
        "/internal/v1/bff/email-inbox/list",
        headers=headers,
        json={**_scope_payload(roles=["Sales User"], teams=["*"]), "page_size": 25},
    )

    assert ceo.status_code == 200
    assert len(ceo.json()["data"]["inbox_items"]) == 1
    assert sales.status_code == 403
    assert read.accesses == 1


def test_upsert_is_closed_domain_complete_and_creates_revisioned_mailbox() -> None:
    client, repository, _ = _app()
    payload = {
        **_scope_payload(roles=["GBOS Admin"], teams=["*"]),
        "display_label": "Gulf Sales",
        "provider_kind": "fake",
        "business_mode": "primary",
        "business_purpose": "sales_follow_up",
        "provider_account_ref": "provider-account-01",
        "observer_connector_instance_ref": "OCI-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "default_team_ref": "TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "account_owner_user_ref": "owner-01",
        "priority": 10,
        "credential_ref": "secretref:v1/email/fake",
        "inbound_enabled": False,
        "outbound_enabled": False,
        "expected_revision": 0,
        "idempotency_key": "create-mailbox-01",
    }
    headers = {
        **ADMIN_HEADERS,
        "X-Processing-Purpose": "email_mailbox_admin",
        "Idempotency-Key": "create-mailbox-01",
    }

    first = client.post(
        "/internal/v1/bff/email-admin/mailboxes/upsert", headers=headers, json=payload
    )
    second = client.post(
        "/internal/v1/bff/email-admin/mailboxes/upsert", headers=headers, json=payload
    )

    assert first.status_code == 200
    assert second.json() == first.json()
    mailbox = first.json()["data"]["mailbox"]
    assert mailbox["config_revision"] == 1
    assert mailbox["outbound_enabled"] is False
    assert set(mailbox) == {
        "mailbox_ref",
        "display_label",
        "provider_kind",
        "business_mode",
        "business_purpose",
        "default_team_ref",
        "account_owner_user_ref",
        "inbound_enabled",
        "outbound_enabled",
        "status",
        "config_revision",
    }
    scope = TenantScope(SITE, "sales_follow_up")
    assert repository.get(scope, mailbox["mailbox_ref"]) is not None


def test_status_preserves_domain_fields_and_forces_outbound_false() -> None:
    repository = InMemoryMailboxRepository()
    registry = MailboxRegistry(repository)
    scope = TenantScope(SITE, "sales_follow_up")
    mailbox = Mailbox(
        mailbox_ref="MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        site_id=SITE,
        address_display="Gulf Sales",
        provider="fake",
        provider_account_ref="provider-account-01",
        observer_connector_instance_ref="OCI-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        entry_role="primary",
        business_purpose="sales_follow_up",
        default_team_ref="TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        account_owner_user_ref="owner-01",
        priority=10,
        inbound_enabled=True,
        outbound_enabled=False,
        credential_ref="secretref:v1/email/fake",
        status="active",
        config_revision=1,
        observer_config_projection_receipt=None,
    )
    durable = registry.upsert(
        scope,
        mailbox,
        expected_revision=0,
        actor_ref="seed",
        request_id="seed",
        idempotency_key="seed",
    ).mailbox
    read = InMemoryPhase1ReadRepository(mailboxes=(_mailbox_projection(),))
    client, _, _ = _app(read_repository=read, mailbox_repository=repository)
    response = client.post(
        "/internal/v1/bff/email-admin/mailboxes/status",
        headers={
            **ADMIN_HEADERS,
            "X-Processing-Purpose": "email_mailbox_admin",
            "Idempotency-Key": "pause-mailbox-01",
        },
        json={
            **_scope_payload(roles=["Integration Admin"], teams=[]),
            "mailbox_ref": durable.mailbox_ref,
            "action": "pause",
            "expected_revision": 1,
            "idempotency_key": "pause-mailbox-01",
        },
    )
    replay = client.post(
        "/internal/v1/bff/email-admin/mailboxes/status",
        headers={
            **ADMIN_HEADERS,
            "X-Processing-Purpose": "email_mailbox_admin",
            "Idempotency-Key": "pause-mailbox-01",
        },
        json={
            **_scope_payload(roles=["Integration Admin"], teams=[]),
            "mailbox_ref": durable.mailbox_ref,
            "action": "pause",
            "expected_revision": 1,
            "idempotency_key": "pause-mailbox-01",
        },
    )

    assert response.status_code == 200
    assert replay.json() == response.json()
    changed = repository.get(scope, mailbox.mailbox_ref)
    assert changed is not None
    assert (changed.status, changed.inbound_enabled, changed.outbound_enabled) == (
        "paused",
        False,
        False,
    )
    assert changed.provider_account_ref == mailbox.provider_account_ref
    assert changed.config_revision == 2


def test_existing_mailbox_upsert_replays_with_original_expected_revision() -> None:
    repository = InMemoryMailboxRepository()
    scope = TenantScope(SITE, "sales_follow_up")
    existing = Mailbox(
        mailbox_ref="MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        site_id=SITE,
        address_display="Gulf Sales",
        provider="fake",
        provider_account_ref="provider-account-01",
        observer_connector_instance_ref="OCI-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        entry_role="primary",
        business_purpose="sales_follow_up",
        default_team_ref="TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        account_owner_user_ref="owner-01",
        priority=10,
        inbound_enabled=True,
        outbound_enabled=False,
        credential_ref="secretref:v1/email/fake",
        status="active",
        config_revision=1,
        observer_config_projection_receipt=None,
    )
    MailboxRegistry(repository).upsert(
        scope,
        existing,
        expected_revision=0,
        actor_ref="seed",
        request_id="seed",
        idempotency_key="seed",
    )
    client, _, _ = _app(mailbox_repository=repository)
    payload = {
        **_scope_payload(roles=["GBOS Admin"], teams=["*"]),
        "mailbox_ref": existing.mailbox_ref,
        "display_label": "Gulf Sales Updated",
        "provider_kind": "fake",
        "business_mode": "primary",
        "business_purpose": "sales_follow_up",
        "provider_account_ref": "provider-account-01",
        "observer_connector_instance_ref": "OCI-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "default_team_ref": "TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "account_owner_user_ref": "owner-01",
        "priority": 10,
        "credential_ref": "secretref:v1/email/fake",
        "inbound_enabled": True,
        "outbound_enabled": False,
        "expected_revision": 1,
        "idempotency_key": "update-mailbox-01",
    }
    headers = {
        **ADMIN_HEADERS,
        "X-Processing-Purpose": "email_mailbox_admin",
        "Idempotency-Key": "update-mailbox-01",
    }

    first = client.post(
        "/internal/v1/bff/email-admin/mailboxes/upsert", headers=headers, json=payload
    )
    replay = client.post(
        "/internal/v1/bff/email-admin/mailboxes/upsert", headers=headers, json=payload
    )

    assert first.status_code == 200
    assert replay.json() == first.json()
    assert first.json()["data"]["mailbox"]["config_revision"] == 2


def test_bounds_closed_body_and_health_injection() -> None:
    health = _Health()
    client, _, _ = _app(health=health)
    too_large = client.post(
        "/internal/v1/bff/email-admin/mailboxes/list",
        headers=ADMIN_HEADERS,
        json={
            **_scope_payload(roles=["Integration Admin"], teams=[]),
            "page_size": 51,
        },
    )
    extra = client.post(
        "/internal/v1/bff/email-admin/mailboxes/list",
        headers=ADMIN_HEADERS,
        json={
            **_scope_payload(roles=["Integration Admin"], teams=[]),
            "page_size": 25,
            "fields": ["credential_ref"],
        },
    )
    health_response = client.post(
        "/internal/v1/bff/email-admin/connector-health/get",
        headers={**ADMIN_HEADERS, "X-Processing-Purpose": "email_connector_health_read"},
        json=_scope_payload(roles=["Integration Admin"], teams=[]),
    )

    assert too_large.status_code == 400
    assert extra.status_code == 400
    assert health_response.status_code == 200
    assert health.calls and health.calls[0][0] == SITE
    assert health.calls[0][1][0].observer_connector_instance_ref.startswith("OCI-")
    assert health_response.json()["data"]["connector_health"][0]["mailbox_label"] == "Gulf Sales"


def test_gateway_issues_fresh_closed_draft_and_evidence_receipts_without_sensitive_repr() -> None:
    now = datetime(2026, 8, 13, 10, tzinfo=UTC)
    issuer = GatewayAuthorizationIssuer(clock=lambda: now)
    actor = _scope_payload(roles=["Reviewer"], teams=["TEM-01"])

    draft = issuer.issue_draft(
        site_id=SITE,
        actor_ref=str(actor["actor_ref"]),
        team_ref="TEM-01",
        inbox_item_ref="INB-01",
        draft_ref="DRF-01",
        draft_revision=1,
        request_digest="sha256:" + "a" * 64,
    )
    reveal = issuer.issue_evidence(
        site_id=SITE,
        actor_ref=str(actor["actor_ref"]),
        team_ref="TEM-01",
        inbox_item_ref="INB-01",
        evidence_ref="EVR-01",
    )

    assert set(draft) == {
        "receipt_ref",
        "site_id",
        "purpose",
        "inbox_item_ref",
        "draft_ref",
        "draft_revision",
        "actor_ref",
        "team_ref",
        "request_digest",
        "issued_at",
        "expires_at",
    }
    assert set(reveal) == {
        "receipt_ref",
        "site_id",
        "purpose",
        "inbox_item_ref",
        "evidence_ref",
        "actor_ref",
        "team_ref",
        "issued_at",
        "expires_at",
    }
    assert draft["expires_at"] == "2026-08-13T10:05:00Z"
    assert "@" not in repr(issuer)
