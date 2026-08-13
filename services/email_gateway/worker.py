"""Fenced fake-provider Send Outbox worker with fail-closed reconciliation."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import TenantScope, ValidationError
from .outbound import ApprovedOutboundEnvelope, EmailSendRepository
from .provider import (
    EmailProvider,
    ProviderOutcome,
    ProviderSubmission,
    ProviderSubmissionResult,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerAuthorityState:
    emergency_stop_active: bool
    execution_enabled: bool
    command_unexpired: bool
    identity_active: bool
    mailbox_revision_current: bool
    route_authority_current: bool

    @property
    def allowed(self) -> bool:
        return (
            not self.emergency_stop_active
            and self.execution_enabled
            and self.command_unexpired
            and self.identity_active
            and self.mailbox_revision_current
            and self.route_authority_current
        )

    @property
    def safe_code(self) -> str:
        if self.emergency_stop_active:
            return "emergency_stop_active"
        if not self.execution_enabled:
            return "external_send_disabled"
        if not self.command_unexpired:
            return "approval_expired"
        if not self.identity_active:
            return "identity_revoked"
        if not self.mailbox_revision_current:
            return "mailbox_revision_drift"
        if not self.route_authority_current:
            return "route_authority_drift"
        return "authority_current"


@dataclass(frozen=True, slots=True)
class WorkerResult:
    state: str
    send_outbox_ref: str | None = None


class EmailSendWorker:
    def __init__(
        self,
        *,
        repository: EmailSendRepository,
        provider: EmailProvider,
        worker_id: str,
        clock: Callable[[], datetime],
        authority_check: Callable[[ApprovedOutboundEnvelope], WorkerAuthorityState],
        lease_duration: timedelta,
        pre_claim_check: Callable[[], bool] | None = None,
        runtime_stop_reader: Callable[[], str | None] | None = None,
    ) -> None:
        if (
            not worker_id
            or "@" in worker_id
            or lease_duration <= timedelta(0)
            or lease_duration > timedelta(minutes=5)
        ):
            raise ValueError("invalid email send worker configuration")
        self._repository = repository
        self._provider = provider
        self._worker_id = worker_id
        self._clock = clock
        self._authority_check = authority_check
        self._lease_duration = lease_duration
        self._pre_claim_check = pre_claim_check or (lambda: True)
        self._runtime_stop_reader = runtime_stop_reader or (lambda: None)

    def run_once(self, scope: TenantScope) -> WorkerResult:
        if self._runtime_stop() is not None or not self._pre_claim_check():
            return WorkerResult("idle")
        now = self._clock()
        claim = self._repository.claim(
            scope,
            worker_id=self._worker_id,
            now=now,
            lease_duration=self._lease_duration,
        )
        if claim is None:
            return WorkerResult("idle")
        authority = self._authority_check(claim.snapshot.envelope)
        if not authority.allowed:
            snapshot = self._repository.mark_authority_review(
                scope,
                claim,
                safe_code=authority.safe_code,
                now=self._clock(),
            )
            return WorkerResult(snapshot.state, snapshot.send_outbox_ref)
        runtime_stop = self._runtime_stop()
        if runtime_stop is not None:
            snapshot = self._repository.mark_authority_review(
                scope,
                claim,
                safe_code=runtime_stop,
                now=self._clock(),
            )
            return WorkerResult(snapshot.state, snapshot.send_outbox_ref)
        envelope = claim.snapshot.envelope
        submission = ProviderSubmission(
            stable_provider_request_id=claim.stable_provider_request_id,
            send_outbox_ref=claim.snapshot.send_outbox_ref,
            final_mime_evidence_ref=envelope.final_mime_evidence_ref,
            final_mime_digest=envelope.final_mime_digest,
            participant_count=len(envelope.participants),
        )
        try:
            result = self._provider.submit(submission)
        except Exception:
            snapshot = self._repository.mark_uncertain(
                scope,
                claim,
                safe_code="provider_submission_uncertain",
                now=self._clock(),
            )
            return WorkerResult(snapshot.state, snapshot.send_outbox_ref)
        if result.outcome in {ProviderOutcome.UNCERTAIN, ProviderOutcome.UNKNOWN}:
            snapshot = self._repository.mark_uncertain(
                scope,
                claim,
                safe_code=result.safe_code,
                now=self._clock(),
            )
        else:
            snapshot = self._repository.finish(
                scope,
                claim,
                outcome=result.outcome.value,
                safe_code=result.safe_code,
                provider_receipt_ref=result.provider_receipt_ref,
                now=self._clock(),
            )
        return WorkerResult(snapshot.state, snapshot.send_outbox_ref)

    def reconcile(self, scope: TenantScope, send_outbox_ref: str) -> WorkerResult:
        if self._runtime_stop() is not None:
            return WorkerResult("idle", send_outbox_ref)
        snapshot = self._repository.get(scope, send_outbox_ref)
        if snapshot is None or snapshot.state != "reconciliation_required":
            raise ValidationError("outbox is not awaiting reconciliation")
        stable_id = self._repository.stable_provider_request_id(scope, send_outbox_ref)
        try:
            lookup = self._provider.lookup(stable_id)
        except Exception:
            lookup = ProviderSubmissionResult(
                outcome=ProviderOutcome.UNKNOWN,
                safe_code="reconciliation_lookup_unavailable",
                provider_receipt_ref=None,
            )
        snapshot = self._repository.record_reconciliation(
            scope,
            send_outbox_ref,
            outcome=lookup.outcome.value,
            safe_code=lookup.safe_code,
            provider_receipt_ref=lookup.provider_receipt_ref,
            now=self._clock(),
        )
        return WorkerResult(snapshot.state, snapshot.send_outbox_ref)

    def consume_manual_reconciliations(
        self,
        scope: TenantScope,
        send_outbox_refs: Iterable[str],
    ) -> tuple[WorkerResult, ...]:
        """Consume a bounded explicit list; repository state is the reconciliation fence."""

        if isinstance(send_outbox_refs, str | bytes):
            raise ValidationError("manual reconciliation input is invalid")
        unique: list[str] = []
        seen: set[str] = set()
        for position, reference in enumerate(send_outbox_refs, start=1):
            if position > 50:
                raise ValidationError("manual reconciliation input is unbounded")
            if not isinstance(reference, str) or not reference.startswith("SOB-"):
                raise ValidationError("manual reconciliation reference is invalid")
            if reference not in seen:
                seen.add(reference)
                unique.append(reference)
        return tuple(self.reconcile(scope, reference) for reference in unique)

    def _runtime_stop(self) -> str | None:
        try:
            safe_code = self._runtime_stop_reader()
        except Exception:
            return "emergency_stop_active"
        if safe_code in {None, "emergency_stop_active", "external_send_disabled"}:
            return safe_code
        return "emergency_stop_active"


__all__ = ["EmailSendWorker", "WorkerAuthorityState", "WorkerResult"]
