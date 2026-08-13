from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient

from services.email_gateway.api import MailboxSlaPolicy, create_email_gateway_app
from services.email_gateway.mailboxes import MailboxRegistry
from services.email_gateway.models import (
    InboxItem,
    Mailbox,
    TenantScope,
    canonical_digest,
    stable_ref,
)
from services.email_gateway.phase1_read import (
    ConnectorHealth,
    Phase1InboxItem,
    Phase1Mailbox,
)
from services.email_gateway.repositories.mailboxes import InMemoryMailboxRepository
from services.email_gateway.repositories.phase1_read import InMemoryPhase1ReadRepository
from services.email_gateway.repositories.workflow import InMemoryWorkflowRepository
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


class _ParticipantAuthority:
    def __init__(self, binding: dict[str, object]) -> None:
        self.binding = binding
        self.calls: list[tuple[TenantScope, str]] = []

    def load_participant_authority_binding(
        self, scope: TenantScope, *, inbox_item_ref: str
    ) -> dict[str, object] | None:
        self.calls.append((scope, inbox_item_ref))
        return dict(self.binding) if inbox_item_ref == self.binding["inbox_item_ref"] else None


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
    email_send_authority: object | None = None,
    workflow_authority: object | None = None,
    admin_repository: object | None = None,
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
        email_send_authority=email_send_authority,  # type: ignore[arg-type]
        workflow_authority=workflow_authority,  # type: ignore[arg-type]
        admin_repository=admin_repository,  # type: ignore[arg-type]
    )
    return TestClient(app), mailbox_repository, health


def _scope_payload(*, roles: list[str], teams: list[str]) -> dict[str, object]:
    return {
        "actor_ref": "actor-01",
        "actor_roles": roles,
        "allowed_team_refs": teams,
    }


def test_bff_route_set_is_exactly_the_frozen_twenty_internal_operations() -> None:
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
        "/internal/v1/bff/email-admin/sla-policies/list",
        "/internal/v1/bff/email-admin/sla-policies/upsert",
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
        "/internal/v1/bff/email-send/authority",
    }


class _SlaAdminRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.enable_blocker: str | None = None
        self.policy = MailboxSlaPolicy(
            mailbox_ref="MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            policy_ref=stable_ref("SLA", SITE, "MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV"),
            revision=1,
            first_response_duration_seconds=3600,
            effective_at=datetime(2026, 8, 14, 1, 30, tzinfo=UTC),
        )

    def list_rules(self, _site_id: str) -> tuple[object, ...]:
        return ()

    def upsert_rule(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("routing rules are outside this test")

    def list_sla_policies(
        self,
        site_id: str,
        mailbox_ref: str,
        *,
        page_size: int,
        cursor: str | None,
    ) -> tuple[tuple[MailboxSlaPolicy, ...], str | None]:
        self.calls.append(
            ("list", (site_id, mailbox_ref), {"page_size": page_size, "cursor": cursor})
        )
        return (self.policy,), "opaque-next"

    def upsert_sla_policy(self, *args: object, **kwargs: object) -> MailboxSlaPolicy:
        self.calls.append(("upsert", args, dict(kwargs)))
        return self.policy

    def mailbox_enable_blocker(
        self, site_id: str, mailbox_ref: str, *, activation_at: datetime
    ) -> str | None:
        self.calls.append(("enable", (site_id, mailbox_ref), {"activation_at": activation_at}))
        return self.enable_blocker


def test_sla_policy_list_is_closed_scoped_bounded_and_revision_descending() -> None:
    repository = _SlaAdminRepository()
    client, _, _ = _app(admin_repository=repository)

    response = client.post(
        "/internal/v1/bff/email-admin/sla-policies/list",
        headers={**ADMIN_HEADERS, "X-Processing-Purpose": "email_admin_read"},
        json={
            **_scope_payload(roles=["Integration Admin"], teams=[]),
            "mailbox_ref": "MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "cursor": "opaque-current",
            "page_size": 100,
        },
    )
    extra = client.post(
        "/internal/v1/bff/email-admin/sla-policies/list",
        headers={**ADMIN_HEADERS, "X-Processing-Purpose": "email_admin_read"},
        json={
            **_scope_payload(roles=["Integration Admin"], teams=[]),
            "mailbox_ref": "MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "page_size": 25,
            "policy_ref": repository.policy.policy_ref,
        },
    )
    denied = client.post(
        "/internal/v1/bff/email-admin/sla-policies/list",
        headers={**ADMIN_HEADERS, "X-Processing-Purpose": "email_admin_read"},
        json={
            **_scope_payload(roles=["Sales User"], teams=["TEM-01"]),
            "mailbox_ref": "MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "page_size": 1,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "site_id": SITE,
        "data": {
            "mailbox_ref": "MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "sla_policies": [
                {
                    "policy_ref": repository.policy.policy_ref,
                    "revision": 1,
                    "first_response_duration_seconds": 3600,
                    "effective_at": "2026-08-14T01:30:00Z",
                }
            ],
            "next_cursor": "opaque-next",
        },
    }
    assert denied.status_code == 403
    assert extra.status_code == 400
    assert repository.calls == [
        (
            "list",
            (SITE, "MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV"),
            {"page_size": 100, "cursor": "opaque-current"},
        )
    ]


def test_sla_policy_upsert_generates_stable_ref_and_passes_normalized_command() -> None:
    repository = _SlaAdminRepository()
    client, _, _ = _app(admin_repository=repository)
    headers = {
        **ADMIN_HEADERS,
        "X-Processing-Purpose": "email_admin_command",
        "Idempotency-Key": "sla-policy-upsert-01",
    }

    response = client.post(
        "/internal/v1/bff/email-admin/sla-policies/upsert",
        headers=headers,
        json={
            **_scope_payload(roles=["GBOS Admin"], teams=["*"]),
            "mailbox_ref": "MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "first_response_duration_seconds": 3600,
            "effective_at": "2026-08-14T01:30:00Z",
            "expected_revision": 0,
            "idempotency_key": "sla-policy-upsert-01",
        },
    )
    caller_ref = client.post(
        "/internal/v1/bff/email-admin/sla-policies/upsert",
        headers=headers,
        json={
            **_scope_payload(roles=["GBOS Admin"], teams=["*"]),
            "mailbox_ref": "MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "policy_ref": "SLA-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "first_response_duration_seconds": 3600,
            "effective_at": "2026-08-14T01:30:00Z",
            "expected_revision": 0,
            "idempotency_key": "sla-policy-upsert-01",
        },
    )

    assert response.status_code == 200
    assert response.json()["data"]["sla_policy"] == {
        "mailbox_ref": repository.policy.mailbox_ref,
        "policy_ref": repository.policy.policy_ref,
        "revision": repository.policy.revision,
        "first_response_duration_seconds": (repository.policy.first_response_duration_seconds),
        "effective_at": "2026-08-14T01:30:00Z",
    }
    assert caller_ref.status_code == 400
    operation, args, kwargs = repository.calls[0]
    assert operation == "upsert"
    assert args == (TenantScope(SITE, "business_operations"),)
    assert kwargs == {
        "mailbox_ref": "MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "policy_ref": stable_ref("SLA", SITE, "MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV"),
        "first_response_duration_seconds": 3600,
        "effective_at": datetime(2026, 8, 14, 1, 30, tzinfo=UTC),
        "effective_at_wire": "2026-08-14T01:30:00Z",
        "expected_revision": 0,
        "request_id": "request-01",
        "idempotency_key": "sla-policy-upsert-01",
    }


def test_mailbox_enable_fails_closed_for_missing_policy_and_legacy_sla_clocks() -> None:
    mailbox_repository = InMemoryMailboxRepository()
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
        inbound_enabled=False,
        outbound_enabled=False,
        credential_ref="secretref:v1/email/fake",
        status="paused",
        config_revision=1,
        observer_config_projection_receipt=None,
        mailbox_address_identity_ref="extid:v1:email:" + "M" * 43,
    )
    durable = (
        MailboxRegistry(mailbox_repository)
        .upsert(
            scope,
            mailbox,
            expected_revision=0,
            actor_ref="seed",
            request_id="seed",
            idempotency_key="seed-sla-enable",
        )
        .mailbox
    )
    admin = _SlaAdminRepository()
    client, _, _ = _app(
        mailbox_repository=mailbox_repository,
        admin_repository=admin,
        read_repository=InMemoryPhase1ReadRepository(mailboxes=(_mailbox_projection(),)),
    )
    payload = {
        **_scope_payload(roles=["Integration Admin"], teams=[]),
        "mailbox_ref": durable.mailbox_ref,
        "action": "enable",
        "expected_revision": 1,
        "idempotency_key": "enable-sla-mailbox-01",
    }
    headers = {
        **ADMIN_HEADERS,
        "X-Processing-Purpose": "email_mailbox_admin",
        "Idempotency-Key": "enable-sla-mailbox-01",
    }

    admin.enable_blocker = "sla_policy_required"
    missing = client.post(
        "/internal/v1/bff/email-admin/mailboxes/status", headers=headers, json=payload
    )
    admin.enable_blocker = "sla_backfill_required"
    legacy = client.post(
        "/internal/v1/bff/email-admin/mailboxes/status", headers=headers, json=payload
    )

    assert missing.status_code == legacy.status_code == 409
    assert missing.json() == {"error": {"code": "sla_policy_required"}}
    assert legacy.json() == {"error": {"code": "sla_backfill_required"}}
    assert mailbox_repository.get(scope, durable.mailbox_ref) == durable


def test_email_send_authority_route_is_server_scoped_and_closed() -> None:
    class Authority:
        def __init__(self) -> None:
            self.calls: list[tuple[str, TenantScope, dict[str, object]]] = []

        def authorize(self, scope: TenantScope, **values: object) -> dict[str, object]:
            self.calls.append(("authorize", scope, values))
            return {
                "gateway_snapshot": {"inbox_item_ref": values["inbox_item_ref"]},
                "draft_authorization": {"receipt_ref": "DAR-opaque"},
                "draft_evidence_ref": "obs:v1:opaque",
            }

        def validate(self, scope: TenantScope, **values: object) -> dict[str, object]:
            self.calls.append(("validate", scope, values))
            return {
                "gateway_snapshot": values["expected_gateway_snapshot"],
                "participants": values["participant_projection"],
            }

    authority = Authority()

    class ScopeAuthority:
        def authorize_inbox(self, _actor: object, _inbox_item_ref: str) -> tuple[TenantScope, str]:
            return TenantScope(SITE, "sales_follow_up"), "TEM-01"

    client, _, _ = _app(
        email_send_authority=authority,
        workflow_authority=ScopeAuthority(),
    )
    headers = {**ADMIN_HEADERS, "X-Processing-Purpose": "email_inbox_command"}
    actor = _scope_payload(roles=["Sales User"], teams=["TEM-01"])
    authorized = client.post(
        "/internal/v1/bff/email-send/authority",
        headers=headers,
        json={
            **actor,
            "phase": "authorize",
            "inbox_item_ref": "INB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "draft_ref": "DRF-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "expected_inbox_revision": 4,
            "expected_draft_revision": 3,
            "participant_roles_digest": "sha256:" + "a" * 64,
        },
    )
    assert authorized.status_code == 200
    snapshot = authorized.json()["data"]["send_authority"]["gateway_snapshot"]
    validated = client.post(
        "/internal/v1/bff/email-send/authority",
        headers=headers,
        json={
            **actor,
            "phase": "validate",
            "expected_gateway_snapshot": snapshot,
            "participant_projection": [
                {"address_role": "sender", "opaque_address_ref": "extid:v1:email:" + "a" * 43},
                {"address_role": "to", "opaque_address_ref": "extid:v1:email:" + "b" * 43},
            ],
        },
    )
    assert validated.status_code == 200
    assert [call[0] for call in authority.calls] == ["authorize", "validate"]
    assert authority.calls[0][1] == TenantScope(SITE, "sales_follow_up")
    assert authorized.headers["cache-control"] == validated.headers["cache-control"] == "no-store"


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
        "mailbox_address_identity_ref": "extid:v1:email:" + "M" * 43,
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
    assert "mailbox_address_identity_ref" not in mailbox
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


def test_upsert_requires_opaque_mailbox_identity_ref_and_never_accepts_raw_address() -> None:
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
        "idempotency_key": "missing-mailbox-identity-01",
    }
    headers = {
        **ADMIN_HEADERS,
        "X-Processing-Purpose": "email_mailbox_admin",
        "Idempotency-Key": "missing-mailbox-identity-01",
    }

    missing = client.post(
        "/internal/v1/bff/email-admin/mailboxes/upsert", headers=headers, json=payload
    )
    raw = client.post(
        "/internal/v1/bff/email-admin/mailboxes/upsert",
        headers=headers,
        json={**payload, "canonical_mailbox_address": "sales@example.invalid"},
    )
    invalid = client.post(
        "/internal/v1/bff/email-admin/mailboxes/upsert",
        headers=headers,
        json={**payload, "mailbox_address_identity_ref": "sales@example.invalid"},
    )

    assert missing.status_code == raw.status_code == invalid.status_code == 400
    assert repository.list(TenantScope(SITE, "sales_follow_up")) == ()


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


def test_status_enable_fails_closed_for_legacy_mailbox_without_identity_ref() -> None:
    repository = InMemoryMailboxRepository()
    scope = TenantScope(SITE, "sales_follow_up")
    legacy = Mailbox(
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
        inbound_enabled=False,
        outbound_enabled=False,
        credential_ref="secretref:v1/email/fake",
        status="paused",
        config_revision=1,
        observer_config_projection_receipt=None,
        mailbox_address_identity_ref=None,
    )
    durable = (
        MailboxRegistry(repository)
        .upsert(
            scope,
            legacy,
            expected_revision=0,
            actor_ref="seed",
            request_id="seed",
            idempotency_key="seed-legacy",
        )
        .mailbox
    )
    client, _, _ = _app(
        read_repository=InMemoryPhase1ReadRepository(mailboxes=(_mailbox_projection(),)),
        mailbox_repository=repository,
    )

    response = client.post(
        "/internal/v1/bff/email-admin/mailboxes/status",
        headers={
            **ADMIN_HEADERS,
            "X-Processing-Purpose": "email_mailbox_admin",
            "Idempotency-Key": "enable-legacy-mailbox-01",
        },
        json={
            **_scope_payload(roles=["Integration Admin"], teams=[]),
            "mailbox_ref": durable.mailbox_ref,
            "action": "enable",
            "expected_revision": 1,
            "idempotency_key": "enable-legacy-mailbox-01",
        },
    )

    assert response.status_code == 409
    assert response.json() == {"error": {"code": "mailbox_identity_required"}}
    assert repository.get(scope, durable.mailbox_ref) == durable


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
        "mailbox_address_identity_ref": "extid:v1:email:" + "M" * 43,
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
        inbox_item_ref="INB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        draft_ref="DRF-01",
        draft_revision=1,
        request_digest="sha256:" + "a" * 64,
        participant_authority_binding={
            "gateway_receipt_ref": "EGR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "publication_ref": "PUB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "inbox_item_ref": "INB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "message_ref": "MSG-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "mailbox_ref": "MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "mailbox_config_revision": 1,
            "observer_delivery_ref": "DLV-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "payload_digest": "sha256:" + "b" * 64,
            "participant_binding_digest": "sha256:" + "c" * 64,
            "evidence_binding_digest": "sha256:" + "d" * 64,
        },
        participant_roles_digest=canonical_digest(
            {"sender": "mailbox_owner", "recipients": ["original_sender"]}
        ),
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
        "gateway_receipt_ref",
        "publication_ref",
        "message_ref",
        "mailbox_ref",
        "mailbox_config_revision",
        "observer_delivery_ref",
        "payload_digest",
        "participant_binding_digest",
        "evidence_binding_digest",
        "participant_roles_digest",
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


def test_draft_authorization_loads_durable_binding_only_after_actor_authorization() -> None:
    now = datetime(2026, 8, 13, 10, tzinfo=UTC)
    scope = TenantScope(SITE, "business_operations")
    workflow = InMemoryWorkflowRepository()
    inbox = InboxItem.new(
        site_id=SITE,
        mailbox_ref="MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        message_ref="MSG-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        team_ref="TEM-01",
        received_at=now,
    )
    workflow.save_inbox(scope, inbox)
    authority = _ParticipantAuthority(
        {
            "gateway_receipt_ref": "EGR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "publication_ref": "PUB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "inbox_item_ref": inbox.inbox_item_ref,
            "message_ref": inbox.message_ref,
            "mailbox_ref": inbox.mailbox_ref,
            "mailbox_config_revision": 1,
            "observer_delivery_ref": "DLV-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "payload_digest": "sha256:" + "b" * 64,
            "participant_binding_digest": "sha256:" + "c" * 64,
            "evidence_binding_digest": "sha256:" + "d" * 64,
        }
    )
    app = create_email_gateway_app(
        intake=_Intake(),  # type: ignore[arg-type]
        participant_authority_reader=authority,
        publication_bearer_token=PUBLICATION_TOKEN,
        publication_auth_ref="observer-email-publication-v1",
        bff_bearer_token=BFF_TOKEN,
        bff_auth_ref="email-gateway-bff-v1",
        mailbox_registry=MailboxRegistry(InMemoryMailboxRepository()),
        read_repository=InMemoryPhase1ReadRepository(),
        connector_health_reader=_Health(),
        workflow_repository=workflow,
        clock=lambda: now,
    )
    roles_digest = canonical_digest({"sender": "mailbox_owner", "recipients": ["original_sender"]})
    payload = {
        **_scope_payload(roles=["Reviewer"], teams=["TEM-01"]),
        "phase": "authorize",
        "inbox_item_ref": inbox.inbox_item_ref,
        "draft_ref": "DRF-01",
        "expected_revision": 0,
        "content_digest": "sha256:" + "a" * 64,
        "participant_roles_digest": roles_digest,
        "idempotency_key": "draft-authorize-01",
    }
    headers = {
        "Authorization": f"Bearer {BFF_TOKEN}",
        "X-GBOS-Local-Auth-Ref": "email-gateway-bff-v1",
        "X-Site-ID": SITE,
        "X-Processing-Purpose": "email_inbox_command",
        "X-Request-ID": "draft-authorize-request-01",
        "Idempotency-Key": "draft-authorize-01",
    }

    response = TestClient(app).post(
        "/internal/v1/bff/email-inbox/save-draft",
        headers=headers,
        json=payload,
    )

    assert response.status_code == 200
    receipt = response.json()["data"]["draft_authorization"]
    canonical_draft_ref = stable_ref("DRF", SITE, inbox.inbox_item_ref)
    assert receipt["draft_ref"] == canonical_draft_ref
    assert receipt["participant_roles_digest"] == roles_digest
    assert receipt["gateway_receipt_ref"] == authority.binding["gateway_receipt_ref"]
    assert authority.calls == [(scope, inbox.inbox_item_ref)]

    committed = TestClient(app).post(
        "/internal/v1/bff/email-inbox/save-draft",
        headers=headers,
        json={
            **payload,
            "phase": "commit",
            "draft_authorization": receipt,
            "evidence_ref": "EVR-DRAFT-CANONICAL-01",
            "evidence_digest": payload["content_digest"],
            "evidence_revision": 1,
        },
    )
    assert committed.status_code == 200
    assert committed.json()["data"]["draft"] == {
        "draft_ref": canonical_draft_ref,
        "revision": 1,
        "state": "editable",
    }
    durable = workflow.get_draft(scope, canonical_draft_ref)
    assert durable is not None and durable.inbox_item_ref == inbox.inbox_item_ref
    assert workflow.get_draft(scope, "DRF-01") is None
    assert authority.calls == [(scope, inbox.inbox_item_ref), (scope, inbox.inbox_item_ref)]

    rejected = TestClient(app).post(
        "/internal/v1/bff/email-inbox/save-draft",
        headers={**headers, "Idempotency-Key": "draft-authorize-02"},
        json={
            **payload,
            "allowed_team_refs": ["TEM-02"],
            "idempotency_key": "draft-authorize-02",
        },
    )
    assert rejected.status_code == 403
    assert authority.calls == [(scope, inbox.inbox_item_ref), (scope, inbox.inbox_item_ref)]

    missing_digest = TestClient(app).post(
        "/internal/v1/bff/email-inbox/save-draft",
        headers={**headers, "Idempotency-Key": "draft-authorize-03"},
        json={
            key: value
            for key, value in {**payload, "idempotency_key": "draft-authorize-03"}.items()
            if key != "participant_roles_digest"
        },
    )
    assert missing_digest.status_code == 400
    assert authority.calls == [(scope, inbox.inbox_item_ref), (scope, inbox.inbox_item_ref)]


def test_noncanonical_draft_update_is_rejected_before_authority_or_write() -> None:
    now = datetime(2026, 8, 13, 10, tzinfo=UTC)
    scope = TenantScope(SITE, "business_operations")
    workflow = InMemoryWorkflowRepository()
    inbox = InboxItem.new(
        site_id=SITE,
        mailbox_ref="MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        message_ref="MSG-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        team_ref="TEM-01",
        received_at=now,
    )
    workflow.save_inbox(scope, inbox)

    class ScopeAuthority:
        def __init__(self) -> None:
            self.calls = 0

        def authorize_inbox(self, _actor: object, _inbox_item_ref: str) -> tuple[TenantScope, str]:
            self.calls += 1
            return scope, "TEM-01"

        def authorize_conversation(self, *_args: object) -> TenantScope:
            raise AssertionError("conversation authority is outside this test")

    scope_authority = ScopeAuthority()
    participant = _ParticipantAuthority(
        {
            "gateway_receipt_ref": "EGR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "publication_ref": "PUB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "inbox_item_ref": inbox.inbox_item_ref,
            "message_ref": inbox.message_ref,
            "mailbox_ref": inbox.mailbox_ref,
            "mailbox_config_revision": 1,
            "observer_delivery_ref": "DLV-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "payload_digest": "sha256:" + "b" * 64,
            "participant_binding_digest": "sha256:" + "c" * 64,
            "evidence_binding_digest": "sha256:" + "d" * 64,
        }
    )
    app = create_email_gateway_app(
        intake=_Intake(),  # type: ignore[arg-type]
        participant_authority_reader=participant,
        publication_bearer_token=PUBLICATION_TOKEN,
        publication_auth_ref="observer-email-publication-v1",
        bff_bearer_token=BFF_TOKEN,
        bff_auth_ref="email-gateway-bff-v1",
        mailbox_registry=MailboxRegistry(InMemoryMailboxRepository()),
        read_repository=InMemoryPhase1ReadRepository(),
        connector_health_reader=_Health(),
        workflow_repository=workflow,
        workflow_authority=scope_authority,  # type: ignore[arg-type]
        clock=lambda: now,
    )
    payload = {
        **_scope_payload(roles=["Reviewer"], teams=["TEM-01"]),
        "phase": "authorize",
        "inbox_item_ref": inbox.inbox_item_ref,
        "draft_ref": stable_ref("DRF", SITE, "INB-CROSS-INBOX"),
        "expected_revision": 1,
        "content_digest": "sha256:" + "a" * 64,
        "participant_roles_digest": canonical_digest(
            {"sender": "mailbox_owner", "recipients": ["original_sender"]}
        ),
        "idempotency_key": "draft-update-noncanonical-01",
    }
    response = TestClient(app).post(
        "/internal/v1/bff/email-inbox/save-draft",
        headers={
            "Authorization": f"Bearer {BFF_TOKEN}",
            "X-GBOS-Local-Auth-Ref": "email-gateway-bff-v1",
            "X-Site-ID": SITE,
            "X-Processing-Purpose": "email_inbox_command",
            "X-Request-ID": "draft-update-noncanonical-request-01",
            "Idempotency-Key": "draft-update-noncanonical-01",
        },
        json=payload,
    )

    assert response.status_code == 403
    assert response.json() == {"error": {"code": "scope_mismatch"}}
    assert scope_authority.calls == 0
    assert participant.calls == []
    assert workflow.get_draft(scope, str(payload["draft_ref"])) is None
