"""Relational Context intelligence persistence and fenced communication draft outbox."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from services.agent_runtime.materialization import FrappeDraftReceipt
from services.observer.observer.model_projection import ContextIntelligencePublication
from services.observer.observer.models import TenantScope

_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_FACT_FIELDS = frozenset(
    {
        "subject_ref",
        "predicate",
        "value_display",
        "type",
        "unit",
        "confidence",
        "evidence_refs",
        "status",
    }
)
_ASSOCIATION_FIELDS = frozenset({"type", "target_ref", "confidence", "evidence_refs"})
_APPROVED_MODEL = "deepseek-v4-flash"
_PROCESSING_PURPOSE = "observation_processing"


class CommunicationIntelligenceConflict(ValueError):
    """A stable publication or draft identity was replayed with different content."""


class CommunicationDraftLeaseConflict(RuntimeError):
    """A communication draft worker no longer owns the live expected attempt."""


class Cursor(Protocol):
    def __enter__(self) -> Cursor: ...

    def __exit__(self, *args: object) -> None: ...

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> Any: ...

    def fetchone(self) -> tuple[Any, ...] | None: ...

    def fetchall(self) -> list[tuple[Any, ...]]: ...


class Connection(Protocol):
    def transaction(self) -> AbstractContextManager[Any]: ...

    def cursor(self) -> Cursor: ...


TeamRefResolver = Callable[[TenantScope, str], str | None]


@dataclass(frozen=True, slots=True, repr=False)
class CommunicationDraftClaim:
    site_id: str
    draft_id: str
    intelligence_id: str
    observation_id: str
    processing_purpose: str
    subject: str
    summary_zh: str = field(repr=False)
    team_ref: str = field(repr=False)
    evidence_refs: tuple[str, ...] = field(repr=False)
    model_name: str = field(repr=False)
    model_version: str = field(repr=False)
    payload_digest: str = field(repr=False)
    attempt: int
    max_attempts: int
    lease_owner: str
    lease_expires_at: datetime

    def __post_init__(self) -> None:
        for value, field_name, maximum in (
            (self.site_id, "site_id", 140),
            (self.draft_id, "draft_id", 256),
            (self.intelligence_id, "intelligence_id", 256),
            (self.observation_id, "observation_id", 256),
            (self.processing_purpose, "processing_purpose", 80),
            (self.subject, "subject", 140),
            (self.summary_zh, "summary_zh", 2_000),
            (self.team_ref, "team_ref", 256),
            (self.model_name, "model_name", 160),
            (self.model_version, "model_version", 160),
            (self.lease_owner, "lease_owner", 256),
        ):
            _text(value, field_name, maximum=maximum)
        if self.processing_purpose != _PROCESSING_PURPOSE:
            raise ValueError("invalid draft processing purpose")
        if _DIGEST.fullmatch(self.payload_digest) is None:
            raise ValueError("invalid draft payload digest")
        if (
            not isinstance(self.evidence_refs, tuple)
            or not self.evidence_refs
            or len(self.evidence_refs) != len(set(self.evidence_refs))
        ):
            raise ValueError("invalid draft evidence_refs")
        for value in self.evidence_refs:
            _text(value, "evidence_ref", maximum=512)
        if (
            not isinstance(self.attempt, int)
            or isinstance(self.attempt, bool)
            or not 1 <= self.attempt <= self.max_attempts <= 5
        ):
            raise ValueError("invalid draft attempt")
        _aware(self.lease_expires_at, "lease_expires_at")

    def __repr__(self) -> str:
        return (
            "CommunicationDraftClaim("
            f"draft_id={self.draft_id!r}, attempt={self.attempt}, "
            "identity=<redacted>, summary=<redacted>, evidence=<redacted>, "
            "team=<redacted>, model=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class CommunicationDraftRunResult:
    status: Literal["idle", "succeeded", "retry", "dead_letter", "lease_lost"]
    draft_id: str | None
    attempt: int | None
    error_code: str | None = None


class CommunicationDraftRepository(Protocol):
    def claim_draft(
        self,
        site_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> CommunicationDraftClaim | None: ...

    def heartbeat_draft(
        self,
        site_id: str,
        draft_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        now: datetime,
        lease_duration: timedelta,
    ) -> None: ...

    def acknowledge_draft(
        self,
        site_id: str,
        draft_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        now: datetime,
        receipt: FrappeDraftReceipt,
    ) -> FrappeDraftReceipt: ...

    def fail_draft(
        self,
        site_id: str,
        draft_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        now: datetime,
        retry_at: datetime,
        error_code: str,
    ) -> Literal["retry", "dead_letter"]: ...


class PostgresCommunicationIntelligenceRepository:
    """Implements the Observer Context publisher and the local draft outbox."""

    __slots__ = ("_connection", "_team_ref_resolver")

    def __init__(
        self,
        connection: Connection,
        *,
        team_ref_resolver: TeamRefResolver,
    ) -> None:
        if not callable(team_ref_resolver):
            raise TypeError("a trusted team_ref_resolver is required")
        self._connection = connection
        self._team_ref_resolver = team_ref_resolver

    def __repr__(self) -> str:
        return (
            "PostgresCommunicationIntelligenceRepository("
            "connection=<redacted>, team_ref_resolver=<redacted>)"
        )

    def publish(
        self,
        scope: TenantScope,
        publication: ContextIntelligencePublication,
        *,
        idempotency_key: str,
    ) -> None:
        if scope.site_id != publication.site_id:
            raise ValueError("publication site does not match trusted scope")
        if scope.processing_purpose != _PROCESSING_PURPOSE:
            raise ValueError("publication purpose is not observation processing")
        prepared = _prepare_publication(
            scope,
            publication,
            idempotency_key=idempotency_key,
            trusted_team_ref=self._team_ref_resolver(scope, publication.observation_id),
        )
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope.site_id)
            cursor.execute(
                """
                SELECT payload_digest
                FROM context.communication_intelligence
                WHERE site_id = %s AND idempotency_key = %s
                """,
                (scope.site_id, idempotency_key),
            )
            existing = cursor.fetchone()
            if existing is not None:
                if str(existing[0]) != prepared.payload_digest:
                    raise CommunicationIntelligenceConflict(
                        "publication idempotency key conflicts with another payload"
                    )
                return
            cursor.execute(
                """
                INSERT INTO context.communication_intelligence (
                    site_id, intelligence_id, observation_id, processing_purpose,
                    idempotency_key, payload_digest, summary_zh, original_language,
                    confidence, review_status, team_ref, model_name, model_version
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, 'AI Draft',
                    %s, %s, %s
                )
                """,
                (
                    scope.site_id,
                    prepared.intelligence_id,
                    publication.observation_id,
                    scope.processing_purpose,
                    idempotency_key,
                    prepared.payload_digest,
                    publication.summary_zh,
                    publication.original_language,
                    publication.confidence,
                    prepared.team_ref,
                    publication.model["name"],
                    publication.model["version"],
                ),
            )
            for ordinal, evidence_ref in enumerate(publication.evidence_refs, start=1):
                cursor.execute(
                    """
                    INSERT INTO context.communication_intelligence_evidence (
                        site_id, intelligence_id, evidence_ref, ordinal
                    ) VALUES (%s, %s, %s, %s)
                    """,
                    (scope.site_id, prepared.intelligence_id, evidence_ref, ordinal),
                )
            for ordinal, invocation_ref in enumerate(publication.invocation_refs, start=1):
                cursor.execute(
                    """
                    INSERT INTO context.communication_intelligence_invocations (
                        site_id, intelligence_id, invocation_ref, ordinal
                    ) VALUES (%s, %s, %s, %s)
                    """,
                    (scope.site_id, prepared.intelligence_id, invocation_ref, ordinal),
                )
            for ordinal, fact in enumerate(publication.fact_proposals, start=1):
                fact_id = _stable_id(
                    "communication-fact",
                    prepared.intelligence_id,
                    str(ordinal),
                    _digest(fact),
                )
                cursor.execute(
                    """
                    INSERT INTO context.communication_fact_proposals (
                        site_id, intelligence_id, fact_proposal_id, ordinal,
                        subject_ref, predicate, value_display, value_type, unit,
                        confidence, status
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'proposed'
                    )
                    """,
                    (
                        scope.site_id,
                        prepared.intelligence_id,
                        fact_id,
                        ordinal,
                        fact["subject_ref"],
                        fact["predicate"],
                        fact["value_display"],
                        fact["type"],
                        fact["unit"],
                        fact["confidence"],
                    ),
                )
                for evidence_ref in fact["evidence_refs"]:
                    cursor.execute(
                        """
                        INSERT INTO context.communication_fact_evidence (
                            site_id, intelligence_id, fact_proposal_id, evidence_ref
                        ) VALUES (%s, %s, %s, %s)
                        """,
                        (
                            scope.site_id,
                            prepared.intelligence_id,
                            fact_id,
                            evidence_ref,
                        ),
                    )
            for ordinal, suggestion in enumerate(
                publication.association_suggestions,
                start=1,
            ):
                suggestion_id = _stable_id(
                    "communication-association",
                    prepared.intelligence_id,
                    str(ordinal),
                    _digest(suggestion),
                )
                cursor.execute(
                    """
                    INSERT INTO context.communication_association_suggestions (
                        site_id, intelligence_id, association_suggestion_id,
                        ordinal, association_type, target_ref, confidence, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'proposed')
                    """,
                    (
                        scope.site_id,
                        prepared.intelligence_id,
                        suggestion_id,
                        ordinal,
                        suggestion["type"],
                        suggestion["target_ref"],
                        suggestion["confidence"],
                    ),
                )
                for evidence_ref in suggestion["evidence_refs"]:
                    cursor.execute(
                        """
                        INSERT INTO context.communication_association_evidence (
                            site_id, intelligence_id, association_suggestion_id,
                            evidence_ref
                        ) VALUES (%s, %s, %s, %s)
                        """,
                        (
                            scope.site_id,
                            prepared.intelligence_id,
                            suggestion_id,
                            evidence_ref,
                        ),
                    )
            if prepared.team_ref is not None:
                cursor.execute(
                    """
                    INSERT INTO context.communication_draft_outbox (
                        site_id, draft_id, intelligence_id, observation_id,
                        processing_purpose, subject, summary_zh, team_ref,
                        model_name, model_version, origin, origin_reference,
                        review_status, is_official_metric, idempotency_key,
                        payload_digest, status, attempt, max_attempts,
                        next_attempt_at, created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        'AI', %s, 'AI Draft', FALSE, %s, %s, 'pending',
                        0, 5, now(), now(), now()
                    )
                    """,
                    (
                        scope.site_id,
                        prepared.draft_id,
                        prepared.intelligence_id,
                        publication.observation_id,
                        scope.processing_purpose,
                        prepared.subject,
                        publication.summary_zh,
                        prepared.team_ref,
                        publication.model["name"],
                        publication.model["version"],
                        publication.observation_id,
                        f"communication-draft:{publication.observation_id}",
                        prepared.draft_payload_digest,
                    ),
                )
                for ordinal, evidence_ref in enumerate(
                    publication.evidence_refs,
                    start=1,
                ):
                    cursor.execute(
                        """
                        INSERT INTO context.communication_draft_evidence (
                            site_id, draft_id, evidence_ref, ordinal
                        ) VALUES (%s, %s, %s, %s)
                        """,
                        (scope.site_id, prepared.draft_id, evidence_ref, ordinal),
                    )

    def claim_draft(
        self,
        site_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> CommunicationDraftClaim | None:
        _text(site_id, "site_id", maximum=140)
        _text(worker_id, "worker_id", maximum=256)
        _aware(now, "now")
        _positive_duration(lease_duration)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            cursor.execute(
                """
                UPDATE context.communication_draft_outbox
                SET status = 'dead_letter', lease_owner = NULL,
                    lease_expires_at = NULL,
                    last_error_code = 'lease_expired_max_attempts',
                    updated_at = %s
                WHERE site_id = %s
                  AND status = 'running'
                  AND lease_expires_at <= %s
                  AND attempt >= max_attempts
                """,
                (now, site_id, now),
            )
            cursor.execute(
                """
                WITH candidate AS (
                    SELECT site_id, draft_id
                    FROM context.communication_draft_outbox
                    WHERE site_id = %s
                      AND attempt < max_attempts
                      AND (
                        (status IN ('pending', 'retry') AND next_attempt_at <= %s)
                        OR (status = 'running' AND lease_expires_at <= %s)
                      )
                    ORDER BY next_attempt_at, created_at, draft_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE context.communication_draft_outbox AS draft
                SET status = 'running',
                    attempt = draft.attempt + 1,
                    lease_owner = %s,
                    lease_expires_at = %s,
                    last_error_code = NULL,
                    updated_at = %s
                FROM candidate
                WHERE draft.site_id = candidate.site_id
                  AND draft.draft_id = candidate.draft_id
                RETURNING
                    draft.draft_id, draft.intelligence_id, draft.observation_id,
                    draft.processing_purpose, draft.subject, draft.summary_zh,
                    draft.team_ref, draft.model_name, draft.model_version,
                    draft.payload_digest, draft.attempt, draft.max_attempts, draft.lease_owner,
                    draft.lease_expires_at
                """,
                (
                    site_id,
                    now,
                    now,
                    worker_id,
                    now + lease_duration,
                    now,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            cursor.execute(
                """
                SELECT evidence_ref
                FROM context.communication_draft_evidence
                WHERE site_id = %s AND draft_id = %s
                ORDER BY ordinal
                """,
                (site_id, str(row[0])),
            )
            evidence_refs = tuple(str(value[0]) for value in cursor.fetchall())
            return CommunicationDraftClaim(
                site_id=site_id,
                draft_id=str(row[0]),
                intelligence_id=str(row[1]),
                observation_id=str(row[2]),
                processing_purpose=str(row[3]),
                subject=str(row[4]),
                summary_zh=str(row[5]),
                team_ref=str(row[6]),
                evidence_refs=evidence_refs,
                model_name=str(row[7]),
                model_version=str(row[8]),
                payload_digest=str(row[9]),
                attempt=int(row[10]),
                max_attempts=int(row[11]),
                lease_owner=str(row[12]),
                lease_expires_at=row[13],
            )

    def heartbeat_draft(
        self,
        site_id: str,
        draft_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        now: datetime,
        lease_duration: timedelta,
    ) -> None:
        _aware(now, "now")
        _positive_duration(lease_duration)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            cursor.execute(
                """
                WITH renewed AS (
                    UPDATE context.communication_draft_outbox
                    SET lease_expires_at = %s + %s, updated_at = %s
                    WHERE site_id = %s AND draft_id = %s
                      AND status = 'running' AND lease_owner = %s
                      AND attempt = %s AND lease_expires_at > %s
                      AND lease_expires_at < %s + %s
                    RETURNING draft_id
                )
                SELECT draft_id FROM renewed
                UNION ALL
                SELECT draft_id
                FROM context.communication_draft_outbox
                WHERE site_id = %s AND draft_id = %s
                  AND status = 'running' AND lease_owner = %s
                  AND attempt = %s AND lease_expires_at > %s
                LIMIT 1
                """,
                (
                    now,
                    lease_duration,
                    now,
                    site_id,
                    draft_id,
                    worker_id,
                    expected_attempt,
                    now,
                    now,
                    lease_duration,
                    site_id,
                    draft_id,
                    worker_id,
                    expected_attempt,
                    now,
                ),
            )
            if cursor.fetchone() is None:
                raise CommunicationDraftLeaseConflict("communication draft lease lost")

    def acknowledge_draft(
        self,
        site_id: str,
        draft_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        now: datetime,
        receipt: FrappeDraftReceipt,
    ) -> FrappeDraftReceipt:
        _aware(now, "now")
        if (
            not isinstance(receipt, FrappeDraftReceipt)
            or receipt.request_id != draft_id
            or receipt.doctype != "GBOS Informal Observation"
        ):
            raise CommunicationIntelligenceConflict("invalid communication draft receipt")
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            cursor.execute(
                """
                SELECT status, receipt_doctype, receipt_name, receipt_revision,
                       receipt_request_id, receipt_digest
                FROM context.communication_draft_outbox
                WHERE site_id = %s AND draft_id = %s
                FOR UPDATE
                """,
                (site_id, draft_id),
            )
            existing = cursor.fetchone()
            if existing is None:
                raise CommunicationIntelligenceConflict("communication draft is absent")
            if str(existing[0]) == "succeeded":
                replay = FrappeDraftReceipt(
                    doctype=str(existing[1]),
                    name=str(existing[2]),
                    revision=int(existing[3]),
                    request_id=str(existing[4]),
                    request_digest=str(existing[5]),
                )
                if replay != receipt:
                    raise CommunicationIntelligenceConflict(
                        "communication draft receipt replay conflicts"
                    )
                return replay
            cursor.execute(
                """
                UPDATE context.communication_draft_outbox
                SET status = 'succeeded', lease_owner = NULL,
                    lease_expires_at = NULL, last_error_code = NULL,
                    receipt_doctype = %s, receipt_name = %s,
                    receipt_revision = %s, receipt_request_id = %s,
                    receipt_digest = %s, updated_at = %s
                WHERE site_id = %s AND draft_id = %s
                  AND status = 'running' AND lease_owner = %s
                  AND attempt = %s AND lease_expires_at > %s
                RETURNING draft_id
                """,
                (
                    receipt.doctype,
                    receipt.name,
                    receipt.revision,
                    receipt.request_id,
                    receipt.request_digest,
                    now,
                    site_id,
                    draft_id,
                    worker_id,
                    expected_attempt,
                    now,
                ),
            )
            if cursor.fetchone() is None:
                raise CommunicationDraftLeaseConflict("communication draft lease lost")
            return receipt

    def fail_draft(
        self,
        site_id: str,
        draft_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        now: datetime,
        retry_at: datetime,
        error_code: str,
    ) -> Literal["retry", "dead_letter"]:
        _aware(now, "now")
        _aware(retry_at, "retry_at")
        if retry_at <= now or _SAFE_CODE.fullmatch(error_code) is None:
            raise ValueError("invalid communication draft retry")
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            cursor.execute(
                """
                UPDATE context.communication_draft_outbox
                SET status = CASE
                        WHEN attempt >= max_attempts THEN 'dead_letter'
                        ELSE 'retry'
                    END,
                    lease_owner = NULL, lease_expires_at = NULL,
                    next_attempt_at = %s, last_error_code = %s,
                    updated_at = %s
                WHERE site_id = %s AND draft_id = %s
                  AND status = 'running' AND lease_owner = %s
                  AND attempt = %s AND lease_expires_at > %s
                RETURNING status
                """,
                (
                    retry_at,
                    error_code,
                    now,
                    site_id,
                    draft_id,
                    worker_id,
                    expected_attempt,
                    now,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise CommunicationDraftLeaseConflict("communication draft lease lost")
            status = str(row[0])
            if status not in {"retry", "dead_letter"}:
                raise ValueError("invalid communication draft failure state")
            if status == "retry":
                return "retry"
            return "dead_letter"

    @staticmethod
    def _set_site(cursor: Cursor, site_id: str) -> None:
        cursor.execute("SELECT set_config('app.site_id', %s, true)", (site_id,))


@dataclass(frozen=True, slots=True)
class _PreparedPublication:
    intelligence_id: str
    draft_id: str
    payload_digest: str
    draft_payload_digest: str
    subject: str
    team_ref: str | None


def _prepare_publication(
    scope: TenantScope,
    publication: ContextIntelligencePublication,
    *,
    idempotency_key: str,
    trusted_team_ref: str | None,
) -> _PreparedPublication:
    if scope.site_id != publication.site_id:
        raise ValueError("publication site does not match trusted scope")
    if scope.processing_purpose != _PROCESSING_PURPOSE:
        raise ValueError("publication purpose is not observation processing")
    _text(idempotency_key, "idempotency_key", maximum=256)
    resolved_team = _optional_text(trusted_team_ref, "trusted team_ref", maximum=256)
    if publication.team_ref is not None and publication.team_ref != resolved_team:
        raise ValueError("publication team does not match trusted source")
    team_ref = publication.team_ref or resolved_team
    evidence = set(publication.evidence_refs)
    facts = tuple(_closed_fact(value, evidence) for value in publication.fact_proposals)
    associations = tuple(
        _closed_association(value, evidence) for value in publication.association_suggestions
    )
    payload = {
        "site_id": publication.site_id,
        "processing_purpose": scope.processing_purpose,
        "observation_id": publication.observation_id,
        "team_ref": team_ref,
        "evidence_refs": list(publication.evidence_refs),
        "summary_zh": publication.summary_zh,
        "original_language": publication.original_language,
        "confidence": publication.confidence,
        "review_status": publication.review_status,
        "fact_proposals": list(facts),
        "association_suggestions": list(associations),
        "model": publication.model,
        "invocation_refs": list(publication.invocation_refs),
    }
    intelligence_id = _stable_id(
        "communication-intelligence",
        publication.site_id,
        publication.observation_id,
    )
    draft_id = _stable_id(
        "communication-draft",
        publication.site_id,
        publication.observation_id,
    )
    subject = _draft_subject(publication.observation_id)
    draft_payload = (
        {}
        if team_ref is None
        else {
            "operation": "create",
            "doctype": "GBOS Informal Observation",
            "values": {
                "subject": subject,
                "summary_zh": publication.summary_zh,
                "team": team_ref,
                "evidence_refs": [
                    {"evidence_ref": value, "locator_ref": value}
                    for value in publication.evidence_refs
                ],
                "model_name": publication.model["name"],
                "model_version": publication.model["version"],
                "is_official_metric": False,
                "origin": "AI",
                "origin_reference": publication.observation_id,
                "review_status": "AI Draft",
            },
        }
    )
    return _PreparedPublication(
        intelligence_id=intelligence_id,
        draft_id=draft_id,
        payload_digest=_digest(payload),
        draft_payload_digest=_digest(draft_payload),
        subject=subject,
        team_ref=team_ref,
    )


def _closed_fact(value: dict[str, Any], allowed_evidence: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _FACT_FIELDS:
        raise ValueError("fact proposal must use the closed schema")
    if value.get("status") != "proposed":
        raise ValueError("fact proposal must remain proposed")
    _text(value.get("subject_ref"), "fact subject_ref", maximum=512)
    _text(value.get("predicate"), "fact predicate", maximum=160)
    _text(value.get("value_display"), "fact value_display", maximum=2_000)
    _text(value.get("type"), "fact type", maximum=80)
    if value.get("unit") is not None:
        _text(value.get("unit"), "fact unit", maximum=80)
    _confidence(value.get("confidence"), "fact confidence")
    _bound_evidence(value.get("evidence_refs"), allowed_evidence, "fact evidence")
    return dict(value)


def _closed_association(
    value: dict[str, Any],
    allowed_evidence: set[str],
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _ASSOCIATION_FIELDS:
        raise ValueError("association suggestion must use the closed schema")
    _text(value.get("type"), "association type", maximum=80)
    _text(value.get("target_ref"), "association target_ref", maximum=512)
    _confidence(value.get("confidence"), "association confidence")
    _bound_evidence(
        value.get("evidence_refs"),
        allowed_evidence,
        "association evidence",
    )
    return dict(value)


def _bound_evidence(value: object, allowed: set[str], field_name: str) -> None:
    if (
        not isinstance(value, list)
        or not value
        or len(value) != len(set(value))
        or not all(isinstance(item, str) and item in allowed for item in value)
    ):
        raise ValueError(f"{field_name} is outside the publication evidence")


def _text(value: object, field_name: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(character in value for character in ("\r", "\n", "\x00"))
    ):
        raise ValueError(f"invalid {field_name}")
    return value


def _optional_text(value: object, field_name: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _text(value, field_name, maximum=maximum)


def _confidence(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 1:
        raise ValueError(f"invalid {field_name}")
    return float(value)


def _aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _positive_duration(value: timedelta) -> None:
    if value <= timedelta(0):
        raise ValueError("lease_duration must be positive")


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _stable_id(namespace: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join((namespace, *parts)).encode()).hexdigest()
    return f"{namespace}-{digest[:32]}"


def _draft_subject(observation_id: str) -> str:
    candidate = f"Communication event {observation_id}"
    if len(candidate) <= 140:
        return candidate
    suffix = hashlib.sha256(observation_id.encode()).hexdigest()[:24]
    return f"Communication event {suffix}"


__all__ = [
    "CommunicationDraftClaim",
    "CommunicationDraftLeaseConflict",
    "CommunicationDraftRepository",
    "CommunicationDraftRunResult",
    "CommunicationIntelligenceConflict",
    "PostgresCommunicationIntelligenceRepository",
]
