from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from services.action_guard.models import VerifiedEmailSendCommand

from .models import (
    IdempotencyConflict,
    OutboundNotAuthorized,
    TenantScope,
    ValidationError,
    stable_ref,
)
from .outbound import (
    ApprovedOutboundEnvelope,
    CommandIngestReceipt,
    CommandPublication,
    OutboundRepository,
    OutboxClaim,
    SendOutboxSnapshot,
    _envelope,
    _restore_envelope,
)
from .postgres import (
    Connection,
    redacted_database_errors,
    require_database_role,
    site_transaction,
)


class DisabledSendOutboxRepository:
    """Schema placeholder that cannot create external work before Chunk 4."""

    def __init__(self, *, outbound_enabled: bool) -> None:
        self.outbound_enabled = outbound_enabled

    def insert(self, scope: TenantScope, command: object) -> None:
        raise OutboundNotAuthorized("outbound_not_authorized")


class PostgresSendOutboxRepository(OutboundRepository):
    """Executor-only atomic command receipt and immutable Send Outbox writer."""

    def __init__(self, connection: Connection, *, actual_database_role: str) -> None:
        require_database_role(actual_database_role, "gbos_email_command_executor")
        self._connection = connection

    def __repr__(self) -> str:
        return "PostgresSendOutboxRepository(connection=<redacted>)"

    def replay(
        self,
        scope: TenantScope,
        publication_ref: str,
        payload_digest: str,
    ) -> CommandIngestReceipt | None:
        del scope, publication_ref, payload_digest
        # Replay lookup is deliberately folded into accept_verified so receipt
        # lookup, drift detection, and both inserts share one durable transaction.
        return None

    def accept_verified(
        self,
        scope: TenantScope,
        *,
        publication: CommandPublication,
        command: Mapping[str, Any],
        verified: VerifiedEmailSendCommand,
        received_at: datetime,
    ) -> CommandIngestReceipt:
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
        persisted_envelope = {
            key: value for key, value in command.items() if key != "payload_sha256"
        }
        with redacted_database_errors(), site_transaction(self._connection, scope) as db:
            db.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"email-command:{scope.site_id}:{publication.publication_ref}",),
            )
            db.execute(
                """
                SELECT command.command_receipt_ref, send.send_ref, command.payload_digest
                  FROM email_gateway.command_inbox AS command
                  JOIN email_gateway.send_outbox AS send
                    ON send.site_id = command.site_id
                   AND send.command_receipt_ref = command.command_receipt_ref
                 WHERE command.site_id = %s
                   AND command.publication_ref = %s
                """,
                (scope.site_id, publication.publication_ref),
            )
            replay = db.fetchone()
            if replay is not None:
                if str(replay[2]) != verified.payload_digest:
                    from .models import IdempotencyConflict

                    raise IdempotencyConflict("command replay drift")
                return CommandIngestReceipt(
                    command_receipt_ref=str(replay[0]),
                    send_outbox_ref=str(replay[1]),
                    payload_digest=str(replay[2]),
                )
            db.execute(
                """
                INSERT INTO email_gateway.command_inbox (
                    site_id, processing_purpose, command_receipt_ref,
                    publication_ref, publication_attempt, publication_generation,
                    publication_fence_token, command_ref, idempotency_key,
                    stable_client_request_id, payload_digest, approved_envelope,
                    received_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s
                )
                """,
                (
                    scope.site_id,
                    scope.processing_purpose,
                    receipt.command_receipt_ref,
                    publication.publication_ref,
                    publication.attempt,
                    publication.generation,
                    publication.fence_token,
                    verified.command_ref,
                    verified.idempotency_key,
                    verified.stable_client_request_id,
                    verified.payload_digest,
                    json.dumps(
                        persisted_envelope,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    received_at,
                ),
            )
            db.execute(
                """
                INSERT INTO email_gateway.send_outbox (
                    site_id, send_ref, mailbox_ref, inbox_item_ref,
                    conversation_ref, draft_ref, approved_command_ref,
                    approved_payload_digest, final_mime_evidence_ref,
                    final_mime_digest, state, processing_purpose,
                    command_receipt_ref, idempotency_key,
                    stable_client_request_id, review_case_ref,
                    review_case_revision, approval_expires_at, approved_envelope,
                    created_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'queued',
                    %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s
                )
                """,
                (
                    scope.site_id,
                    receipt.send_outbox_ref,
                    envelope.mailbox_ref,
                    envelope.inbox_item_ref,
                    envelope.conversation_ref,
                    envelope.reply_draft_ref,
                    envelope.command_ref,
                    f"sha256:{envelope.payload_digest}",
                    envelope.final_mime_evidence_ref,
                    envelope.final_mime_digest,
                    scope.processing_purpose,
                    receipt.command_receipt_ref,
                    envelope.idempotency_key,
                    envelope.stable_client_request_id,
                    envelope.review_case_ref,
                    envelope.review_case_revision,
                    envelope.approval_expires_at,
                    json.dumps(
                        persisted_envelope,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    received_at,
                ),
            )
            db.execute(
                """
                INSERT INTO email_gateway.send_outbox_state (
                    site_id, send_outbox_ref, state, attempt, generation, updated_at
                ) VALUES (%s, %s, 'queued', 0, 0, %s)
                """,
                (scope.site_id, receipt.send_outbox_ref, received_at),
            )
        return receipt


class PostgresEmailSendRepository:
    """Worker-only fenced durable state/attempt/receipt repository."""

    _TERMINAL_STATES = {
        "accepted": "provider_accepted",
        "delivered": "delivered",
        "bounced": "bounced",
        "permanently_rejected": "provider_rejected",
    }

    def __init__(self, connection: Connection, *, actual_database_role: str) -> None:
        require_database_role(actual_database_role, "gbos_email_send_worker")
        self._connection = connection

    def __repr__(self) -> str:
        return "PostgresEmailSendRepository(connection=<redacted>)"

    def get(self, scope: TenantScope, send_outbox_ref: str) -> SendOutboxSnapshot | None:
        with redacted_database_errors(), site_transaction(self._connection, scope) as db:
            db.execute(
                """
                SELECT outbox.send_ref, outbox.command_receipt_ref,
                       outbox.approved_payload_digest, outbox.approved_envelope,
                       state.state, outbox.created_at
                  FROM email_gateway.send_outbox AS outbox
                  JOIN email_gateway.send_outbox_state AS state
                    ON state.site_id = outbox.site_id
                   AND state.send_outbox_ref = outbox.send_ref
                 WHERE outbox.site_id = %s AND outbox.send_ref = %s
                   AND outbox.processing_purpose = %s
                """,
                (scope.site_id, send_outbox_ref, scope.processing_purpose),
            )
            row = db.fetchone()
        return None if row is None else self._snapshot(row)

    def claim(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> OutboxClaim | None:
        with redacted_database_errors(), site_transaction(self._connection, scope) as db:
            db.execute(
                """
                UPDATE email_gateway.send_outbox_state
                   SET state = 'reconciliation_required', lease_owner = NULL,
                       fence_token = NULL, lease_expires_at = NULL,
                       safe_code = 'worker_lease_expired', updated_at = %s
                 WHERE site_id = %s AND state = 'leased' AND lease_expires_at <= %s
                   AND EXISTS (
                       SELECT 1 FROM email_gateway.send_outbox AS outbox
                        WHERE outbox.site_id = send_outbox_state.site_id
                          AND outbox.send_ref = send_outbox_state.send_outbox_ref
                          AND outbox.processing_purpose = %s
                   )
                """,
                (now, scope.site_id, now, scope.processing_purpose),
            )
            db.execute(
                """
                SELECT outbox.send_ref, outbox.command_receipt_ref,
                       outbox.approved_payload_digest, outbox.approved_envelope,
                       state.state, outbox.created_at, state.attempt, state.generation
                  FROM email_gateway.send_outbox_state AS state
                  JOIN email_gateway.send_outbox AS outbox
                    ON outbox.site_id = state.site_id
                   AND outbox.send_ref = state.send_outbox_ref
                 WHERE state.site_id = %s AND state.state = 'queued'
                   AND outbox.processing_purpose = %s
                 ORDER BY outbox.created_at, outbox.send_ref
                 FOR UPDATE OF state SKIP LOCKED
                 LIMIT 1
                """,
                (scope.site_id, scope.processing_purpose),
            )
            row = db.fetchone()
            if row is None:
                return None
            snapshot = self._snapshot(row[:6])
            attempt = int(row[6]) + 1
            generation = int(row[7]) + 1
            stable_request_id = stable_ref(
                "PRQ", scope.site_id, snapshot.envelope.stable_client_request_id
            )
            fence_token = stable_ref(
                "FNC",
                scope.site_id,
                snapshot.send_outbox_ref,
                worker_id,
                str(generation),
                now.isoformat(),
            )
            lease_expires_at = now + lease_duration
            db.execute(
                """
                UPDATE email_gateway.send_outbox_state
                   SET state = 'leased', attempt = %s, generation = %s,
                       lease_owner = %s, fence_token = %s, lease_expires_at = %s,
                       safe_code = NULL, updated_at = %s
                 WHERE site_id = %s AND send_outbox_ref = %s AND state = 'queued'
                """,
                (
                    attempt,
                    generation,
                    worker_id,
                    fence_token,
                    lease_expires_at,
                    now,
                    scope.site_id,
                    snapshot.send_outbox_ref,
                ),
            )
            self._insert_attempt(
                db,
                scope,
                send_outbox_ref=snapshot.send_outbox_ref,
                attempt=attempt,
                generation=generation,
                fence_token=fence_token,
                stable_provider_request_id=stable_request_id,
                event_kind="started",
                occurred_at=now,
                request_digest=snapshot.envelope.final_mime_digest,
                safe_code=None,
            )
        leased = SendOutboxSnapshot(
            send_outbox_ref=snapshot.send_outbox_ref,
            command_receipt_ref=snapshot.command_receipt_ref,
            envelope=snapshot.envelope,
            state="leased",
            created_at=snapshot.created_at,
        )
        return OutboxClaim(
            snapshot=leased,
            worker_id=worker_id,
            attempt=attempt,
            generation=generation,
            fence_token=fence_token,
            lease_expires_at=lease_expires_at,
            stable_provider_request_id=stable_request_id,
        )

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
        state = self._TERMINAL_STATES.get(outcome)
        if state is None:
            raise ValidationError("invalid provider completion")
        with redacted_database_errors(), site_transaction(self._connection, scope) as db:
            self._finish_state(db, scope, claim, state=state, safe_code=safe_code, now=now)
            self._insert_attempt(
                db,
                scope,
                send_outbox_ref=claim.snapshot.send_outbox_ref,
                attempt=claim.attempt,
                generation=claim.generation,
                fence_token=claim.fence_token,
                stable_provider_request_id=claim.stable_provider_request_id,
                event_kind="completed",
                occurred_at=now,
                request_digest=claim.snapshot.envelope.final_mime_digest,
                safe_code=safe_code,
            )
            provider_receipt_record_ref = self._insert_provider_receipt(
                db,
                scope,
                send_outbox_ref=claim.snapshot.send_outbox_ref,
                attempt=claim.attempt,
                outcome=outcome,
                safe_code=safe_code,
                provider_receipt_ref=provider_receipt_ref,
                observed_at=now,
                identity=claim.fence_token,
            )
            if outcome in {"accepted", "delivered"}:
                self._create_sent_material_authorities(
                    db,
                    scope,
                    provider_receipt_record_ref=provider_receipt_record_ref,
                )
        return self._claim_snapshot(claim, state)

    def mark_uncertain(
        self,
        scope: TenantScope,
        claim: OutboxClaim,
        *,
        safe_code: str,
        now: datetime,
    ) -> SendOutboxSnapshot:
        return self._close_for_review(
            scope,
            claim,
            state="reconciliation_required",
            event_kind="uncertain",
            safe_code=safe_code,
            now=now,
        )

    def mark_authority_review(
        self,
        scope: TenantScope,
        claim: OutboxClaim,
        *,
        safe_code: str,
        now: datetime,
    ) -> SendOutboxSnapshot:
        return self._close_for_review(
            scope,
            claim,
            state="authority_review_required",
            event_kind="authority_rejected",
            safe_code=safe_code,
            now=now,
        )

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
        if outcome not in {"not_submitted", "unknown", *self._TERMINAL_STATES}:
            raise ValidationError("invalid reconciliation outcome")
        with redacted_database_errors(), site_transaction(self._connection, scope) as db:
            db.execute(
                """
                SELECT outbox.send_ref, outbox.command_receipt_ref,
                       outbox.approved_payload_digest, outbox.approved_envelope,
                       state.state, outbox.created_at, state.attempt
                  FROM email_gateway.send_outbox_state AS state
                  JOIN email_gateway.send_outbox AS outbox
                    ON outbox.site_id = state.site_id
                   AND outbox.send_ref = state.send_outbox_ref
                 WHERE state.site_id = %s AND state.send_outbox_ref = %s
                   AND outbox.processing_purpose = %s
                   AND state.state = 'reconciliation_required'
                 FOR UPDATE OF state
                """,
                (scope.site_id, send_outbox_ref, scope.processing_purpose),
            )
            row = db.fetchone()
            if row is None:
                raise ValidationError("outbox is not awaiting reconciliation")
            snapshot = self._snapshot(row[:6])
            stable_request_id = stable_ref(
                "PRQ", scope.site_id, snapshot.envelope.stable_client_request_id
            )
            db.execute(
                """
                INSERT INTO email_gateway.reconciliation_receipts (
                    site_id, reconciliation_receipt_ref, send_outbox_ref,
                    stable_provider_request_id, lookup_outcome, observed_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    scope.site_id,
                    stable_ref("RCR", send_outbox_ref, outcome, now.isoformat()),
                    send_outbox_ref,
                    stable_request_id,
                    outcome,
                    now,
                ),
            )
            state = (
                "queued"
                if outcome == "not_submitted"
                else self._TERMINAL_STATES.get(outcome, "reconciliation_required")
            )
            if state != "reconciliation_required":
                db.execute(
                    """
                    UPDATE email_gateway.send_outbox_state
                       SET state = %s, safe_code = %s, updated_at = %s
                     WHERE site_id = %s AND send_outbox_ref = %s
                       AND state = 'reconciliation_required'
                    """,
                    (state, safe_code, now, scope.site_id, send_outbox_ref),
                )
            if outcome in self._TERMINAL_STATES:
                provider_receipt_record_ref = self._insert_provider_receipt(
                    db,
                    scope,
                    send_outbox_ref=send_outbox_ref,
                    attempt=max(1, int(row[6])),
                    outcome=outcome,
                    safe_code=safe_code,
                    provider_receipt_ref=provider_receipt_ref,
                    observed_at=now,
                    identity=stable_request_id + ":reconciliation",
                )
                if outcome in {"accepted", "delivered"}:
                    self._create_sent_material_authorities(
                        db,
                        scope,
                        provider_receipt_record_ref=provider_receipt_record_ref,
                    )
        return SendOutboxSnapshot(
            send_outbox_ref=snapshot.send_outbox_ref,
            command_receipt_ref=snapshot.command_receipt_ref,
            envelope=snapshot.envelope,
            state=state,
            created_at=snapshot.created_at,
        )

    def stable_provider_request_id(self, scope: TenantScope, send_outbox_ref: str) -> str:
        snapshot = self.get(scope, send_outbox_ref)
        if snapshot is None:
            raise ValidationError("outbox not found")
        return stable_ref("PRQ", scope.site_id, snapshot.envelope.stable_client_request_id)

    def _close_for_review(
        self,
        scope: TenantScope,
        claim: OutboxClaim,
        *,
        state: str,
        event_kind: str,
        safe_code: str,
        now: datetime,
    ) -> SendOutboxSnapshot:
        with redacted_database_errors(), site_transaction(self._connection, scope) as db:
            self._finish_state(db, scope, claim, state=state, safe_code=safe_code, now=now)
            self._insert_attempt(
                db,
                scope,
                send_outbox_ref=claim.snapshot.send_outbox_ref,
                attempt=claim.attempt,
                generation=claim.generation,
                fence_token=claim.fence_token,
                stable_provider_request_id=claim.stable_provider_request_id,
                event_kind=event_kind,
                occurred_at=now,
                request_digest=claim.snapshot.envelope.final_mime_digest,
                safe_code=safe_code,
            )
        return self._claim_snapshot(claim, state)

    @staticmethod
    def _finish_state(
        db: Any,
        scope: TenantScope,
        claim: OutboxClaim,
        *,
        state: str,
        safe_code: str,
        now: datetime,
    ) -> None:
        db.execute(
            """
            UPDATE email_gateway.send_outbox_state
               SET state = %s, lease_owner = NULL, fence_token = NULL,
                   lease_expires_at = NULL, safe_code = %s, updated_at = %s
             WHERE site_id = %s AND send_outbox_ref = %s AND state = 'leased'
               AND lease_owner = %s AND fence_token = %s AND generation = %s
               AND EXISTS (
                   SELECT 1 FROM email_gateway.send_outbox AS outbox
                    WHERE outbox.site_id = send_outbox_state.site_id
                      AND outbox.send_ref = send_outbox_state.send_outbox_ref
                      AND outbox.processing_purpose = %s
               )
             RETURNING send_outbox_ref
            """,
            (
                state,
                safe_code,
                now,
                scope.site_id,
                claim.snapshot.send_outbox_ref,
                claim.worker_id,
                claim.fence_token,
                claim.generation,
                scope.processing_purpose,
            ),
        )
        if db.fetchone() is None:
            raise IdempotencyConflict("outbox lease fence conflict")

    @staticmethod
    def _insert_attempt(
        db: Any,
        scope: TenantScope,
        *,
        send_outbox_ref: str,
        attempt: int,
        generation: int,
        fence_token: str,
        stable_provider_request_id: str,
        event_kind: str,
        occurred_at: datetime,
        request_digest: str,
        safe_code: str | None,
    ) -> None:
        db.execute(
            """
            INSERT INTO email_gateway.send_attempts (
                site_id, attempt_event_ref, send_outbox_ref, attempt, generation,
                fence_token, stable_provider_request_id, event_kind, occurred_at,
                provider_request_digest, safe_code
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                scope.site_id,
                stable_ref("ATE", fence_token, event_kind),
                send_outbox_ref,
                attempt,
                generation,
                fence_token,
                stable_provider_request_id,
                event_kind,
                occurred_at,
                request_digest,
                safe_code,
            ),
        )

    @staticmethod
    def _insert_provider_receipt(
        db: Any,
        scope: TenantScope,
        *,
        send_outbox_ref: str,
        attempt: int,
        outcome: str,
        safe_code: str,
        provider_receipt_ref: str | None,
        observed_at: datetime,
        identity: str,
    ) -> str:
        provider_receipt_record_ref = stable_ref(
            "PRC", identity, outcome, provider_receipt_ref or "none"
        )
        db.execute(
            """
            INSERT INTO email_gateway.provider_receipts (
                site_id, provider_receipt_record_ref, send_outbox_ref, attempt,
                outcome, safe_code, provider_receipt_ref, observed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                scope.site_id,
                provider_receipt_record_ref,
                send_outbox_ref,
                attempt,
                outcome,
                safe_code,
                provider_receipt_ref,
                observed_at,
            ),
        )
        return provider_receipt_record_ref

    @staticmethod
    def _create_sent_material_authorities(
        db: Any,
        scope: TenantScope,
        *,
        provider_receipt_record_ref: str,
    ) -> None:
        db.execute(
            "SELECT email_gateway.create_sent_email_material_authorities(%s, %s)",
            (scope.site_id, provider_receipt_record_ref),
        )

    @staticmethod
    def _snapshot(row: tuple[Any, ...]) -> SendOutboxSnapshot:
        digest = str(row[2])
        if digest.startswith("sha256:"):
            digest = digest[7:]
        value = row[3]
        command = json.loads(value) if isinstance(value, str) else value
        if not isinstance(command, Mapping):
            raise ValidationError("approved envelope is invalid")
        envelope: ApprovedOutboundEnvelope = _restore_envelope(
            command,
            payload_digest=digest,
        )
        return SendOutboxSnapshot(
            send_outbox_ref=str(row[0]),
            command_receipt_ref=str(row[1]),
            envelope=envelope,
            state=str(row[4]),
            created_at=row[5],
        )

    @staticmethod
    def _claim_snapshot(claim: OutboxClaim, state: str) -> SendOutboxSnapshot:
        return SendOutboxSnapshot(
            send_outbox_ref=claim.snapshot.send_outbox_ref,
            command_receipt_ref=claim.snapshot.command_receipt_ref,
            envelope=claim.snapshot.envelope,
            state=state,
            created_at=claim.snapshot.created_at,
        )


__all__ = [
    "DisabledSendOutboxRepository",
    "PostgresEmailSendRepository",
    "PostgresSendOutboxRepository",
]
