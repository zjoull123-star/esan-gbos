from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from threading import RLock
from typing import Any

from ..models import (
    AuditEvent,
    IdempotencyConflict,
    IdentityProjection,
    InboxItem,
    RevisionConflict,
    TenantScope,
    ValidationError,
    require_scope,
    stable_ref,
)
from ..postgres import Connection, redacted_database_errors, site_transaction

_ACTIVE = frozenset({"queued", "retry", "leased"})
_TERMINAL = frozenset({"completed", "superseded", "dead_letter"})
_RETRY_CODES = frozenset({"authority_timeout", "authority_rate_limited", "authority_server_error"})
_PERMANENT_CODES = frozenset(
    {
        "authority_rejected",
        "authority_response_invalid",
        "projection_not_routeable",
        "route_apply_rejected",
    }
)

CANDIDATE_REFS_SQL = """
    SELECT inbox.inbox_item_ref
      FROM email_gateway.identity_route_work AS work
      JOIN email_gateway.inbox_items AS inbox
        ON inbox.site_id = work.site_id
      JOIN email_gateway.mailboxes AS mailbox
        ON mailbox.site_id = inbox.site_id
       AND mailbox.mailbox_ref = inbox.mailbox_ref
      JOIN email_gateway.message_participants AS participant
        ON participant.site_id = inbox.site_id
       AND participant.message_ref = inbox.message_ref
     WHERE work.site_id = %s
       AND work.processing_purpose = %s
       AND work.work_ref = %s
       AND work.status = 'leased'
       AND work.lease_owner = %s
       AND work.attempt = %s
       AND work.lease_generation = %s
       AND work.fence_token = %s
       AND work.lease_expires_at >= %s
       AND mailbox.business_purpose = work.processing_purpose
       AND mailbox.default_team_ref = work.expected_team_ref
       AND inbox.team_ref = work.expected_team_ref
       AND inbox.state = 'identity_pending'
       AND inbox.assignee_user_ref IS NULL
       AND participant.role = 'from'
       AND participant.identity_ref = work.opaque_address_ref
     ORDER BY inbox.received_at, inbox.inbox_item_ref
     LIMIT %s
"""


class IdentityRouteLeaseLost(ValidationError):
    """The exact work generation is no longer live."""


@dataclass(frozen=True, slots=True)
class IdentityRouteWorkItem:
    work_ref: str
    site_id: str
    processing_purpose: str
    opaque_address_ref: str
    mapping_ref: str
    mapping_revision: int
    expected_team_ref: str
    projection_receipt_ref: str
    projection_payload_digest: str
    status: str
    attempt: int = 0
    generation: int = 0

    def __repr__(self) -> str:
        return (
            "IdentityRouteWorkItem("
            f"work_ref={self.work_ref!r}, site_id={self.site_id!r}, "
            f"processing_purpose={self.processing_purpose!r}, "
            f"mapping_revision={self.mapping_revision}, status={self.status!r}, "
            "opaque_address_ref=<redacted>, mapping_ref=<redacted>, "
            "expected_team_ref=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class IdentityRouteWorkClaim:
    work_ref: str
    site_id: str
    processing_purpose: str
    opaque_address_ref: str
    mapping_ref: str
    mapping_revision: int
    expected_team_ref: str
    projection_receipt_ref: str
    projection_payload_digest: str
    worker_id: str
    attempt: int
    generation: int
    fence_token: str
    lease_expires_at: datetime

    def __repr__(self) -> str:
        return (
            "IdentityRouteWorkClaim("
            f"work_ref={self.work_ref!r}, site_id={self.site_id!r}, "
            f"processing_purpose={self.processing_purpose!r}, attempt={self.attempt}, "
            f"generation={self.generation}, lease_expires_at={self.lease_expires_at!r}, "
            "opaque_address_ref=<redacted>, mapping_ref=<redacted>, "
            "expected_team_ref=<redacted>, fence_token=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class IdentityRouteCandidate:
    inbox: InboxItem
    projection: IdentityProjection


class InMemoryIdentityRouteWorkRepository:
    """Reference enqueue semantics used by the in-memory projection repository."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str, str, int], IdentityRouteWorkItem] = {}
        self._lock = RLock()

    def enqueue_projection(
        self, scope: TenantScope, projection: IdentityProjection
    ) -> IdentityRouteWorkItem:
        require_scope(
            scope,
            site_id=projection.site_id,
            processing_purpose=projection.processing_purpose,
        )
        key = (
            scope.site_id,
            scope.processing_purpose,
            projection.opaque_address_ref,
            projection.external_identity_revision,
        )
        with self._lock:
            current = self._items.get(key)
            expected = _item_from_projection(projection)
            if current is not None:
                if current != expected:
                    raise IdempotencyConflict("identity route work projection drift")
                return current
            for old_key, old in tuple(self._items.items()):
                if (
                    old_key[:3] == key[:3]
                    and old.mapping_revision < projection.external_identity_revision
                    and old.status in _ACTIVE
                ):
                    self._items[old_key] = replace(
                        old,
                        status="superseded",
                        generation=old.generation + 1,
                    )
            self._items[key] = expected
            return expected

    def list(self, scope: TenantScope) -> tuple[IdentityRouteWorkItem, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._items.values()
                    if item.site_id == scope.site_id
                    and item.processing_purpose == scope.processing_purpose
                ),
                key=lambda item: (item.mapping_revision, item.work_ref),
            )
        )


def enqueue_projection_in_transaction(
    cursor: Any,
    scope: TenantScope,
    projection: IdentityProjection,
) -> None:
    """Enqueue exactly one immutable work generation inside projection persistence."""

    item = _item_from_projection(projection)
    cursor.execute(
        """
        UPDATE email_gateway.identity_route_work
           SET status = 'superseded', lease_owner = NULL,
               lease_expires_at = NULL, fence_token = NULL,
               lease_generation = lease_generation + 1,
               safe_error_code = 'projection_superseded',
               completed_at = greatest(clock_timestamp(), created_at),
               updated_at = greatest(clock_timestamp(), created_at)
         WHERE site_id = %s AND processing_purpose = %s
           AND opaque_address_ref = %s AND mapping_revision < %s
           AND status IN ('queued', 'retry', 'leased')
        """,
        (
            scope.site_id,
            scope.processing_purpose,
            projection.opaque_address_ref,
            projection.external_identity_revision,
        ),
    )
    cursor.execute(
        """
        INSERT INTO email_gateway.identity_route_work (
            site_id, processing_purpose, work_ref, opaque_address_ref,
            mapping_ref, mapping_revision, expected_team_ref,
            projection_receipt_ref, projection_payload_digest,
            status, attempt, max_attempts, lease_generation,
            next_attempt_at, created_at, updated_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s,
            'queued', 0, 5, 0,
            clock_timestamp(), clock_timestamp(), clock_timestamp()
        )
        ON CONFLICT (
            site_id, processing_purpose, opaque_address_ref, mapping_revision
        ) DO NOTHING
        """,
        (
            item.site_id,
            item.processing_purpose,
            item.work_ref,
            item.opaque_address_ref,
            item.mapping_ref,
            item.mapping_revision,
            item.expected_team_ref,
            item.projection_receipt_ref,
            item.projection_payload_digest,
        ),
    )
    cursor.execute(
        """
        SELECT work_ref, mapping_ref, expected_team_ref,
               projection_receipt_ref, projection_payload_digest
          FROM email_gateway.identity_route_work
         WHERE site_id = %s AND processing_purpose = %s
           AND opaque_address_ref = %s AND mapping_revision = %s
        """,
        (
            scope.site_id,
            scope.processing_purpose,
            projection.opaque_address_ref,
            projection.external_identity_revision,
        ),
    )
    row = cursor.fetchone()
    if row is None or tuple(str(value) for value in row) != (
        item.work_ref,
        item.mapping_ref,
        item.expected_team_ref,
        item.projection_receipt_ref,
        item.projection_payload_digest,
    ):
        raise IdempotencyConflict("identity route work projection drift")


class PostgresIdentityRouteWorkRepository:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def __repr__(self) -> str:
        return "PostgresIdentityRouteWorkRepository(connection=<redacted>)"

    def claim(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> IdentityRouteWorkClaim | None:
        _worker(worker_id)
        _aware(now)
        if not timedelta(seconds=5) <= lease_duration <= timedelta(minutes=5):
            raise ValidationError("invalid identity route lease")
        fence = _fence_seed(scope, worker_id, now)
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            cursor.execute(
                """
                UPDATE email_gateway.identity_route_work AS work
                   SET status = 'dead_letter', lease_owner = NULL,
                       lease_expires_at = NULL,
                       lease_generation = work.lease_generation + 1,
                       fence_token = NULL,
                       safe_error_code = 'attempts_exhausted',
                       completed_at = greatest(%s, work.created_at),
                       updated_at = greatest(%s, work.created_at)
                 WHERE work.site_id = %s AND work.processing_purpose = %s
                   AND work.status = 'leased'
                   AND work.lease_expires_at < %s
                   AND work.attempt >= work.max_attempts
                """,
                (
                    now,
                    now,
                    scope.site_id,
                    scope.processing_purpose,
                    now,
                ),
            )
            cursor.execute(
                """
                WITH candidate AS (
                    SELECT work_ref
                      FROM email_gateway.identity_route_work
                     WHERE site_id = %s AND processing_purpose = %s
                       AND attempt < max_attempts
                       AND (
                           (status IN ('queued', 'retry') AND next_attempt_at <= %s)
                           OR (status = 'leased' AND lease_expires_at < %s)
                       )
                     ORDER BY next_attempt_at, created_at, work_ref
                     FOR UPDATE SKIP LOCKED
                     LIMIT 1
                )
                UPDATE email_gateway.identity_route_work AS work
                   SET status = 'leased', attempt = work.attempt + 1,
                       lease_owner = %s, lease_expires_at = %s,
                       lease_generation = work.lease_generation + 1,
                       fence_token = %s, safe_error_code = NULL,
                       updated_at = greatest(%s, work.created_at)
                  FROM candidate
                 WHERE work.site_id = %s AND work.processing_purpose = %s
                   AND work.work_ref = candidate.work_ref
                RETURNING work.work_ref, work.site_id, work.processing_purpose,
                          work.opaque_address_ref, work.mapping_ref,
                          work.mapping_revision, work.expected_team_ref,
                          work.projection_receipt_ref,
                          work.projection_payload_digest, work.lease_owner,
                          work.attempt, work.lease_generation, work.fence_token,
                          work.lease_expires_at
                """,
                (
                    scope.site_id,
                    scope.processing_purpose,
                    now,
                    now,
                    worker_id,
                    now + lease_duration,
                    fence,
                    now,
                    scope.site_id,
                    scope.processing_purpose,
                ),
            )
            row = cursor.fetchone()
        return None if row is None else _claim(row)

    def projection_state(self, scope: TenantScope, claim: IdentityRouteWorkClaim) -> str:
        _claim_scope(scope, claim)
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            cursor.execute(
                """
                SELECT external_identity_ref, external_identity_revision,
                       identity_type, team_ref, status,
                       projection_receipt_ref, payload_digest
                  FROM email_gateway.identity_projection_receipts
                 WHERE site_id = %s AND processing_purpose = %s
                   AND opaque_address_ref = %s
                 ORDER BY external_identity_revision DESC, created_at DESC
                 LIMIT 1
                """,
                (scope.site_id, scope.processing_purpose, claim.opaque_address_ref),
            )
            row = cursor.fetchone()
        if row is None or int(row[1]) != claim.mapping_revision:
            return "superseded"
        if (
            str(row[0]) != claim.mapping_ref
            or str(row[2]) != "Party"
            or str(row[3]) != claim.expected_team_ref
            or str(row[4]) != "confirmed"
            or str(row[5]) != claim.projection_receipt_ref
            or str(row[6]) != claim.projection_payload_digest
        ):
            return "not_routeable"
        return "current_routeable"

    def list_candidate_refs(
        self,
        scope: TenantScope,
        claim: IdentityRouteWorkClaim,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[str, ...]:
        _claim_scope(scope, claim)
        _limit(limit)
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            cursor.execute(
                CANDIDATE_REFS_SQL,
                (
                    scope.site_id,
                    scope.processing_purpose,
                    claim.work_ref,
                    claim.worker_id,
                    claim.attempt,
                    claim.generation,
                    claim.fence_token,
                    now,
                    limit,
                ),
            )
            return tuple(str(row[0]) for row in cursor.fetchall())

    def load_candidate(
        self,
        scope: TenantScope,
        claim: IdentityRouteWorkClaim,
        inbox_item_ref: str,
        *,
        now: datetime,
    ) -> IdentityRouteCandidate | None:
        _claim_scope(scope, claim)
        workflow = self.workflow_for(claim)
        inbox = workflow.get_inbox(scope, inbox_item_ref, now=now)
        if inbox is None:
            return None
        projection = IdentityProjection(
            site_id=claim.site_id,
            processing_purpose=claim.processing_purpose,
            opaque_address_ref=claim.opaque_address_ref,
            external_identity_ref=claim.mapping_ref,
            external_identity_revision=claim.mapping_revision,
            identity_type="Party",
            team_ref=claim.expected_team_ref,
            status="confirmed",
            projection_receipt_ref=claim.projection_receipt_ref,
            observed_at=inbox.updated_at,
            payload_digest=claim.projection_payload_digest,
        )
        return IdentityRouteCandidate(inbox, projection)

    def workflow_for(self, claim: IdentityRouteWorkClaim) -> FencedIdentityRouteWorkflowRepository:
        return FencedIdentityRouteWorkflowRepository(self.connection, claim)

    def complete(self, scope: TenantScope, claim: IdentityRouteWorkClaim, *, now: datetime) -> None:
        self._terminal(scope, claim, now=now, status="completed", code=None)

    def continue_work(
        self, scope: TenantScope, claim: IdentityRouteWorkClaim, *, now: datetime
    ) -> None:
        _claim_scope(scope, claim)
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            cursor.execute(
                """
                UPDATE email_gateway.identity_route_work AS work
                   SET status = 'queued', attempt = 0,
                       lease_owner = NULL, lease_expires_at = NULL,
                       lease_generation = work.lease_generation + 1,
                       fence_token = NULL, safe_error_code = NULL,
                       next_attempt_at = greatest(%s, work.created_at),
                       completed_at = NULL,
                       updated_at = greatest(%s, work.created_at)
                 WHERE work.site_id = %s AND work.processing_purpose = %s
                   AND work.work_ref = %s AND work.status = 'leased'
                   AND work.lease_owner = %s AND work.attempt = %s
                   AND work.lease_generation = %s AND work.fence_token = %s
                   AND work.lease_expires_at >= %s
                RETURNING work.status
                """,
                (
                    now,
                    now,
                    scope.site_id,
                    scope.processing_purpose,
                    claim.work_ref,
                    claim.worker_id,
                    claim.attempt,
                    claim.generation,
                    claim.fence_token,
                    now,
                ),
            )
            if cursor.fetchone() is None:
                raise IdentityRouteLeaseLost("identity route lease fence conflict")

    def supersede(
        self, scope: TenantScope, claim: IdentityRouteWorkClaim, *, now: datetime
    ) -> None:
        self._terminal(scope, claim, now=now, status="superseded", code="projection_superseded")

    def reject(
        self,
        scope: TenantScope,
        claim: IdentityRouteWorkClaim,
        *,
        code: str,
        now: datetime,
    ) -> None:
        if code not in _PERMANENT_CODES:
            raise ValidationError("unsafe identity route rejection code")
        self._terminal(scope, claim, now=now, status="dead_letter", code=code)

    def retry(
        self,
        scope: TenantScope,
        claim: IdentityRouteWorkClaim,
        *,
        code: str,
        retry_at: datetime,
        now: datetime,
    ) -> None:
        if code not in _RETRY_CODES or retry_at <= now or retry_at > now + timedelta(hours=1):
            raise ValidationError("unsafe identity route retry")
        status = "dead_letter" if claim.attempt >= 5 else "retry"
        self._terminal(
            scope,
            claim,
            now=now,
            status=status,
            code=code,
            next_attempt_at=retry_at,
        )

    def _terminal(
        self,
        scope: TenantScope,
        claim: IdentityRouteWorkClaim,
        *,
        now: datetime,
        status: str,
        code: str | None,
        next_attempt_at: datetime | None = None,
    ) -> None:
        _claim_scope(scope, claim)
        if status not in _TERMINAL | {"retry"}:
            raise ValidationError("invalid identity route terminal state")
        completed_at = now if status in _TERMINAL else None
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            cursor.execute(
                """
                UPDATE email_gateway.identity_route_work
                   SET status = %s, lease_owner = NULL, lease_expires_at = NULL,
                       fence_token = NULL, safe_error_code = %s,
                       next_attempt_at = COALESCE(%s, next_attempt_at),
                       completed_at = %s,
                       updated_at = greatest(%s, created_at)
                 WHERE site_id = %s AND processing_purpose = %s
                   AND work_ref = %s AND status = 'leased'
                   AND lease_owner = %s AND attempt = %s
                   AND lease_generation = %s AND fence_token = %s
                   AND lease_expires_at >= %s
                RETURNING status
                """,
                (
                    status,
                    code,
                    next_attempt_at,
                    completed_at,
                    now,
                    scope.site_id,
                    scope.processing_purpose,
                    claim.work_ref,
                    claim.worker_id,
                    claim.attempt,
                    claim.generation,
                    claim.fence_token,
                    now,
                ),
            )
            if cursor.fetchone() is None:
                raise IdentityRouteLeaseLost("identity route lease fence conflict")


class FencedIdentityRouteWorkflowRepository:
    """Existing InboxOperations persistence seam with an extra work fence."""

    def __init__(self, connection: Connection, claim: IdentityRouteWorkClaim) -> None:
        self.connection = connection
        self.claim = claim

    def get_inbox(
        self,
        scope: TenantScope,
        inbox_ref: str,
        *,
        now: datetime | None = None,
    ) -> InboxItem | None:
        _claim_scope(scope, self.claim)
        at = now or datetime.now().astimezone()
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            return self._load_current(cursor, scope, inbox_ref, at)

    def replay(
        self, scope: TenantScope, idempotency_key: str, payload_digest: str
    ) -> InboxItem | None:
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            cursor.execute(
                """
                SELECT inbox_item_ref, payload_digest
                  FROM email_gateway.inbox_operation_requests
                 WHERE site_id = %s AND idempotency_key = %s
                """,
                (scope.site_id, idempotency_key),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            if str(row[1]) != payload_digest:
                raise IdempotencyConflict("identity route replay drift")
            return self._load_inbox_result(cursor, scope, str(row[0]))

    def apply_inbox_operation(
        self,
        scope: TenantScope,
        *,
        before: InboxItem,
        revised: InboxItem,
        audit_event: AuditEvent,
        idempotency_key: str,
        payload_digest: str,
    ) -> InboxItem:
        if revised.revision != before.revision + 1:
            raise RevisionConflict("identity route Inbox revision conflict")
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            cursor.execute(
                """
                SELECT email_gateway.apply_identity_route_fenced(
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    scope.site_id,
                    scope.processing_purpose,
                    self.claim.work_ref,
                    self.claim.worker_id,
                    self.claim.attempt,
                    self.claim.generation,
                    self.claim.fence_token,
                    before.inbox_item_ref,
                    before.revision,
                    revised.state,
                    revised.assignee_user_ref,
                    revised.updated_at,
                    stable_ref("OPR", scope.site_id, scope.processing_purpose, idempotency_key),
                    audit_event.request_id,
                    idempotency_key,
                    payload_digest,
                    audit_event.audit_ref,
                    audit_event.idempotency_key,
                ),
            )
            row = cursor.fetchone()
        if row is None or int(row[0]) != revised.revision:
            raise RevisionConflict("identity route Inbox revision conflict")
        return revised

    def _load_current(
        self, cursor: Any, scope: TenantScope, inbox_ref: str, now: datetime
    ) -> InboxItem | None:
        cursor.execute(
            """
            SELECT inbox.inbox_item_ref, inbox.site_id, inbox.mailbox_ref,
                   inbox.message_ref, inbox.team_ref, inbox.assignee_user_ref,
                   inbox.priority, inbox.sla_due_at, inbox.state,
                   inbox.conversation_ref, inbox.business_links, inbox.revision,
                   inbox.received_at, inbox.updated_at
              FROM email_gateway.identity_route_work AS work
              JOIN email_gateway.inbox_items AS inbox
                ON inbox.site_id = work.site_id
               AND inbox.inbox_item_ref = %s
              JOIN email_gateway.mailboxes AS mailbox
                ON mailbox.site_id = inbox.site_id
               AND mailbox.mailbox_ref = inbox.mailbox_ref
             WHERE work.site_id = %s AND work.processing_purpose = %s
               AND work.work_ref = %s AND work.status = 'leased'
               AND work.lease_owner = %s AND work.attempt = %s
               AND work.lease_generation = %s AND work.fence_token = %s
               AND work.lease_expires_at >= %s
               AND mailbox.business_purpose = work.processing_purpose
               AND mailbox.default_team_ref = work.expected_team_ref
               AND inbox.team_ref = work.expected_team_ref
               AND inbox.state = 'identity_pending'
               AND inbox.assignee_user_ref IS NULL
               AND EXISTS (
                   SELECT 1 FROM email_gateway.message_participants AS participant
                    WHERE participant.site_id = inbox.site_id
                      AND participant.message_ref = inbox.message_ref
                      AND participant.role = 'from'
                      AND participant.identity_ref = work.opaque_address_ref
               )
               AND EXISTS (
                   SELECT 1
                     FROM email_gateway.identity_projection_receipts AS projection
                    WHERE projection.site_id = work.site_id
                      AND projection.processing_purpose = work.processing_purpose
                      AND projection.opaque_address_ref = work.opaque_address_ref
                      AND projection.external_identity_ref = work.mapping_ref
                      AND projection.external_identity_revision = work.mapping_revision
                      AND projection.identity_type = 'Party'
                      AND projection.team_ref = work.expected_team_ref
                      AND projection.status = 'confirmed'
                      AND projection.projection_receipt_ref = work.projection_receipt_ref
                      AND projection.payload_digest = work.projection_payload_digest
                      AND NOT EXISTS (
                          SELECT 1
                            FROM email_gateway.identity_projection_receipts AS newer
                           WHERE newer.site_id = projection.site_id
                             AND newer.processing_purpose = projection.processing_purpose
                             AND newer.opaque_address_ref = projection.opaque_address_ref
                             AND newer.external_identity_revision
                                 > projection.external_identity_revision
                      )
               )
            """,
            (
                inbox_ref,
                scope.site_id,
                scope.processing_purpose,
                self.claim.work_ref,
                self.claim.worker_id,
                self.claim.attempt,
                self.claim.generation,
                self.claim.fence_token,
                now,
            ),
        )
        row = cursor.fetchone()
        return None if row is None else _inbox(row)

    @staticmethod
    def _load_inbox_result(cursor: Any, scope: TenantScope, inbox_ref: str) -> InboxItem:
        cursor.execute(
            """
            SELECT inbox.inbox_item_ref, inbox.site_id, inbox.mailbox_ref,
                   inbox.message_ref, inbox.team_ref, inbox.assignee_user_ref,
                   inbox.priority, inbox.sla_due_at, inbox.state,
                   inbox.conversation_ref, inbox.business_links, inbox.revision,
                   inbox.received_at, inbox.updated_at
              FROM email_gateway.inbox_items AS inbox
              JOIN email_gateway.mailboxes AS mailbox
                ON mailbox.site_id = inbox.site_id
               AND mailbox.mailbox_ref = inbox.mailbox_ref
             WHERE inbox.site_id = %s AND inbox.inbox_item_ref = %s
               AND mailbox.business_purpose = %s
            """,
            (scope.site_id, inbox_ref, scope.processing_purpose),
        )
        row = cursor.fetchone()
        if row is None:
            raise IdempotencyConflict("identity route replay result missing")
        return _inbox(row)


def _item_from_projection(projection: IdentityProjection) -> IdentityRouteWorkItem:
    return IdentityRouteWorkItem(
        work_ref=stable_ref(
            "IRW",
            projection.site_id,
            projection.processing_purpose,
            projection.opaque_address_ref,
            str(projection.external_identity_revision),
        ),
        site_id=projection.site_id,
        processing_purpose=projection.processing_purpose,
        opaque_address_ref=projection.opaque_address_ref,
        mapping_ref=projection.external_identity_ref,
        mapping_revision=projection.external_identity_revision,
        expected_team_ref=projection.team_ref,
        projection_receipt_ref=projection.projection_receipt_ref,
        projection_payload_digest=projection.payload_digest,
        status="queued",
    )


def _claim(row: tuple[Any, ...]) -> IdentityRouteWorkClaim:
    return IdentityRouteWorkClaim(
        work_ref=str(row[0]),
        site_id=str(row[1]),
        processing_purpose=str(row[2]),
        opaque_address_ref=str(row[3]),
        mapping_ref=str(row[4]),
        mapping_revision=int(row[5]),
        expected_team_ref=str(row[6]),
        projection_receipt_ref=str(row[7]),
        projection_payload_digest=str(row[8]),
        worker_id=str(row[9]),
        attempt=int(row[10]),
        generation=int(row[11]),
        fence_token=str(row[12]),
        lease_expires_at=row[13],
    )


def _inbox(row: tuple[Any, ...]) -> InboxItem:
    return InboxItem(
        inbox_item_ref=str(row[0]),
        site_id=str(row[1]),
        mailbox_ref=str(row[2]),
        message_ref=str(row[3]),
        team_ref=str(row[4]),
        assignee_user_ref=None if row[5] is None else str(row[5]),
        priority=int(row[6]),
        sla_due_at=row[7],
        state=str(row[8]),
        conversation_ref=None if row[9] is None else str(row[9]),
        business_links=tuple(str(item) for item in row[10]),
        revision=int(row[11]),
        received_at=row[12],
        updated_at=row[13],
    )


def _fence_seed(scope: TenantScope, worker_id: str, now: datetime) -> str:
    value = (
        f"{scope.site_id}\x1f{scope.processing_purpose}\x1f{worker_id}\x1f{now.isoformat()}"
    ).encode()
    return "v1:" + hashlib.sha256(value).hexdigest()


def _claim_scope(scope: TenantScope, claim: IdentityRouteWorkClaim) -> None:
    require_scope(
        scope,
        site_id=claim.site_id,
        processing_purpose=claim.processing_purpose,
    )


def _worker(value: str) -> None:
    if not value or value != value.strip() or "@" in value or len(value) > 256:
        raise ValidationError("invalid identity route worker")


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("invalid identity route time")


def _limit(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise ValidationError("invalid identity route batch limit")


__all__ = [
    "CANDIDATE_REFS_SQL",
    "FencedIdentityRouteWorkflowRepository",
    "IdentityRouteCandidate",
    "IdentityRouteLeaseLost",
    "IdentityRouteWorkClaim",
    "IdentityRouteWorkItem",
    "InMemoryIdentityRouteWorkRepository",
    "PostgresIdentityRouteWorkRepository",
    "enqueue_projection_in_transaction",
]
