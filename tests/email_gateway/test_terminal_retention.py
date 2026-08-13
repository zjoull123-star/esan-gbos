from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from services.email_gateway.models import TenantScope
from services.email_gateway.terminal_retention import (
    EmailMaterialTerminalRetentionService,
    GatewayTombstoneCallbackReceipt,
    HumanDiscardAuthorityReceipt,
    ObserverRegistrationReceipt,
    TerminalAuthorityRegistrationLease,
    TerminalMaterialAuthority,
)

NOW = datetime(2026, 8, 14, 8, tzinfo=UTC)
SCOPE = TenantScope("alpha.example", "audit_compliance")
PURPOSE = "email_draft_material"
DRAFT_REF = "DRF-01ARZ3NDEKTSV4RRFFQ69G5FAV"
DRAFT_EVIDENCE = "EVR-01ARZ3NDEKTSV4RRFFQ69G5FAV"
MIME_EVIDENCE = "EVR-01ARZ3NDEKTSV4RRFFQ69G5FAW"
DRAFT_DIGEST = "sha256:" + "1" * 64
MIME_DIGEST = "sha256:" + "2" * 64
AUTHORITY_DIGEST = "sha256:" + "3" * 64
CALLBACK_DIGEST = "sha256:" + "4" * 64


def _authority(kind: str = "draft", *, state: str = "sent") -> TerminalMaterialAuthority:
    return TerminalMaterialAuthority(
        authority_receipt_ref=(
            "ETA-01ARZ3NDEKTSV4RRFFQ69G5FAV"
            if kind == "draft"
            else "ETA-01ARZ3NDEKTSV4RRFFQ69G5FAW"
        ),
        site_id=SCOPE.site_id,
        purpose=PURPOSE,
        draft_ref=DRAFT_REF,
        draft_revision=4,
        material_kind=kind,
        evidence_ref=DRAFT_EVIDENCE if kind == "draft" else MIME_EVIDENCE,
        evidence_digest=DRAFT_DIGEST if kind == "draft" else MIME_DIGEST,
        terminal_state=state,
        terminal_at=NOW,
        not_before=NOW + timedelta(days=30),
        source_authority_receipt_ref="PRC-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        payload_digest=AUTHORITY_DIGEST,
    )


def _lease(
    authority: TerminalMaterialAuthority | None = None,
) -> TerminalAuthorityRegistrationLease:
    return TerminalAuthorityRegistrationLease(
        authority=authority or _authority(),
        registration_request_ref="ETR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        worker_id="gateway-retention-1",
        attempt=2,
        lease_generation=3,
        lease_expires_at=NOW + timedelta(minutes=5),
    )


class _Repository:
    def __init__(self) -> None:
        self.sent: tuple[TerminalMaterialAuthority, ...] = ()
        self.discarded: list[HumanDiscardAuthorityReceipt] = []
        self.resolved: TerminalMaterialAuthority | None = None
        self.lease: TerminalAuthorityRegistrationLease | None = None
        self.acks: list[tuple[TerminalAuthorityRegistrationLease, ObserverRegistrationReceipt]] = []
        self.failures: list[tuple[TerminalAuthorityRegistrationLease, str, datetime]] = []
        self.callbacks: list[object] = []

    def create_sent_authorities(
        self, scope: TenantScope, *, provider_receipt_record_ref: str
    ) -> tuple[TerminalMaterialAuthority, ...]:
        assert scope == SCOPE
        assert provider_receipt_record_ref.startswith("PRC-")
        return self.sent

    def create_discard_authority(
        self, scope: TenantScope, *, receipt: HumanDiscardAuthorityReceipt
    ) -> TerminalMaterialAuthority:
        assert scope == SCOPE
        self.discarded.append(receipt)
        return _authority(state="discarded")

    def resolve_terminal(
        self, scope: TenantScope, authority_receipt_ref: str
    ) -> TerminalMaterialAuthority:
        assert scope == SCOPE
        assert self.resolved is not None
        assert authority_receipt_ref == self.resolved.authority_receipt_ref
        return self.resolved

    def claim_registration(self, *args: object, **kwargs: object) -> object:
        return self.lease

    def heartbeat_registration(self, *args: object, **kwargs: object) -> datetime:
        return NOW + timedelta(minutes=5)

    def ack_registration(
        self,
        scope: TenantScope,
        lease: TerminalAuthorityRegistrationLease,
        *,
        receipt: ObserverRegistrationReceipt,
        now: datetime,
    ) -> ObserverRegistrationReceipt:
        assert scope == SCOPE
        assert now == NOW
        self.acks.append((lease, receipt))
        return receipt

    def fail_registration(
        self,
        scope: TenantScope,
        lease: TerminalAuthorityRegistrationLease,
        *,
        safe_code: str,
        next_attempt_at: datetime,
        now: datetime,
    ) -> None:
        assert scope == SCOPE
        assert now == NOW
        self.failures.append((lease, safe_code, next_attempt_at))

    def accept_tombstone_callback(self, scope: TenantScope, *, callback: object, now: datetime):
        assert scope == SCOPE
        assert now == NOW
        self.callbacks.append(callback)
        return GatewayTombstoneCallbackReceipt(
            callback_receipt_ref="GTC-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            site_id=SCOPE.site_id,
            authority_receipt_ref=_authority().authority_receipt_ref,
            tombstone_receipt_ref="TMB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        )


def _service(repository: _Repository) -> EmailMaterialTerminalRetentionService:
    return EmailMaterialTerminalRetentionService(repository=repository, clock=lambda: NOW)


def test_sent_provider_receipt_creates_exactly_draft_and_final_mime_authorities() -> None:
    repository = _Repository()
    repository.sent = (_authority("draft"), _authority("final_mime"))

    result = _service(repository).record_provider_outcome(
        SCOPE,
        provider_receipt_record_ref="PRC-01ARZ3NDEKTSV4RRFFQ69G5FAV",
    )

    assert tuple(item.material_kind for item in result) == ("draft", "final_mime")
    assert tuple(item.evidence_ref for item in result) == (DRAFT_EVIDENCE, MIME_EVIDENCE)
    assert len({item.authority_receipt_ref for item in result}) == 2


def test_rejected_provider_receipt_creates_no_authority() -> None:
    repository = _Repository()
    assert (
        _service(repository).record_provider_outcome(
            SCOPE,
            provider_receipt_record_ref="PRC-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        )
        == ()
    )


def test_explicit_discard_requires_closed_human_receipt_and_creates_only_draft() -> None:
    repository = _Repository()
    receipt = HumanDiscardAuthorityReceipt(
        authority_receipt_ref="HDR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        site_id=SCOPE.site_id,
        purpose=PURPOSE,
        draft_ref=DRAFT_REF,
        draft_revision=4,
        evidence_ref=DRAFT_EVIDENCE,
        evidence_digest=DRAFT_DIGEST,
        terminal_at=NOW,
        payload_digest=AUTHORITY_DIGEST,
    )

    authority = _service(repository).discard(SCOPE, receipt=receipt)

    assert authority.material_kind == "draft"
    assert authority.terminal_state == "discarded"
    assert repository.discarded == [receipt]
    with pytest.raises(TypeError):
        _service(repository).discard(SCOPE, receipt=True)  # type: ignore[arg-type]


def test_resolve_and_registration_ack_are_exactly_revision_and_evidence_pinned() -> None:
    repository = _Repository()
    repository.resolved = _authority()
    service = _service(repository)

    assert service.resolve_terminal(SCOPE, _authority().authority_receipt_ref) == _authority()
    response = {
        "schema_version": "1.0",
        "site_id": SCOPE.site_id,
        "evidence_ref": DRAFT_EVIDENCE,
        "request_ref": "EMR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "not_before": (NOW + timedelta(days=30)).isoformat().replace("+00:00", "Z"),
    }
    receipt = service.ack_registration(SCOPE, _lease(), response=response)
    assert receipt.request_ref == "EMR-01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert repository.acks == [(_lease(), receipt)]

    drift = dict(response)
    drift["evidence_ref"] = MIME_EVIDENCE
    with pytest.raises(ValueError, match="registration response"):
        service.ack_registration(SCOPE, _lease(), response=drift)


def test_registration_failure_is_safe_and_retry_is_bounded() -> None:
    repository = _Repository()
    service = _service(repository)

    service.fail_registration(SCOPE, _lease(), safe_code="observer_unavailable")

    assert repository.failures == [(_lease(), "observer_unavailable", NOW + timedelta(seconds=4))]
    with pytest.raises(ValueError, match="safe code"):
        service.fail_registration(SCOPE, _lease(), safe_code="person@example.invalid")


def test_callback_is_closed_and_draft_final_mime_bindings_remain_separate() -> None:
    repository = _Repository()
    service = _service(repository)
    payload = {
        "schema_version": "1.0",
        "site_id": SCOPE.site_id,
        "purpose": PURPOSE,
        "authority_receipt_ref": _authority().authority_receipt_ref,
        "evidence_ref": DRAFT_EVIDENCE,
        "observer_request_ref": "EMR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "tombstone_receipt_ref": "TMB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "deleted_at": (NOW + timedelta(days=30)).isoformat().replace("+00:00", "Z"),
        "evidence_digest": DRAFT_DIGEST,
        "callback_payload_digest": CALLBACK_DIGEST,
    }

    result = service.accept_tombstone_callback(SCOPE, payload=payload)

    assert result.callback_receipt_ref.startswith("GTC-")
    assert len(repository.callbacks) == 1
    assert "object_ref" not in payload
    assert "content" not in payload
    with pytest.raises(ValueError, match="callback payload"):
        service.accept_tombstone_callback(SCOPE, payload={**payload, "object_ref": "forbidden"})


def test_sensitive_identifiers_are_redacted_from_repr() -> None:
    value = repr(_authority()) + repr(_lease())
    assert DRAFT_EVIDENCE not in value
    assert DRAFT_REF not in value
    assert DRAFT_DIGEST not in value
