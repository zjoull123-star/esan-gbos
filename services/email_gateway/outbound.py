"""Closed ApprovedCommand ingest and atomic immutable Send Outbox creation."""

from __future__ import annotations

import hmac
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from threading import RLock
from types import MappingProxyType, SimpleNamespace
from typing import Any, Protocol

from services.action_guard.email_send import EmailSendAuthorityReceipt
from services.action_guard.models import VerifiedEmailSendCommand
from services.action_guard.policy import ActionGuard

from .models import (
    IdempotencyConflict,
    TenantScope,
    ValidationError,
    require_scope,
    stable_ref,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class CommandPublication:
    publication_ref: str
    attempt: int
    generation: int
    fence_token: str
    payload_digest: str

    def __post_init__(self) -> None:
        if (
            not self.publication_ref.startswith("PUB-")
            or not self.fence_token.startswith("FNC-")
            or not isinstance(self.attempt, int)
            or isinstance(self.attempt, bool)
            or self.attempt < 1
            or not isinstance(self.generation, int)
            or isinstance(self.generation, bool)
            or self.generation < 1
            or not self.payload_digest.startswith("sha256:")
            or len(self.payload_digest) != 71
        ):
            raise ValidationError("invalid command publication")


@dataclass(frozen=True, slots=True, kw_only=True)
class OutboundParticipant:
    address_role: str
    opaque_address_ref: str
    identity_mapping_ref: str | None
    identity_mapping_revision: int | None

    def __repr__(self) -> str:
        return (
            "OutboundParticipant("
            f"address_role={self.address_role!r}, opaque_address_ref=<redacted>, "
            "identity_mapping_ref=<redacted>, "
            f"identity_mapping_revision={self.identity_mapping_revision!r})"
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ApprovedOutboundEnvelope:
    site_id: str
    processing_purpose: str
    command_ref: str
    payload_digest: str
    idempotency_key: str
    stable_client_request_id: str
    review_case_ref: str
    review_case_revision: int
    review_policy_version: str
    approval_expires_at: str
    actor_user_ref: str
    delegated_approver_user_ref: str
    team_ref: str
    mailbox_ref: str
    mailbox_config_revision: int
    inbox_item_ref: str
    inbox_item_revision: int
    conversation_ref: str
    conversation_revision: int
    reply_draft_ref: str
    reply_draft_revision: int
    reply_draft_digest: str
    participants: tuple[OutboundParticipant, ...]
    party_ref: str
    party_revision: int
    team_revision: int
    owner_user_ref: str
    owner_eligibility_revision: str
    final_mime_evidence_ref: str
    final_mime_digest: str
    evidence_refs: tuple[str, ...]
    request_id: str
    approved_command: Mapping[str, object]

    def __repr__(self) -> str:
        return (
            "ApprovedOutboundEnvelope("
            f"site_id={self.site_id!r}, processing_purpose={self.processing_purpose!r}, "
            f"command_ref={self.command_ref!r}, mailbox_ref={self.mailbox_ref!r}, "
            f"inbox_item_ref={self.inbox_item_ref!r}, participant_count={len(self.participants)}, "
            f"evidence_count={len(self.evidence_refs)}, approved_command=<redacted>)"
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CommandIngestReceipt:
    command_receipt_ref: str
    send_outbox_ref: str
    payload_digest: str

    def to_wire(self) -> dict[str, str]:
        return {
            "command_receipt_ref": self.command_receipt_ref,
            "send_outbox_ref": self.send_outbox_ref,
            "payload_digest": self.payload_digest,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class CommandReceiptRecord:
    receipt: CommandIngestReceipt
    publication: CommandPublication
    envelope: ApprovedOutboundEnvelope
    received_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class SendOutboxSnapshot:
    send_outbox_ref: str
    command_receipt_ref: str
    envelope: ApprovedOutboundEnvelope
    state: str
    created_at: datetime

    def __repr__(self) -> str:
        return (
            "SendOutboxSnapshot("
            f"send_outbox_ref={self.send_outbox_ref!r}, "
            f"command_receipt_ref={self.command_receipt_ref!r}, state={self.state!r}, "
            f"created_at={self.created_at!r}, envelope={self.envelope!r})"
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class OutboxClaim:
    snapshot: SendOutboxSnapshot
    worker_id: str
    attempt: int
    generation: int
    fence_token: str
    lease_expires_at: datetime
    stable_provider_request_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class AttemptEvent:
    event_ref: str
    send_outbox_ref: str
    attempt: int
    generation: int
    fence_token: str
    stable_provider_request_id: str
    event_kind: str
    occurred_at: datetime
    request_digest: str
    safe_code: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderReceiptRecord:
    receipt_ref: str
    send_outbox_ref: str
    attempt: int
    outcome: str
    safe_code: str
    provider_receipt_ref: str | None
    observed_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class ReconciliationReceiptRecord:
    receipt_ref: str
    send_outbox_ref: str
    stable_provider_request_id: str
    outcome: str
    observed_at: datetime


class OutboundRepository(Protocol):
    def replay(
        self,
        scope: TenantScope,
        publication_ref: str,
        payload_digest: str,
    ) -> CommandIngestReceipt | None: ...

    def accept_verified(
        self,
        scope: TenantScope,
        *,
        publication: CommandPublication,
        command: Mapping[str, Any],
        verified: VerifiedEmailSendCommand,
        received_at: datetime,
    ) -> CommandIngestReceipt: ...


class EmailSendRepository(Protocol):
    def get(self, scope: TenantScope, send_outbox_ref: str) -> SendOutboxSnapshot | None: ...

    def claim(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> OutboxClaim | None: ...

    def finish(
        self,
        scope: TenantScope,
        claim: OutboxClaim,
        *,
        outcome: str,
        safe_code: str,
        provider_receipt_ref: str | None,
        now: datetime,
    ) -> SendOutboxSnapshot: ...

    def mark_uncertain(
        self,
        scope: TenantScope,
        claim: OutboxClaim,
        *,
        safe_code: str,
        now: datetime,
    ) -> SendOutboxSnapshot: ...

    def mark_authority_review(
        self,
        scope: TenantScope,
        claim: OutboxClaim,
        *,
        safe_code: str,
        now: datetime,
    ) -> SendOutboxSnapshot: ...

    def record_reconciliation(
        self,
        scope: TenantScope,
        send_outbox_ref: str,
        *,
        outcome: str,
        safe_code: str,
        provider_receipt_ref: str | None,
        now: datetime,
    ) -> SendOutboxSnapshot: ...

    def stable_provider_request_id(self, scope: TenantScope, send_outbox_ref: str) -> str: ...


AuthorityResolver = Callable[
    [TenantScope, CommandPublication, Mapping[str, Any]],
    EmailSendAuthorityReceipt,
]


class CommandIngestService:
    def __init__(
        self,
        *,
        repository: OutboundRepository,
        action_guard: ActionGuard,
        authority_resolver: AuthorityResolver,
        clock: Callable[[], datetime],
    ) -> None:
        self._repository = repository
        self._guard = action_guard
        self._authority = authority_resolver
        self._clock = clock

    def accept(
        self,
        scope: TenantScope,
        *,
        publication: CommandPublication,
        command: Mapping[str, Any],
    ) -> CommandIngestReceipt:
        if command.get("site_id") != scope.site_id:
            raise ValidationError("command site mismatch")
        if command.get("processing_purpose") != scope.processing_purpose:
            raise ValidationError("command purpose mismatch")
        command_digest = command.get("payload_sha256")
        if not isinstance(command_digest, str) or not hmac.compare_digest(
            publication.payload_digest, f"sha256:{command_digest}"
        ):
            raise IdempotencyConflict("command replay drift")
        replay = self._repository.replay(
            scope,
            publication.publication_ref,
            command_digest,
        )
        if replay is not None:
            return replay
        now = self._clock()
        authority = self._authority(scope, publication, command)
        verified = self._guard.verify_email_send(command, authority=authority, now=now)
        receipt = self._repository.accept_verified(
            scope,
            publication=publication,
            command=command,
            verified=verified,
            received_at=now,
        )
        self._guard.verify_email_send_result(verified, receipt.to_wire())
        return receipt


class InMemoryOutboundRepository:
    """Atomic reference implementation used only by offline tests and local fakes."""

    def __init__(
        self,
        *,
        transaction_failure_injector: Callable[[str], None] | None = None,
    ) -> None:
        self._receipts: dict[tuple[str, str], CommandReceiptRecord] = {}
        self._outboxes: dict[tuple[str, str], SendOutboxSnapshot] = {}
        self._by_publication: dict[tuple[str, str], CommandIngestReceipt] = {}
        self._leases: dict[tuple[str, str], OutboxClaim] = {}
        self._attempt_events: dict[tuple[str, str], list[AttemptEvent]] = {}
        self._provider_receipts: dict[tuple[str, str], list[ProviderReceiptRecord]] = {}
        self._reconciliation_receipts: dict[tuple[str, str], list[ReconciliationReceiptRecord]] = {}
        self._transaction_failure_injector = transaction_failure_injector
        self._lock = RLock()

    def accept_verified(
        self,
        scope: TenantScope,
        *,
        publication: CommandPublication,
        command: Mapping[str, Any],
        verified: VerifiedEmailSendCommand,
        received_at: datetime,
    ) -> CommandIngestReceipt:
        require_scope(
            scope,
            site_id=str(command["site_id"]),
            processing_purpose=str(command["processing_purpose"]),
        )
        replay_key = (scope.site_id, publication.publication_ref)
        with self._lock:
            replay = self._by_publication.get(replay_key)
            if replay is not None:
                if replay.payload_digest != verified.payload_digest:
                    raise IdempotencyConflict("command replay drift")
                return replay
            receipt = CommandIngestReceipt(
                command_receipt_ref=stable_ref(
                    "ECR", scope.site_id, publication.publication_ref, verified.payload_digest
                ),
                send_outbox_ref=stable_ref(
                    "SOB", scope.site_id, verified.command_ref, verified.idempotency_key
                ),
                payload_digest=verified.payload_digest,
            )
            envelope = _envelope(command, verified)
            receipt_key = (scope.site_id, receipt.command_receipt_ref)
            outbox_key = (scope.site_id, receipt.send_outbox_ref)
            if outbox_key in self._outboxes:
                raise IdempotencyConflict("command replay drift")
            try:
                self._receipts[receipt_key] = CommandReceiptRecord(
                    receipt=receipt,
                    publication=publication,
                    envelope=envelope,
                    received_at=received_at,
                )
                self._fail("after_command_receipt")
                self._outboxes[outbox_key] = SendOutboxSnapshot(
                    send_outbox_ref=receipt.send_outbox_ref,
                    command_receipt_ref=receipt.command_receipt_ref,
                    envelope=envelope,
                    state="queued",
                    created_at=received_at,
                )
                self._fail("after_send_outbox")
                self._by_publication[replay_key] = receipt
                self._fail("after_idempotency_receipt")
            except Exception:
                self._receipts.pop(receipt_key, None)
                self._outboxes.pop(outbox_key, None)
                self._by_publication.pop(replay_key, None)
                raise
            return receipt

    def replay(
        self,
        scope: TenantScope,
        publication_ref: str,
        payload_digest: str,
    ) -> CommandIngestReceipt | None:
        replay = self._by_publication.get((scope.site_id, publication_ref))
        if replay is None:
            return None
        if not hmac.compare_digest(replay.payload_digest, payload_digest):
            raise IdempotencyConflict("command replay drift")
        return replay

    def get(self, scope: TenantScope, send_outbox_ref: str) -> SendOutboxSnapshot | None:
        return self._outboxes.get((scope.site_id, send_outbox_ref))

    def command_receipt_count(self, scope: TenantScope) -> int:
        return sum(site == scope.site_id for site, _ref in self._receipts)

    def outbox_count(self, scope: TenantScope) -> int:
        return sum(site == scope.site_id for site, _ref in self._outboxes)

    def claim(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> OutboxClaim | None:
        with self._lock:
            for key, snapshot in sorted(self._outboxes.items()):
                if key[0] != scope.site_id:
                    continue
                prior = self._leases.get(key)
                if snapshot.state == "leased" and prior is not None:
                    if prior.lease_expires_at <= now:
                        self._outboxes[key] = replace(snapshot, state="reconciliation_required")
                        self._leases.pop(key, None)
                    continue
                if snapshot.state != "queued":
                    continue
                attempt = 1 + self.attempt_count(scope, snapshot.send_outbox_ref)
                generation = 1 + max(
                    (event.generation for event in self._attempt_events.get(key, ())),
                    default=0,
                )
                stable_request = stable_ref(
                    "PRQ",
                    scope.site_id,
                    snapshot.envelope.stable_client_request_id,
                )
                fence = stable_ref(
                    "FNC",
                    scope.site_id,
                    snapshot.send_outbox_ref,
                    worker_id,
                    str(generation),
                    now.isoformat(),
                )
                claimed_snapshot = replace(snapshot, state="leased")
                claim = OutboxClaim(
                    snapshot=claimed_snapshot,
                    worker_id=worker_id,
                    attempt=attempt,
                    generation=generation,
                    fence_token=fence,
                    lease_expires_at=now + lease_duration,
                    stable_provider_request_id=stable_request,
                )
                self._outboxes[key] = claimed_snapshot
                self._leases[key] = claim
                self._attempt_events.setdefault(key, []).append(
                    AttemptEvent(
                        event_ref=stable_ref("ATE", fence, "started"),
                        send_outbox_ref=snapshot.send_outbox_ref,
                        attempt=attempt,
                        generation=generation,
                        fence_token=fence,
                        stable_provider_request_id=stable_request,
                        event_kind="started",
                        occurred_at=now,
                        request_digest=snapshot.envelope.final_mime_digest,
                    )
                )
                return claim
        return None

    def finish(
        self,
        scope: TenantScope,
        claim: OutboxClaim,
        *,
        outcome: str,
        safe_code: str,
        provider_receipt_ref: str | None,
        now: datetime,
    ) -> SendOutboxSnapshot:
        states = {
            "accepted": "provider_accepted",
            "delivered": "delivered",
            "bounced": "bounced",
            "permanently_rejected": "provider_rejected",
        }
        state = states.get(outcome)
        if state is None:
            raise ValidationError("invalid provider completion")
        key = (scope.site_id, claim.snapshot.send_outbox_ref)
        with self._lock:
            self._require_claim(key, claim, now)
            snapshot = replace(self._outboxes[key], state=state)
            self._attempt_events[key].append(
                AttemptEvent(
                    event_ref=stable_ref("ATE", claim.fence_token, "completed"),
                    send_outbox_ref=snapshot.send_outbox_ref,
                    attempt=claim.attempt,
                    generation=claim.generation,
                    fence_token=claim.fence_token,
                    stable_provider_request_id=claim.stable_provider_request_id,
                    event_kind="completed",
                    occurred_at=now,
                    request_digest=snapshot.envelope.final_mime_digest,
                    safe_code=safe_code,
                )
            )
            self._provider_receipts.setdefault(key, []).append(
                ProviderReceiptRecord(
                    receipt_ref=stable_ref(
                        "PRC", claim.fence_token, outcome, provider_receipt_ref or "none"
                    ),
                    send_outbox_ref=snapshot.send_outbox_ref,
                    attempt=claim.attempt,
                    outcome=outcome,
                    safe_code=safe_code,
                    provider_receipt_ref=provider_receipt_ref,
                    observed_at=now,
                )
            )
            self._outboxes[key] = snapshot
            self._leases.pop(key, None)
            return snapshot

    def mark_uncertain(
        self,
        scope: TenantScope,
        claim: OutboxClaim,
        *,
        safe_code: str,
        now: datetime,
    ) -> SendOutboxSnapshot:
        key = (scope.site_id, claim.snapshot.send_outbox_ref)
        with self._lock:
            self._require_claim(key, claim, now, allow_expired=True)
            snapshot = replace(self._outboxes[key], state="reconciliation_required")
            self._attempt_events[key].append(
                AttemptEvent(
                    event_ref=stable_ref("ATE", claim.fence_token, "uncertain"),
                    send_outbox_ref=snapshot.send_outbox_ref,
                    attempt=claim.attempt,
                    generation=claim.generation,
                    fence_token=claim.fence_token,
                    stable_provider_request_id=claim.stable_provider_request_id,
                    event_kind="uncertain",
                    occurred_at=now,
                    request_digest=snapshot.envelope.final_mime_digest,
                    safe_code=safe_code,
                )
            )
            self._outboxes[key] = snapshot
            self._leases.pop(key, None)
            return snapshot

    def mark_authority_review(
        self,
        scope: TenantScope,
        claim: OutboxClaim,
        *,
        safe_code: str,
        now: datetime,
    ) -> SendOutboxSnapshot:
        key = (scope.site_id, claim.snapshot.send_outbox_ref)
        with self._lock:
            self._require_claim(key, claim, now, allow_expired=True)
            snapshot = replace(self._outboxes[key], state="authority_review_required")
            self._attempt_events[key].append(
                AttemptEvent(
                    event_ref=stable_ref("ATE", claim.fence_token, "authority"),
                    send_outbox_ref=snapshot.send_outbox_ref,
                    attempt=claim.attempt,
                    generation=claim.generation,
                    fence_token=claim.fence_token,
                    stable_provider_request_id=claim.stable_provider_request_id,
                    event_kind="authority_rejected",
                    occurred_at=now,
                    request_digest=snapshot.envelope.final_mime_digest,
                    safe_code=safe_code,
                )
            )
            self._outboxes[key] = snapshot
            self._leases.pop(key, None)
            return snapshot

    def record_reconciliation(
        self,
        scope: TenantScope,
        send_outbox_ref: str,
        *,
        outcome: str,
        safe_code: str,
        provider_receipt_ref: str | None,
        now: datetime,
    ) -> SendOutboxSnapshot:
        terminal_states = {
            "accepted": "provider_accepted",
            "delivered": "delivered",
            "bounced": "bounced",
            "permanently_rejected": "provider_rejected",
        }
        if outcome not in {"not_submitted", "unknown", *terminal_states}:
            raise ValidationError("invalid reconciliation outcome")
        key = (scope.site_id, send_outbox_ref)
        with self._lock:
            snapshot = self._outboxes.get(key)
            if snapshot is None or snapshot.state != "reconciliation_required":
                raise ValidationError("outbox is not awaiting reconciliation")
            stable_request_id = self.stable_provider_request_id(scope, send_outbox_ref)
            self._reconciliation_receipts.setdefault(key, []).append(
                ReconciliationReceiptRecord(
                    receipt_ref=stable_ref("RCR", send_outbox_ref, outcome, now.isoformat()),
                    send_outbox_ref=send_outbox_ref,
                    stable_provider_request_id=stable_request_id,
                    outcome=outcome,
                    observed_at=now,
                )
            )
            if outcome == "not_submitted":
                snapshot = replace(snapshot, state="queued")
                self._outboxes[key] = snapshot
            elif outcome in terminal_states:
                snapshot = replace(snapshot, state=terminal_states[outcome])
                self._provider_receipts.setdefault(key, []).append(
                    ProviderReceiptRecord(
                        receipt_ref=stable_ref(
                            "PRC",
                            stable_request_id,
                            "reconciliation",
                            outcome,
                            provider_receipt_ref or "none",
                        ),
                        send_outbox_ref=send_outbox_ref,
                        attempt=max(1, self.attempt_count(scope, send_outbox_ref)),
                        outcome=outcome,
                        safe_code=safe_code,
                        provider_receipt_ref=provider_receipt_ref,
                        observed_at=now,
                    )
                )
                self._outboxes[key] = snapshot
            return snapshot

    def stable_provider_request_id(self, scope: TenantScope, send_outbox_ref: str) -> str:
        snapshot = self.get(scope, send_outbox_ref)
        if snapshot is None:
            raise ValidationError("outbox not found")
        return stable_ref("PRQ", scope.site_id, snapshot.envelope.stable_client_request_id)

    def attempt_count(self, scope: TenantScope, send_outbox_ref: str) -> int:
        events = self._attempt_events.get((scope.site_id, send_outbox_ref), ())
        return len({event.attempt for event in events})

    def receipt_count(self, scope: TenantScope, send_outbox_ref: str) -> int:
        return len(self._provider_receipts.get((scope.site_id, send_outbox_ref), ()))

    def _require_claim(
        self,
        key: tuple[str, str],
        claim: OutboxClaim,
        now: datetime,
        *,
        allow_expired: bool = False,
    ) -> None:
        active = self._leases.get(key)
        if (
            active != claim
            or self._outboxes.get(key, claim.snapshot).state != "leased"
            or (not allow_expired and claim.lease_expires_at < now)
        ):
            raise IdempotencyConflict("outbox lease fence conflict")

    def _fail(self, phase: str) -> None:
        if self._transaction_failure_injector is not None:
            self._transaction_failure_injector(phase)


def _envelope(
    command: Mapping[str, Any],
    verified: VerifiedEmailSendCommand,
) -> ApprovedOutboundEnvelope:
    participants = tuple(
        OutboundParticipant(
            address_role=item["address_role"],
            opaque_address_ref=item["opaque_address_ref"],
            identity_mapping_ref=item.get("identity_mapping_ref"),
            identity_mapping_revision=item.get("identity_mapping_revision"),
        )
        for item in command["participants"]
    )
    detached = _freeze(deepcopy(dict(command)))
    if not isinstance(detached, Mapping):  # pragma: no cover - construction invariant
        raise RuntimeError("command detachment failed")
    return ApprovedOutboundEnvelope(
        site_id=command["site_id"],
        processing_purpose=command["processing_purpose"],
        command_ref=verified.command_ref,
        payload_digest=verified.payload_digest,
        idempotency_key=verified.idempotency_key,
        stable_client_request_id=verified.stable_client_request_id,
        review_case_ref=command["review_case_ref"],
        review_case_revision=command["review_case_revision"],
        review_policy_version=command["review_policy_version"],
        approval_expires_at=command["approval_expires_at"],
        actor_user_ref=command["actor_user_ref"],
        delegated_approver_user_ref=command["delegated_approver_user_ref"],
        team_ref=command["team_ref"],
        mailbox_ref=command["mailbox_ref"],
        mailbox_config_revision=command["mailbox_config_revision"],
        inbox_item_ref=command["inbox_item_ref"],
        inbox_item_revision=command["inbox_item_revision"],
        conversation_ref=command["conversation_ref"],
        conversation_revision=command["conversation_revision"],
        reply_draft_ref=command["reply_draft_ref"],
        reply_draft_revision=command["reply_draft_revision"],
        reply_draft_digest=command["reply_draft_digest"],
        participants=participants,
        party_ref=command["party_ref"],
        party_revision=command["party_revision"],
        team_revision=command["team_revision"],
        owner_user_ref=command["owner_user_ref"],
        owner_eligibility_revision=command["owner_eligibility_revision"],
        final_mime_evidence_ref=command["final_mime_evidence_ref"],
        final_mime_digest=command["final_mime_digest"],
        evidence_refs=tuple(command["evidence_refs"]),
        request_id=command["request_id"],
        approved_command=detached,
    )


def _restore_envelope(
    command: Mapping[str, Any],
    *,
    payload_digest: str,
) -> ApprovedOutboundEnvelope:
    """Restore the immutable executor-verified envelope without re-authorizing it."""

    verified = SimpleNamespace(
        command_ref=str(command["command_id"]),
        payload_digest=payload_digest,
        idempotency_key=str(command["idempotency_key"]),
        stable_client_request_id=str(command["stable_client_request_id"]),
    )
    return _envelope(command, verified)  # type: ignore[arg-type]


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    return value


__all__ = [
    "ApprovedOutboundEnvelope",
    "CommandIngestReceipt",
    "CommandIngestService",
    "CommandPublication",
    "EmailSendRepository",
    "InMemoryOutboundRepository",
    "OutboundParticipant",
    "OutboundRepository",
    "OutboxClaim",
    "SendOutboxSnapshot",
]
