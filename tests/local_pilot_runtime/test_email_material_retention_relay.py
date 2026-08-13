from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from services.email_gateway.models import TenantScope
from services.email_gateway.terminal_retention import (
    TerminalAuthorityRegistrationLease,
    TerminalMaterialAuthority,
)
from services.local_pilot_runtime.email_material_retention_relay import (
    GatewayAuthorityRegistrationRelay,
    ObserverTombstoneCallbackRelay,
)
from services.observer.observer.email_material_retention_callback import (
    EmailMaterialRetentionCallback,
    EmailMaterialRetentionCallbackLease,
)

NOW = datetime(2026, 8, 14, 8, tzinfo=UTC)
SCOPE = TenantScope("alpha.example", "audit_compliance")
PURPOSE = "email_draft_material"


def _authority_lease() -> TerminalAuthorityRegistrationLease:
    authority = TerminalMaterialAuthority(
        authority_receipt_ref="ETA-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        site_id=SCOPE.site_id,
        purpose=PURPOSE,
        draft_ref="DRF-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        draft_revision=4,
        material_kind="draft",
        evidence_ref="EVR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        evidence_digest="sha256:" + "1" * 64,
        terminal_state="sent",
        terminal_at=NOW,
        not_before=NOW + timedelta(days=30),
        source_authority_receipt_ref="PRC-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        payload_digest="sha256:" + "2" * 64,
    )
    return TerminalAuthorityRegistrationLease(
        authority=authority,
        registration_request_ref="ETR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        worker_id="gateway-retention-1",
        attempt=1,
        lease_generation=1,
        lease_expires_at=NOW + timedelta(minutes=5),
    )


class _GatewayService:
    def __init__(self) -> None:
        self.lease = _authority_lease()
        self.acked: list[object] = []
        self.failed: list[str] = []

    def claim_registration(self, *args: object, **kwargs: object):
        lease, self.lease = self.lease, None
        return lease

    def ack_registration(self, scope: object, lease: object, *, response: object):
        self.acked.append(response)

    def fail_registration(self, scope: object, lease: object, *, safe_code: str):
        self.failed.append(safe_code)


class _ObserverRegistrationTransport:
    def register(self, payload: dict[str, object]) -> dict[str, object]:
        assert set(payload) == {"schema_version", "site_id", "authority_receipt_ref"}
        lease = _authority_lease()
        return {
            "schema_version": "1.0",
            "site_id": SCOPE.site_id,
            "evidence_ref": lease.authority.evidence_ref,
            "request_ref": "EMR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "not_before": lease.authority.not_before.isoformat().replace("+00:00", "Z"),
        }


def test_gateway_registration_relay_acks_only_strict_observer_response() -> None:
    service = _GatewayService()
    worker = GatewayAuthorityRegistrationRelay(
        service=service,
        transport=_ObserverRegistrationTransport(),
        worker_id="gateway-retention-1",
        clock=lambda: NOW,
    )

    assert worker.run_once(SCOPE)
    assert len(service.acked) == 1
    assert service.failed == []


class _ResponseLossGatewayService(_GatewayService):
    def __init__(self) -> None:
        super().__init__()
        self.persisted = False

    def ack_registration(self, scope: object, lease: object, *, response: object):
        self.persisted = True
        raise TimeoutError("injected response loss after durable ack")

    def fail_registration(self, scope: object, lease: object, *, safe_code: str):
        assert self.persisted
        self.failed.append("already_registered")


def test_gateway_registration_response_loss_restarts_without_duplicate_work() -> None:
    service = _ResponseLossGatewayService()
    worker = GatewayAuthorityRegistrationRelay(
        service=service,
        transport=_ObserverRegistrationTransport(),
        worker_id="gateway-retention-1",
        clock=lambda: NOW,
    )

    assert worker.run_once(SCOPE)
    assert service.persisted
    assert worker.run_once(SCOPE) is False
    assert service.failed == ["already_registered"]


def _callback_lease() -> EmailMaterialRetentionCallbackLease:
    callback = EmailMaterialRetentionCallback(
        callback_ref="EMC-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        site_id=SCOPE.site_id,
        purpose=PURPOSE,
        authority_receipt_ref="ETA-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        evidence_ref="EVR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        observer_request_ref="EMR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        tombstone_receipt_ref="TMB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        deleted_at=NOW + timedelta(days=30),
        evidence_digest="sha256:" + "1" * 64,
        callback_payload_digest="sha256:" + "2" * 64,
    )
    return EmailMaterialRetentionCallbackLease(
        callback=callback,
        worker_id="observer-callback-1",
        attempt=1,
        lease_generation=1,
        lease_expires_at=NOW + timedelta(minutes=5),
    )


class _CallbackRepository:
    def __init__(self) -> None:
        self.lease = _callback_lease()
        self.acked: list[str] = []

    def claim(self, *args: object, **kwargs: object):
        lease, self.lease = self.lease, None
        return lease

    def ack(self, scope: object, lease: object, *, callback_receipt_ref: str, now: datetime):
        self.acked.append(callback_receipt_ref)

    def fail(self, *args: object, **kwargs: object):
        raise AssertionError("callback should not fail")


class _GatewayCallbackTransport:
    def deliver(self, payload: dict[str, object]) -> dict[str, object]:
        assert "object_ref" not in payload
        return {
            "schema_version": "1.0",
            "site_id": SCOPE.site_id,
            "authority_receipt_ref": payload["authority_receipt_ref"],
            "tombstone_receipt_ref": payload["tombstone_receipt_ref"],
            "callback_receipt_ref": "GTC-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "accepted": True,
        }


def test_observer_callback_relay_delivers_closed_payload_and_acks_receipt() -> None:
    repository = _CallbackRepository()
    worker = ObserverTombstoneCallbackRelay(
        repository=repository,
        transport=_GatewayCallbackTransport(),
        worker_id="observer-callback-1",
        clock=lambda: NOW,
    )

    assert worker.run_once(SCOPE)
    assert repository.acked == ["GTC-01ARZ3NDEKTSV4RRFFQ69G5FAV"]


class _AdvancingClock:
    def __init__(self, *values: datetime) -> None:
        self.values = iter(values)

    def __call__(self) -> datetime:
        return next(self.values)


class _FencedCallbackRepository(_CallbackRepository):
    def __init__(self) -> None:
        super().__init__()
        self.current_generation = 2
        self.ack_times: list[datetime] = []
        self.fail_times: list[datetime] = []

    def ack(self, scope: object, lease: object, *, callback_receipt_ref: str, now: datetime):
        assert isinstance(lease, EmailMaterialRetentionCallbackLease)
        self.ack_times.append(now)
        if lease.lease_generation != self.current_generation or now >= lease.lease_expires_at:
            raise ValueError("callback lease fence conflict")
        self.acked.append(callback_receipt_ref)

    def fail(
        self,
        scope: object,
        lease: object,
        *,
        safe_code: str,
        next_attempt_at: datetime,
        now: datetime,
    ) -> None:
        assert isinstance(lease, EmailMaterialRetentionCallbackLease)
        self.fail_times.append(now)
        if lease.lease_generation != self.current_generation or now >= lease.lease_expires_at:
            raise ValueError("callback lease fence conflict")


def test_callback_relay_uses_fresh_post_transport_time_and_stale_generation_cannot_ack() -> None:
    repository = _FencedCallbackRepository()
    after_expiry = NOW + timedelta(minutes=6)
    clock = _AdvancingClock(NOW, after_expiry, after_expiry)
    worker = ObserverTombstoneCallbackRelay(
        repository=repository,
        transport=_GatewayCallbackTransport(),
        worker_id="observer-callback-1",
        clock=clock,
    )

    with pytest.raises(ValueError, match="lease fence conflict"):
        worker.run_once(SCOPE)

    assert repository.acked == []
    assert repository.ack_times == [after_expiry]
    assert repository.fail_times == [after_expiry]
    assert repository.current_generation == 2
