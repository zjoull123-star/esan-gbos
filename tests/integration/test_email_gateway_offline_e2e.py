from __future__ import annotations

import hashlib
import json
import socket
from collections.abc import Mapping
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from services.email_gateway import protocols as gateway_protocols
from services.email_gateway.api import build_email_publication_api
from services.email_gateway.conversations import ConversationService
from services.email_gateway.identity_projection import IdentityProjectionService
from services.email_gateway.intake import GatewayIntakeService
from services.email_gateway.mailboxes import MailboxRegistry
from services.email_gateway.models import (
    AuthorityRoute,
    GatewayActorScope,
    IdentityProjection,
    IntakeResult,
    Mailbox,
    OutboundNotAuthorized,
    RevisionConflict,
    TenantScope,
)
from services.email_gateway.repositories.identity import InMemoryIdentityProjectionRepository
from services.email_gateway.repositories.intake import InMemoryIntakeRepository
from services.email_gateway.repositories.mailboxes import InMemoryMailboxRepository
from services.email_gateway.repositories.workflow import InMemoryWorkflowRepository
from services.email_gateway.routing import RoutingService
from services.email_gateway.send_outbox import DisabledSendOutboxRepository
from services.local_pilot_runtime.channel_config import (
    ChannelConfig,
    ChannelSettings,
    translate_legacy_imap_mailbox,
)
from services.local_pilot_runtime.email_gateway_config import MailboxRuntimeDeclaration
from services.local_pilot_runtime.email_gateway_worker import RelayStatus
from services.local_pilot_runtime.email_publication_worker import (
    EmailPublicationRelayWorker,
    ObserverPublicationOutboxAdapter,
)
from services.observer.observer.connectors.email_delivery import EmailRawDeliveryDecoder
from services.observer.observer.connectors.email_provider import (
    EmailProviderError,
    EmailProviderPollResult,
)
from services.observer.observer.connectors.fake_email_provider import (
    FakeEmailProvider,
    FakeEmailProviderMode,
)
from services.observer.observer.email_checkpoint_fence import (
    EmailPollBatchFence,
    InMemoryEmailCheckpointFence,
)
from services.observer.observer.email_publication import build_email_publication
from services.observer.observer.email_publication_outbox import (
    InMemoryEmailPublicationOutbox,
)
from services.observer.observer.evidence_store import ContentAddressedEvidenceStore
from services.observer.observer.identity_tokens import HmacSha256IdentityTokenResolver
from services.observer.observer.local_pilot_ingestion import DeliveryQuarantine
from services.observer.observer.models import (
    ConnectorKey,
    RawDelivery,
    stable_ulid,
)
from services.observer.observer.models import (
    TenantScope as ObserverTenantScope,
)
from services.observer.observer.normalizers import EmailObservationNormalizer
from services.observer.observer.scheduler import (
    DurablePollingScheduler,
    PollBatch,
    PollDisposition,
)

NOW = datetime(2026, 8, 13, 9, tzinfo=UTC)
SCOPE = TenantScope("alpha.example", "observation_processing")
OBSERVER_SCOPE = ObserverTenantScope(SCOPE.site_id, SCOPE.processing_purpose)
TEAM_REF = "team-sales"


def _connector_ref(instance_id: str) -> str:
    return "OCI-" + stable_ulid("email-connector-instance", OBSERVER_SCOPE.site_id, instance_id)


def _mailbox(suffix: str, *, instance_id: str) -> Mailbox:
    return Mailbox(
        mailbox_ref=f"MBX-01ARZ3NDEKTSV4RRFFQ69G5F{suffix}",
        site_id=SCOPE.site_id,
        address_display=f"encrypted-{suffix}",
        provider="fake",
        provider_account_ref=f"provider-{suffix}",
        observer_connector_instance_ref=_connector_ref(instance_id),
        entry_role="primary",
        business_purpose=SCOPE.processing_purpose,
        default_team_ref=TEAM_REF,
        account_owner_user_ref="owner-ref",
        priority=1,
        inbound_enabled=True,
        outbound_enabled=False,
        credential_ref="fake-disabled",
        status="active",
        config_revision=1,
        observer_config_projection_receipt=None,
    )


class _RecordingIntake:
    """Injected EmailPublicationIntake boundary backed by the real Gateway service."""

    def __init__(self, service: GatewayIntakeService) -> None:
        self.service = service
        self.results: list[IntakeResult] = []

    def accept(self, scope: TenantScope, publication: object) -> IntakeResult:
        result = self.service.accept(scope, publication)  # type: ignore[arg-type]
        self.results.append(result)
        return result

    def load_participant_authority_binding(
        self,
        scope: TenantScope,
        *,
        inbox_item_ref: str,
    ) -> Mapping[str, object] | None:
        return self.service.load_participant_authority_binding(
            scope,
            inbox_item_ref=inbox_item_ref,
        )


class _InProcessTransport:
    """Injected local HTTP boundary; it never opens a socket."""

    def __init__(self, client: TestClient, statuses: tuple[int, ...] = ()) -> None:
        self.client = client
        self.statuses = list(statuses)
        self.calls = 0

    def post(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> tuple[int, dict[str, Any]]:
        assert url == "http://email-gateway-api:8004/internal/v1/email-publications/accept"
        assert timeout_seconds < 30
        self.calls += 1
        if self.statuses:
            status = self.statuses.pop(0)
            if status != 200:
                return status, {"error": {"code": "rate_limited"}}
        response = self.client.post(
            "/internal/v1/email-publications/accept",
            headers=dict(headers),
            json=dict(payload),
        )
        return response.status_code, response.json()


class _OfflineObserverState:
    """Injected PollingState boundary composed from real Observer primitives."""

    def __init__(
        self,
        *,
        cas_root: Path,
        mailbox: Mailbox,
        crash_once: bool = False,
    ) -> None:
        self.cursor: str | None = None
        self.version = 0
        self.status = "healthy"
        self.mailbox = mailbox
        self.crash_once = crash_once
        self.fence = InMemoryEmailCheckpointFence()
        self.cas = ContentAddressedEvidenceStore(cas_root)
        self.outbox = InMemoryEmailPublicationOutbox()
        self.accepted_refs: dict[str, str] = {}
        self.quarantined: dict[str, str] = {}
        self.health: list[tuple[str, str | None]] = []
        self.lease_generation = 0

    def acquire(self, *_: object, **__: object) -> int:
        self.lease_generation += 1
        return self.lease_generation

    def release(self, *_: object, **__: object) -> None:
        return None

    def load_checkpoint(self, *_: object) -> tuple[str | None, int, str]:
        return self.cursor, self.version, self.status

    def register_poll_batch(
        self,
        scope: ObserverTenantScope,
        key: ConnectorKey,
        batch: PollBatch,
        *,
        expected_version: int,
        owner: str,
        lease_generation: int,
        now: datetime,
    ) -> EmailPollBatchFence:
        assert owner == "offline-email-poller"
        assert lease_generation == self.lease_generation
        return self.fence.register(
            scope,
            key,
            expected_cursor=batch.expected_cursor,
            candidate_cursor=batch.candidate_cursor,
            expected_version=expected_version,
            lease_generation=lease_generation,
            delivery_ids=tuple(item.delivery_id for item in batch.deliveries),
            now=now,
        )

    def accept_delivery(
        self,
        scope: ObserverTenantScope,
        key: ConnectorKey,
        delivery: RawDelivery,
        *,
        batch_id: str | None = None,
        owner: str,
        lease_generation: int,
        now: datetime,
    ) -> None:
        assert batch_id is not None
        assert owner == "offline-email-poller"
        assert lease_generation == self.lease_generation
        assert now == NOW
        stored = self.cas.put(scope, delivery.exact_bytes, media_type=delivery.media_type)
        self.accepted_refs[delivery.delivery_id] = stored.object_ref
        if self.crash_once:
            self.crash_once = False
            raise RuntimeError("injected crash after durable CAS")
        try:
            item = EmailRawDeliveryDecoder().decode_delivery(
                delivery.exact_bytes,
                delivery_id=delivery.delivery_id,
                received_at=delivery.received_at,
                source_ref=stored.object_ref,
            )[0]
            normalized = EmailObservationNormalizer(
                identity_resolver=HmacSha256IdentityTokenResolver(b"x" * 32),
                site_id=scope.site_id,
                purpose=scope.processing_purpose,
            ).normalize(item, source_ref=stored.object_ref)
            publication = build_email_publication(
                scope=scope,
                key=key,
                item=item,
                normalized=normalized,
                mailbox_id=self.mailbox.mailbox_ref,
                mailbox_config_revision=self.mailbox.config_revision,
                observer_delivery_ref=delivery.delivery_id,
                received_at=delivery.received_at,
                publication_revision=1,
            )
            self.outbox.append(publication, max_attempts=3)
        except DeliveryQuarantine as error:
            quarantine_ref = f"quarantine:{error.reason_code}:{delivery.delivery_id}"
            self.quarantined[delivery.delivery_id] = error.reason_code
            self.fence.mark_quarantine_terminal(
                scope,
                key,
                batch_id=batch_id,
                delivery_id=delivery.delivery_id,
                terminal_ref=quarantine_ref,
                lease_generation=lease_generation,
                now=NOW,
            )
            return
        self.fence.mark_publication_terminal(
            scope,
            key,
            batch_id=batch_id,
            delivery_id=delivery.delivery_id,
            terminal_ref=publication.publication_id,
            lease_generation=lease_generation,
            now=NOW,
        )

    def finalize_poll_batch(
        self,
        scope: ObserverTenantScope,
        key: ConnectorKey,
        *,
        batch_id: str,
        expected_version: int,
        owner: str,
        lease_generation: int,
        now: datetime,
    ) -> bool:
        assert owner == "offline-email-poller"
        assert lease_generation == self.lease_generation
        finalized = self.fence.finalize(
            scope,
            key,
            batch_id=batch_id,
            expected_version=expected_version,
            lease_generation=lease_generation,
            now=now,
        )
        if finalized:
            batch = next(item for item in self.fence.batches if item.batch_id == batch_id)
            self.cursor = batch.candidate_cursor
            self.version += 1
        return finalized

    def advance_checkpoint(self, *_: object, **__: object) -> None:
        raise AssertionError("email checkpoints must advance only through the batch fence")

    def update_health(
        self,
        _scope: ObserverTenantScope,
        _key: ConnectorKey,
        *,
        status: str,
        error_code: str | None,
        now: datetime,
    ) -> None:
        assert now == NOW
        self.status = status
        self.health.append((status, error_code))


class _FakeFrappeRouteAuthority:
    """Injected FrappeRouteAuthority protocol boundary; no Frappe or network call occurs."""

    def __init__(self, response: AuthorityRoute) -> None:
        self.response = response
        self.calls = 0

    def resolve(self, **_: object) -> AuthorityRoute:
        self.calls += 1
        return self.response


class _DeterministicProviderBoundary:
    """Injected EmailProvider boundary for one valid, distinct RFC Message-ID."""

    @property
    def provider_kind(self) -> str:
        return "fake"

    def poll(self, checkpoint: str | None, limit: int) -> EmailProviderPollResult:
        message = EmailMessage()
        message["From"] = "sender@example.invalid"
        message["To"] = "recipient@example.invalid"
        message["Subject"] = "fake subject three"
        message["Message-ID"] = "<fake-0003@example.invalid>"
        message.set_content("fake body three")
        deliveries = (RawDelivery("fake:0003", message.as_bytes(), "message/rfc822", NOW),)
        return EmailProviderPollResult(
            expected_cursor=checkpoint,
            candidate_cursor="fake:cursor:0003",
            deliveries=deliveries[:limit],
        )


def _poll(provider: Any) -> Any:
    def run(cursor: str | None, limit: int) -> PollBatch:
        result = provider.poll(cursor, limit)
        return PollBatch(
            disposition=PollDisposition.OK,
            expected_cursor=result.expected_cursor,
            candidate_cursor=result.candidate_cursor,
            deliveries=result.deliveries,
        )

    return run


def _scheduler(
    state: _OfflineObserverState,
    provider: Any,
    key: ConnectorKey,
) -> DurablePollingScheduler:
    return DurablePollingScheduler(
        state=state,
        poll=_poll(provider),
        scope=OBSERVER_SCOPE,
        key=key,
        clock=lambda: NOW,
        worker_id="offline-email-poller",
    )


def _relay(
    outbox: InMemoryEmailPublicationOutbox,
    transport: _InProcessTransport,
    *,
    now: datetime = NOW,
) -> RelayStatus:
    adapter = ObserverPublicationOutboxAdapter(
        cast(Any, outbox),
        site_id=SCOPE.site_id,
    )
    worker = EmailPublicationRelayWorker(
        outbox=adapter,
        transport=transport,
        bearer_token="offline-publication-secret",
        worker_id="offline-publisher",
        clock=lambda: now,
        lease_duration=timedelta(seconds=30),
        retry_delay=timedelta(seconds=5),
    )
    return worker.run_once().status


def _registry(*mailboxes: Mailbox) -> MailboxRegistry:
    registry = MailboxRegistry(InMemoryMailboxRepository())
    for index, mailbox in enumerate(mailboxes, start=1):
        registry.upsert(
            SCOPE,
            mailbox,
            expected_revision=0,
            actor_ref="offline-fixture",
            request_id=f"mailbox-{index}",
            idempotency_key=f"mailbox-{index}",
        )
    return registry


def test_complete_fake_provider_chain_reaches_independent_inboxes_and_manual_merge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbid_socket(*_: object, **__: object) -> None:
        raise AssertionError("offline E2E attempted a real network connection")

    monkeypatch.setattr(socket, "create_connection", forbid_socket)
    monkeypatch.setattr(socket.socket, "connect", forbid_socket)
    first_key = ConnectorKey("email", "primary-av")
    second_key = ConnectorKey("email", "primary-aw")
    first_mailbox = _mailbox("AV", instance_id=first_key.instance_id)
    second_mailbox = _mailbox("AW", instance_id=second_key.instance_id)
    registry = _registry(first_mailbox, second_mailbox)
    intake_repository = InMemoryIntakeRepository()
    recording = _RecordingIntake(GatewayIntakeService(intake_repository, registry))
    app = build_email_publication_api(
        intake=recording,
        bearer_token="offline-publication-secret",
        auth_ref="observer-email-publication-v1",
    )
    client = TestClient(app)
    transport = _InProcessTransport(client)
    states = (
        _OfflineObserverState(
            cas_root=tmp_path / "cas-av",
            mailbox=first_mailbox,
        ),
        _OfflineObserverState(
            cas_root=tmp_path / "cas-aw",
            mailbox=second_mailbox,
        ),
    )

    for state, key in zip(states, (first_key, second_key), strict=True):
        result = _scheduler(
            state,
            FakeEmailProvider(mode=FakeEmailProviderMode.ORDERED_SUCCESS, now=NOW),
            key,
        ).run_once(limit=1)
        assert result.status == "ok"
        assert result.accepted_count == 1
        assert result.checkpoint_advanced is True
        assert state.cursor == "fake:cursor:0002"
        assert len(state.accepted_refs) == 1
        assert len(state.outbox.records) == 1
        assert _relay(state.outbox, transport) is RelayStatus.DELIVERED

    distinct = _scheduler(states[0], _DeterministicProviderBoundary(), first_key).run_once(limit=1)
    assert distinct.status == "ok"
    assert distinct.checkpoint_advanced is True
    assert states[0].cursor == "fake:cursor:0003"
    assert _relay(states[0].outbox, transport) is RelayStatus.DELIVERED

    assert intake_repository.counts(SCOPE) == (3, 2, 3)
    assert len({item.receipt.mailbox_ref for item in recording.results}) == 2
    paired = [
        item
        for item in recording.results
        if item.message.message_ref == recording.results[0].message.message_ref
    ]
    assert len(paired) == 2
    assert paired[0].inbox_item.inbox_item_ref != paired[1].inbox_item.inbox_item_ref
    assert all(mailbox.entry_role == "primary" for mailbox in (first_mailbox, second_mailbox))

    replay_outbox = InMemoryEmailPublicationOutbox()
    replay_outbox.append(states[0].outbox.records[0])
    assert _relay(replay_outbox, transport) is RelayStatus.DELIVERED
    assert recording.results[-1].receipt.receipt_ref == recording.results[0].receipt.receipt_ref
    assert intake_repository.counts(SCOPE) == (3, 2, 3)

    identity_ref = recording.results[0].message.participants[0].identity_ref
    identity_service = IdentityProjectionService(InMemoryIdentityProjectionRepository())
    confirmed = IdentityProjection(
        site_id=SCOPE.site_id,
        processing_purpose=SCOPE.processing_purpose,
        opaque_address_ref=identity_ref,
        external_identity_ref="external-party-1",
        external_identity_revision=1,
        identity_type="Party",
        team_ref=TEAM_REF,
        status="confirmed",
        projection_receipt_ref="projection-receipt-1",
        observed_at=NOW,
        payload_digest="sha256:" + "a" * 64,
    )
    identity_service.apply(SCOPE, confirmed)
    authority = _FakeFrappeRouteAuthority(
        AuthorityRoute.assigned(
            party_ref="party-1",
            party_revision=4,
            team_ref=TEAM_REF,
            team_revision=7,
            owner_user_ref="owner@example.invalid",
            owner_eligibility_revision="sha256:" + "b" * 64,
            resolved_at=NOW,
        )
    )
    assigned = RoutingService(authority).route(
        scope=SCOPE,
        inbox=recording.results[0].inbox_item,
        mailbox=first_mailbox,
        projection=identity_service.get(SCOPE, identity_ref),
        rules=(),
    )
    assert assigned.route_status == "assigned"
    assert authority.calls == 1

    revoked = replace(
        confirmed,
        external_identity_revision=2,
        status="revoked",
        projection_receipt_ref="projection-receipt-2",
        payload_digest="sha256:" + "c" * 64,
    )
    identity_service.apply(SCOPE, revoked)
    with pytest.raises(RevisionConflict, match="stale identity projection"):
        identity_service.apply(SCOPE, confirmed)
    stale_route = RoutingService(authority).route(
        scope=SCOPE,
        inbox=recording.results[0].inbox_item,
        mailbox=first_mailbox,
        projection=identity_service.get(SCOPE, identity_ref),
        rules=(),
    )
    assert stale_route.route_status == "unassigned"
    assert stale_route.safe_reason_code == "identity_unavailable"
    assert authority.calls == 1

    workflow = InMemoryWorkflowRepository()
    conversations = ConversationService(workflow)
    distinct_result = next(
        item
        for item in recording.results
        if item.message.message_ref != recording.results[0].message.message_ref
    )
    workflow.save_inbox(SCOPE, recording.results[0].inbox_item)
    workflow.save_inbox(SCOPE, distinct_result.inbox_item)
    suggestion = conversations.propose(
        SCOPE,
        left_inbox_ref=recording.results[0].inbox_item.inbox_item_ref,
        right_inbox_ref=distinct_result.inbox_item.inbox_item_ref,
        signals=("message_id_family", "participant_time_digest"),
        confidence=0.91,
        now=NOW,
    )
    assert suggestion.status == "proposed"
    assert (
        conversations.get_conversation_for(SCOPE, recording.results[0].inbox_item.inbox_item_ref)
        is None
    )
    merged = conversations.accept(
        SCOPE,
        actor=GatewayActorScope(
            site_id=SCOPE.site_id,
            actor_ref="reviewer@example.invalid",
            team_refs=(TEAM_REF,),
            roles=("Reviewer",),
        ),
        suggestion_ref=suggestion.suggestion_ref,
        expected_suggestion_revision=1,
        expected_left_revision=1,
        expected_right_revision=1,
        request_id="offline-manual-merge",
        idempotency_key="offline-manual-merge",
        now=NOW,
    )
    assert len(merged.inbox_item_refs) == 2

    assert client.get("/health").json() == {
        "ready": True,
        "external_send": False,
        "provider_credentials_loaded": False,
    }
    registered = tuple(
        registry.get(SCOPE, mailbox.mailbox_ref) for mailbox in (first_mailbox, second_mailbox)
    )
    assert all(mailbox is not None and mailbox.outbound_enabled is False for mailbox in registered)
    with pytest.raises(OutboundNotAuthorized, match="outbound_not_authorized"):
        DisabledSendOutboxRepository(outbound_enabled=False).insert(SCOPE, object())
    assert {
        "EmailTransport",
        "LLMClient",
        "ModelClient",
        "ProviderSender",
        "SmtpClient",
        "send_email",
    }.isdisjoint(vars(gateway_protocols))
    assert transport.calls == 4


def test_crash_restart_preserves_cas_and_checkpoint_until_publication(
    tmp_path: Path,
) -> None:
    key = ConnectorKey("email", "crash-restart")
    mailbox = _mailbox("AX", instance_id=key.instance_id)
    state = _OfflineObserverState(
        cas_root=tmp_path / "cas-crash",
        mailbox=mailbox,
        crash_once=True,
    )
    provider = FakeEmailProvider(mode=FakeEmailProviderMode.ORDERED_SUCCESS, now=NOW)

    crashed = _scheduler(state, provider, key).run_once(limit=1)
    assert crashed.status == "retry"
    assert crashed.safe_error_code == "durable_accept_failed"
    assert crashed.checkpoint_advanced is False
    assert state.cursor is None
    assert len(state.accepted_refs) == 1
    assert state.outbox.records == ()

    restarted = _scheduler(state, provider, key).run_once(limit=1)
    assert restarted.status == "ok"
    assert restarted.checkpoint_advanced is True
    assert state.cursor == "fake:cursor:0002"
    assert len(state.accepted_refs) == 1
    assert len(state.outbox.records) == 1


def test_oversized_fake_mode_quarantines_then_allows_checkpoint(
    tmp_path: Path,
) -> None:
    key = ConnectorKey("email", "quarantine")
    mailbox = _mailbox("AY", instance_id=key.instance_id)
    state = _OfflineObserverState(cas_root=tmp_path / "cas-quarantine", mailbox=mailbox)

    result = _scheduler(
        state,
        FakeEmailProvider(mode=FakeEmailProviderMode.OVERSIZED_ATTACHMENT, now=NOW),
        key,
    ).run_once(limit=1)

    assert result.status == "ok"
    assert result.checkpoint_advanced is True
    assert state.quarantined == {"fake:oversized": "email.attachment_too_large"}
    assert state.outbox.records == ()
    assert state.fence.batches[0].members[0].terminal_kind == "quarantined"


def test_provider_duplicate_batch_is_deduplicated_before_checkpoint_and_publication(
    tmp_path: Path,
) -> None:
    key = ConnectorKey("email", "duplicate-batch")
    mailbox = _mailbox("AZ", instance_id=key.instance_id)
    state = _OfflineObserverState(cas_root=tmp_path / "cas-duplicate", mailbox=mailbox)

    result = _scheduler(
        state,
        FakeEmailProvider(mode=FakeEmailProviderMode.DUPLICATE, now=NOW),
        key,
    ).run_once(limit=10)

    assert result.status == "ok"
    assert result.accepted_count == 1
    assert result.safe_error_code is None
    assert result.checkpoint_advanced is True
    assert len(state.accepted_refs) == 1
    assert len(state.outbox.records) == 1


def test_provider_duplicate_delivery_id_with_payload_drift_pauses_connector(
    tmp_path: Path,
) -> None:
    class DriftProvider:
        @property
        def provider_kind(self) -> str:
            return "fake"

        def poll(self, checkpoint: str | None, limit: int) -> EmailProviderPollResult:
            first = RawDelivery(
                "fake:drift",
                b"first",
                "message/rfc822",
                NOW,
            )
            second = RawDelivery(
                "fake:drift",
                b"second",
                "message/rfc822",
                NOW,
            )
            return EmailProviderPollResult(
                expected_cursor=checkpoint,
                candidate_cursor="fake:cursor:drift",
                deliveries=(first, second)[:limit],
            )

    key = ConnectorKey("email", "duplicate-drift")
    mailbox = _mailbox("B1", instance_id=key.instance_id)
    state = _OfflineObserverState(cas_root=tmp_path / "cas-drift", mailbox=mailbox)

    result = _scheduler(state, DriftProvider(), key).run_once(limit=10)

    assert result.status == "paused"
    assert result.safe_error_code == "duplicate_delivery_drift"
    assert result.checkpoint_advanced is False
    assert state.accepted_refs == {}
    assert state.outbox.records == ()


def test_provider_rate_limit_is_safe_and_relay_429_retries_are_bounded(
    tmp_path: Path,
) -> None:
    provider = FakeEmailProvider(mode=FakeEmailProviderMode.RATE_LIMITED, now=NOW)
    with pytest.raises(EmailProviderError, match="provider_rate_limited") as rate_limited:
        provider.poll(None, 10)
    assert rate_limited.value.retryable is True

    key = ConnectorKey("email", "relay-rate-limit")
    mailbox = _mailbox("B0", instance_id=key.instance_id)
    state = _OfflineObserverState(cas_root=tmp_path / "cas-rate-limit", mailbox=mailbox)
    produced = _scheduler(
        state,
        FakeEmailProvider(mode=FakeEmailProviderMode.ORDERED_SUCCESS, now=NOW),
        key,
    ).run_once(limit=1)
    assert produced.status == "ok"
    bounded = InMemoryEmailPublicationOutbox()
    bounded.append(state.outbox.records[0], max_attempts=2)
    intake = _RecordingIntake(GatewayIntakeService(InMemoryIntakeRepository(), _registry(mailbox)))
    app = build_email_publication_api(
        intake=intake,
        bearer_token="offline-publication-secret",
        auth_ref="observer-email-publication-v1",
    )
    transport = _InProcessTransport(TestClient(app), statuses=(429, 429, 200))

    assert _relay(bounded, transport, now=NOW) is RelayStatus.RETRY
    assert _relay(bounded, transport, now=NOW + timedelta(seconds=5)) is RelayStatus.DEAD_LETTER
    assert _relay(bounded, transport, now=NOW + timedelta(seconds=10)) is RelayStatus.IDLE
    assert transport.calls == 2
    assert intake.results == []


def test_legacy_imap_cutover_is_pure_disabled_and_contains_no_history_state(
    tmp_path: Path,
) -> None:
    channel_config = ChannelConfig(
        site_id=SCOPE.site_id,
        external_send=False,
        evidence_cas_root=tmp_path / "legacy-cas",
        channels=MappingProxyType(
            {
                "email": ChannelSettings(
                    enabled=False,
                    kill_switch=True,
                    activation_time=None,
                    backfill_history=False,
                    credential_file=tmp_path / "must-not-be-read.json",
                )
            }
        ),
    )

    first = translate_legacy_imap_mailbox(
        channel_config,
        mailbox_ref="MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        cutover_publication_revision=9,
        activation_watermark="uidvalidity:42;uid:100",
        business_mode="migration",
    )
    replay = translate_legacy_imap_mailbox(
        channel_config,
        mailbox_ref="MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        cutover_publication_revision=9,
        activation_watermark="uidvalidity:42;uid:100",
        business_mode="migration",
    )
    gateway_declaration = MailboxRuntimeDeclaration(
        mailbox_ref=first.mailbox_ref,
        provider_kind=first.provider_kind,
        business_mode=first.business_mode,
        enabled=first.enabled,
        cutover_publication_revision=first.cutover_publication_revision,
        activation_watermark=first.activation_watermark,
        legacy_migration=True,
        backfill_history=first.backfill_history,
    )

    assert replay == first
    assert gateway_declaration.enabled is False
    assert gateway_declaration.backfill_history is False
    assert gateway_declaration.cutover_publication_revision == 9
    assert gateway_declaration.activation_watermark == "uidvalidity:42;uid:100"
    assert channel_config.channels["email"].enabled is False
    assert not (tmp_path / "must-not-be-read.json").exists()
    forbidden_state = {
        "delivery",
        "checkpoint",
        "cursor",
        "quarantine",
        "history",
        "raw_eml",
        "attachment",
    }
    assert forbidden_state.isdisjoint(field.name for field in fields(first))
    assert forbidden_state.isdisjoint(field.name for field in fields(gateway_declaration))


def test_transient_mailbox_address_becomes_only_opaque_revisioned_authority(
    tmp_path: Path,
) -> None:
    from services.email_gateway.models import MailboxConnectorProjection
    from services.observer.observer.email_connector_config import (
        InMemoryEmailConnectorConfigRepository,
    )
    from services.observer.observer.email_mailbox_identity import EmailMailboxIdentityService
    from services.observer.observer.email_participant_authority import (
        EmailParticipantAuthorityBinding,
        EmailParticipantAuthorityRecord,
        EmailParticipantAuthorityResolver,
        InMemoryEmailParticipantAuthorityRepository,
        canonical_binding_digest,
    )

    canonical_address = "mailbox.owner@example.invalid"
    identity_resolver = HmacSha256IdentityTokenResolver(b"x" * 32)
    identity = EmailMailboxIdentityService(identity_resolver=identity_resolver).derive(
        OBSERVER_SCOPE,
        canonical_mailbox_address=canonical_address,
    )
    other_site_identity = EmailMailboxIdentityService(identity_resolver=identity_resolver).derive(
        ObserverTenantScope("other.example", "observation_processing"),
        canonical_mailbox_address=canonical_address,
    )
    assert identity.normalization_version == "email-v1"
    assert identity.opaque_address_ref.startswith("extid:v1:email:")
    assert identity.opaque_address_ref != other_site_identity.opaque_address_ref

    mailbox = replace(
        _mailbox("B1", instance_id="mailbox-identity"),
        address_display="Main customer inbox",
        mailbox_address_identity_ref=identity.opaque_address_ref,
        config_revision=1,
    )
    receipt = MailboxRegistry(InMemoryMailboxRepository()).upsert(
        SCOPE,
        mailbox,
        expected_revision=0,
        actor_ref="offline-admin",
        request_id="offline-mailbox-identity",
        idempotency_key="offline-mailbox-identity",
    )
    projection = MailboxConnectorProjection(
        site_id=SCOPE.site_id,
        observer_connector_instance_ref=mailbox.observer_connector_instance_ref,
        provider_kind="imap_smtp",
        entry_role=mailbox.entry_role,
        business_purpose=mailbox.business_purpose,
        team_ref="TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        credential_ref="secretref:v1/email/main",
        inbound_enabled=True,
        mailbox_ref=mailbox.mailbox_ref,
        mailbox_config_revision=receipt.mailbox.config_revision,
        activation_not_before=NOW,
        projection_revision=receipt.mailbox.config_revision,
        mailbox_address_identity_ref=identity.opaque_address_ref,
    )
    projection_wire = projection.to_wire()
    config_repository = InMemoryEmailConnectorConfigRepository()
    config_repository.apply(
        config_publication_ref=receipt.config_publication_ref,
        projection=projection_wire,
        projected_at=NOW,
    )
    assert config_repository.projections[0].mailbox_address_identity_ref == (
        identity.opaque_address_ref
    )

    message = EmailMessage()
    message["From"] = "customer@example.invalid"
    message["To"] = canonical_address
    message["Subject"] = "Offline mailbox identity"
    message["Message-ID"] = "<mailbox-identity@example.invalid>"
    message.set_content("Offline body")
    raw = message.as_bytes()
    store = ContentAddressedEvidenceStore(tmp_path / "mailbox-identity-cas")
    stored = store.put(OBSERVER_SCOPE, raw, media_type="message/rfc822")
    item = EmailRawDeliveryDecoder().decode_delivery(
        raw,
        delivery_id="provider-mailbox-identity",
        received_at=NOW,
        source_ref=stored.object_ref,
    )[0]
    normalized = EmailObservationNormalizer(
        identity_resolver=identity_resolver,
        site_id=OBSERVER_SCOPE.site_id,
        purpose=OBSERVER_SCOPE.processing_purpose,
    ).normalize(item, source_ref=stored.object_ref)
    publication = build_email_publication(
        scope=OBSERVER_SCOPE,
        key=ConnectorKey("email", "mailbox-identity"),
        item=item,
        normalized=normalized,
        mailbox_id=mailbox.mailbox_ref,
        mailbox_config_revision=receipt.mailbox.config_revision,
        observer_delivery_ref="provider-mailbox-identity",
        received_at=NOW,
        publication_revision=1,
    )
    publication_payload = publication.to_wire()
    binding = EmailParticipantAuthorityBinding.from_wire(
        {
            "gateway_receipt_ref": "EGR-"
            + stable_ulid("gateway-receipt", publication.publication_id),
            "publication_ref": publication.publication_id,
            "inbox_item_ref": "INB-" + stable_ulid("inbox", publication.publication_id),
            "message_ref": "MSG-" + stable_ulid("message", publication.publication_id),
            "mailbox_ref": mailbox.mailbox_ref,
            "mailbox_config_revision": receipt.mailbox.config_revision,
            "observer_delivery_ref": publication.observer_delivery_ref,
            "payload_digest": canonical_binding_digest(publication_payload),
            "participant_binding_digest": canonical_binding_digest(
                publication_payload["participants"]
            ),
            "evidence_binding_digest": canonical_binding_digest(
                publication_payload["evidence_refs"]
            ),
        }
    )
    authority = EmailParticipantAuthorityResolver(
        repository=InMemoryEmailParticipantAuthorityRepository(
            (
                EmailParticipantAuthorityRecord(
                    binding=binding,
                    publication_payload=publication_payload,
                    delivery_id="provider-mailbox-identity",
                    object_ref=stored.object_ref,
                    exact_body_sha256=hashlib.sha256(raw).hexdigest(),
                    byte_size=len(raw),
                    media_type="message/rfc822",
                    received_at=NOW,
                    mailbox_address_identity_ref=(identity.opaque_address_ref),
                ),
            )
        ),
        store=store,
        identity_resolver=identity_resolver,
    )
    resolved = authority(
        OBSERVER_SCOPE,
        binding,
        {"sender": "mailbox_owner", "recipients": ["original_sender"]},
    )

    assert resolved["participant_projection"] == [
        {"address_role": "sender", "opaque_address_ref": identity.opaque_address_ref},
        {
            "address_role": "to",
            "opaque_address_ref": identity_resolver.resolve(
                OBSERVER_SCOPE.site_id,
                OBSERVER_SCOPE.processing_purpose,
                "email",
                "customer@example.invalid",
            ),
        },
    ]
    durable_rendering = json.dumps(
        {
            "mailbox": receipt.mailbox.to_wire(),
            "projection": projection_wire,
            "observer_config": [value.comparable() for value in config_repository.projections],
            "participant_projection": resolved["participant_projection"],
        },
        default=str,
        sort_keys=True,
    )
    assert canonical_address not in durable_rendering
    assert canonical_address not in repr((identity, receipt, projection, authority))
