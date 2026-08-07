from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Protocol

from .models import TenantScope, _require_aware
from .storage import Connection


class InvalidCursor(ValueError):
    """The cursor is malformed or was issued for another scope/filter set."""


class ScopeMismatch(PermissionError):
    """The communication is outside the caller's team scope."""


class RawAccessDenied(PermissionError):
    """Raw communication content is not available under the caller's policy."""


class CommunicationNotFound(LookupError):
    """The site-local observation does not exist."""


@dataclass(frozen=True, slots=True)
class CommunicationAccess:
    team_refs: frozenset[str]
    actor_ref: str | None = None
    allow_all_teams: bool = False
    can_read_raw: bool = False
    can_read_restricted_raw: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.team_refs, frozenset):
            raise TypeError("team_refs must be a frozenset")
        if any(not value or value != value.strip() or len(value) > 256 for value in self.team_refs):
            raise ValueError("invalid team_ref")
        if self.actor_ref is not None and (
            not self.actor_ref
            or self.actor_ref != self.actor_ref.strip()
            or len(self.actor_ref) > 256
        ):
            raise ValueError("invalid actor_ref")
        if not self.allow_all_teams and not self.team_refs:
            raise ValueError("team-scoped access requires at least one team")
        if self.can_read_restricted_raw and not self.can_read_raw:
            raise ValueError("restricted raw access requires raw access")

    def allows(
        self,
        team_ref: str | None,
        actor_refs: frozenset[str] = frozenset(),
    ) -> bool:
        return (
            self.allow_all_teams
            or (team_ref is not None and team_ref in self.team_refs)
            or (self.actor_ref is not None and self.actor_ref in actor_refs)
        )


@dataclass(frozen=True, slots=True)
class CommunicationSummary:
    observation_id: str
    channel: str
    occurred_at: datetime
    summary_zh: str
    original_language: str
    classification: str
    review_status: str
    team_ref: str | None
    party_ref: str | None
    evidence_count: int
    actor_refs: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.observation_id or len(self.observation_id) > 256:
            raise ValueError("invalid observation_id")
        _require_aware(self.occurred_at, "occurred_at")
        if self.classification not in {"Public", "Internal", "Confidential", "Restricted"}:
            raise ValueError("invalid classification")
        if self.evidence_count < 0:
            raise ValueError("evidence_count must be non-negative")
        if not isinstance(self.actor_refs, frozenset):
            raise TypeError("actor_refs must be a frozenset")

    def as_dict(self) -> dict[str, object]:
        return {
            "observation_id": self.observation_id,
            "channel": self.channel,
            "occurred_at": self.occurred_at,
            "summary_zh": self.summary_zh,
            "original_language": self.original_language,
            "classification": self.classification,
            "review_status": self.review_status,
            "team_ref": self.team_ref,
            "party_ref": self.party_ref,
            "evidence_count": self.evidence_count,
        }


@dataclass(frozen=True, slots=True)
class CommunicationDetail:
    summary: CommunicationSummary
    evidence: tuple[dict[str, str], ...]
    fact_proposals: tuple[dict[str, object], ...]
    association_suggestions: tuple[dict[str, object], ...]
    model: dict[str, str]
    original_text: str | None
    raw_access_allowed: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.model, dict)
            or set(self.model) != {"name", "version"}
            or self.model.get("name") != "deepseek-v4-flash"
            or not self.model.get("version")
        ):
            raise ValueError("invalid communication model metadata")

    def as_dict(self) -> dict[str, object]:
        return {
            **self.summary.as_dict(),
            "evidence": list(self.evidence),
            "fact_proposals": list(self.fact_proposals),
            "association_suggestions": list(self.association_suggestions),
            "model": self.model,
            "raw_access_allowed": self.raw_access_allowed,
            **({"original_text": self.original_text} if self.original_text is not None else {}),
        }


@dataclass(frozen=True, slots=True)
class CommunicationPage:
    communications: tuple[CommunicationSummary, ...]
    next_cursor: str | None


class CommunicationRepository(Protocol):
    """Read-only Observer PostgreSQL projection; it never touches Frappe MariaDB."""

    def list_communications(
        self,
        scope: TenantScope,
        access: CommunicationAccess,
        *,
        channel: str | None,
        classification: str | None,
        review_status: str | None,
        before: tuple[datetime, str] | None,
        limit: int,
    ) -> tuple[CommunicationSummary, ...]: ...

    def get_communication(
        self,
        scope: TenantScope,
        observation_id: str,
        *,
        raw_policy: str,
    ) -> CommunicationDetail | None: ...


class PostgresCommunicationRepository:
    """Read 005 observations/evidence joined to durable 006 model projections."""

    __slots__ = ("_connection", "_raw_loader")

    def __init__(
        self,
        *,
        connection: Connection,
        raw_loader: Any | None = None,
    ) -> None:
        if raw_loader is not None and not callable(raw_loader):
            raise TypeError("raw_loader must be callable")
        self._connection = connection
        self._raw_loader = raw_loader

    def __repr__(self) -> str:
        return "PostgresCommunicationRepository(connection=<redacted>)"

    def store_projection(
        self,
        scope: TenantScope,
        detail: CommunicationDetail,
        *,
        projected_at: datetime,
    ) -> None:
        """Persist model output without copying 005 team/classification authority."""

        _require_aware(projected_at, "projected_at")
        with self._connection.transaction(), self._connection.cursor() as cursor:
            _set_site(cursor, scope)
            cursor.execute(
                """
                INSERT INTO observer.communication_projections (
                  site_id, observation_event_id, summary_zh,
                  original_language, review_status, model_name,
                  model_version, fact_proposals, association_suggestions,
                  projected_at
                )
                SELECT %s, event.event_id, %s, %s, %s, %s, %s,
                       %s::jsonb, %s::jsonb, %s
                FROM observer.observation_events AS event
                WHERE event.site_id = %s AND event.event_id = %s
                ON CONFLICT (site_id, observation_event_id)
                DO UPDATE SET
                  summary_zh = EXCLUDED.summary_zh,
                  original_language = EXCLUDED.original_language,
                  review_status = EXCLUDED.review_status,
                  model_name = EXCLUDED.model_name,
                  model_version = EXCLUDED.model_version,
                  fact_proposals = EXCLUDED.fact_proposals,
                  association_suggestions = EXCLUDED.association_suggestions,
                  projected_at = EXCLUDED.projected_at
                RETURNING observation_event_id
                """,
                (
                    scope.site_id,
                    detail.summary.summary_zh,
                    detail.summary.original_language,
                    detail.summary.review_status,
                    detail.model["name"],
                    detail.model["version"],
                    json.dumps(
                        detail.fact_proposals,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        detail.association_suggestions,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    projected_at,
                    scope.site_id,
                    detail.summary.observation_id,
                ),
            )
            if cursor.fetchone() is None:
                raise CommunicationNotFound(detail.summary.observation_id)

    def list_communications(
        self,
        scope: TenantScope,
        access: CommunicationAccess,
        *,
        channel: str | None,
        classification: str | None,
        review_status: str | None,
        before: tuple[datetime, str] | None,
        limit: int,
    ) -> tuple[CommunicationSummary, ...]:
        predicates = [
            "event.site_id = %s",
            "(event.retention_until IS NULL OR event.retention_until > current_timestamp)",
        ]
        params: list[Any] = [scope.site_id]
        if not access.allow_all_teams:
            predicates.append(
                """
                (
                  event.team_ref = ANY(%s)
                  OR (
                    CAST(%s AS text) IS NOT NULL
                    AND EXISTS (
                      SELECT 1
                      FROM observer.participants AS actor
                      WHERE actor.site_id = event.site_id
                        AND actor.event_id = event.event_id
                        AND actor.identity_ref = %s
                    )
                  )
                )
                """
            )
            params.extend([sorted(access.team_refs), access.actor_ref, access.actor_ref])
        if channel is not None:
            predicates.append("event.channel = %s")
            params.append(channel)
        if classification is not None:
            predicates.append("event.data_classification = %s")
            params.append(classification)
        if review_status is not None:
            predicates.append("projection.review_status = %s")
            params.append(review_status)
        if before is not None:
            predicates.append("(event.occurred_at, event.event_id) < (%s, %s)")
            params.extend(before)
        params.append(limit)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            _set_site(cursor, scope)
            cursor.execute(
                f"""
                SELECT {_COMMUNICATION_COLUMNS}
                FROM observer.observation_events AS event
                JOIN observer.communication_projections AS projection
                  ON projection.site_id = event.site_id
                 AND projection.observation_event_id = event.event_id
                WHERE {" AND ".join(predicates)}
                ORDER BY event.occurred_at DESC, event.event_id DESC
                LIMIT %s
                """,
                tuple(params),
            )
            return tuple(_summary_from_row(row) for row in cursor.fetchall())

    def get_communication(
        self,
        scope: TenantScope,
        observation_id: str,
        *,
        raw_policy: str,
    ) -> CommunicationDetail | None:
        if raw_policy not in {"omit", "nonrestricted", "all"}:
            raise ValueError("invalid raw policy")
        with self._connection.transaction(), self._connection.cursor() as cursor:
            _set_site(cursor, scope)
            cursor.execute(
                f"""
                SELECT {_COMMUNICATION_COLUMNS},
                       projection.fact_proposals,
                       projection.association_suggestions,
                       projection.model_name,
                       projection.model_version
                FROM observer.observation_events AS event
                JOIN observer.communication_projections AS projection
                  ON projection.site_id = event.site_id
                 AND projection.observation_event_id = event.event_id
                WHERE event.site_id = %s
                  AND event.event_id = %s
                  AND (
                    event.retention_until IS NULL
                    OR event.retention_until > current_timestamp
                  )
                """,
                (scope.site_id, observation_id),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            cursor.execute(
                """
                SELECT evidence.evidence_id,
                       COALESCE(
                         evidence.content_object_ref,
                         evidence.locator::text
                       )
                FROM observer.event_evidence AS edge
                JOIN observer.evidence_refs AS evidence
                  ON evidence.site_id = edge.site_id
                 AND evidence.evidence_id = edge.evidence_id
                WHERE edge.site_id = %s AND edge.event_id = %s
                ORDER BY edge.evidence_ordinal ASC, edge.evidence_id ASC
                LIMIT 100
                """,
                (scope.site_id, observation_id),
            )
            evidence_rows = cursor.fetchall()
        summary = _summary_from_row(row[:11])
        original_text = None
        if (
            raw_policy != "omit"
            and self._raw_loader is not None
            and evidence_rows
            and (raw_policy == "all" or summary.classification != "Restricted")
        ):
            original_text = self._raw_loader(scope, str(evidence_rows[0][1]))
        return CommunicationDetail(
            summary=summary,
            evidence=tuple(
                {"ref": str(evidence_id), "locator": str(locator)}
                for evidence_id, locator in evidence_rows
            ),
            fact_proposals=tuple(_json_list(row[11], "fact proposals")),
            association_suggestions=tuple(_json_list(row[12], "association suggestions")),
            model={"name": str(row[13]), "version": str(row[14])},
            original_text=original_text,
        )


class LocalPilotReadService:
    """Provides stable cursor pagination and policy-safe communication detail."""

    __slots__ = ("_cursor_secret", "_repository")

    def __init__(
        self,
        *,
        repository: CommunicationRepository,
        cursor_secret: bytes,
    ) -> None:
        if not isinstance(cursor_secret, bytes) or len(cursor_secret) < 32:
            raise ValueError("cursor_secret must contain at least 32 bytes")
        self._repository = repository
        self._cursor_secret = cursor_secret

    def list_communications(
        self,
        scope: TenantScope,
        access: CommunicationAccess,
        *,
        channel: str | None = None,
        classification: str | None = None,
        review_status: str | None = None,
        cursor: str | None = None,
        page_size: int = 20,
    ) -> CommunicationPage:
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= 50
        ):
            raise ValueError("page_size must be between 1 and 50")
        filter_digest = self._filter_digest(
            scope,
            access,
            channel=channel,
            classification=classification,
            review_status=review_status,
        )
        before = (
            None
            if cursor is None
            else self._decode_cursor(cursor, expected_filter_digest=filter_digest)
        )
        rows = self._repository.list_communications(
            scope,
            access,
            channel=channel,
            classification=classification,
            review_status=review_status,
            before=before,
            limit=page_size + 1,
        )
        visible = tuple(row for row in rows if access.allows(row.team_ref, row.actor_refs))
        page = visible[:page_size]
        next_cursor = None
        if len(visible) > page_size:
            last = page[-1]
            next_cursor = self._encode_cursor(
                last.occurred_at,
                last.observation_id,
                filter_digest,
            )
        return CommunicationPage(communications=page, next_cursor=next_cursor)

    def get_communication(
        self,
        scope: TenantScope,
        access: CommunicationAccess,
        *,
        observation_id: str,
        include_raw: bool = False,
    ) -> CommunicationDetail:
        if not observation_id or len(observation_id) > 256:
            raise ValueError("invalid observation_id")
        raw_policy = "omit"
        if include_raw and access.can_read_raw:
            raw_policy = "all" if access.can_read_restricted_raw else "nonrestricted"
        detail = self._repository.get_communication(
            scope,
            observation_id,
            raw_policy=raw_policy,
        )
        if detail is None:
            raise CommunicationNotFound(observation_id)
        if not access.allows(
            detail.summary.team_ref,
            detail.summary.actor_refs,
        ):
            raise ScopeMismatch("communication is outside caller team scope")
        restricted = detail.summary.classification == "Restricted"
        raw_allowed = access.can_read_raw and (not restricted or access.can_read_restricted_raw)
        if include_raw and not raw_allowed:
            raise RawAccessDenied("raw communication access denied")
        return replace(
            detail,
            original_text=(detail.original_text if include_raw and raw_allowed else None),
            raw_access_allowed=raw_allowed,
        )

    def _filter_digest(
        self,
        scope: TenantScope,
        access: CommunicationAccess,
        **filters: str | None,
    ) -> str:
        payload = {
            "site_id": scope.site_id,
            "purpose": scope.processing_purpose,
            "teams": sorted(access.team_refs),
            "actor_ref": access.actor_ref,
            "allow_all": access.allow_all_teams,
            **filters,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _encode_cursor(
        self,
        occurred_at: datetime,
        observation_id: str,
        filter_digest: str,
    ) -> str:
        body = json.dumps(
            {
                "v": 1,
                "occurred_at": occurred_at.isoformat(),
                "observation_id": observation_id,
                "filter_digest": filter_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        signature = hmac.new(self._cursor_secret, body, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(body + signature).decode().rstrip("=")

    def _decode_cursor(
        self,
        cursor: str,
        *,
        expected_filter_digest: str,
    ) -> tuple[datetime, str]:
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            packed = base64.urlsafe_b64decode(padded.encode())
            if len(packed) <= hashlib.sha256().digest_size:
                raise ValueError
            body, signature = packed[:-32], packed[-32:]
            expected = hmac.new(self._cursor_secret, body, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            payload = json.loads(body)
            if (
                not isinstance(payload, dict)
                or payload.get("v") != 1
                or payload.get("filter_digest") != expected_filter_digest
                or not isinstance(payload.get("occurred_at"), str)
                or not isinstance(payload.get("observation_id"), str)
            ):
                raise ValueError
            occurred_at = datetime.fromisoformat(payload["occurred_at"])
            _require_aware(occurred_at, "cursor occurred_at")
            observation_id = payload["observation_id"]
            if not observation_id or len(observation_id) > 256:
                raise ValueError
            return occurred_at, observation_id
        except UnicodeError, ValueError, TypeError, json.JSONDecodeError:
            raise InvalidCursor("invalid cursor") from None


_COMMUNICATION_COLUMNS = """
    event.event_id,
    event.channel,
    event.occurred_at,
    projection.summary_zh,
    projection.original_language,
    event.data_classification,
    projection.review_status,
    event.team_ref,
    event.party_ref,
    (
      SELECT count(*)
      FROM observer.event_evidence AS evidence_count
      WHERE evidence_count.site_id = event.site_id
        AND evidence_count.event_id = event.event_id
    ),
    ARRAY(
      SELECT participant.identity_ref
      FROM observer.participants AS participant
      WHERE participant.site_id = event.site_id
        AND participant.event_id = event.event_id
      ORDER BY participant.identity_ref ASC
    )
"""


def _set_site(cursor: Any, scope: TenantScope) -> None:
    cursor.execute("SELECT set_config('app.site_id', %s, true)", (scope.site_id,))


def _summary_from_row(row: tuple[Any, ...]) -> CommunicationSummary:
    return CommunicationSummary(
        observation_id=str(row[0]),
        channel=str(row[1]),
        occurred_at=row[2],
        summary_zh=str(row[3]),
        original_language=str(row[4]),
        classification=str(row[5]),
        review_status=str(row[6]),
        team_ref=None if row[7] is None else str(row[7]),
        party_ref=None if row[8] is None else str(row[8]),
        evidence_count=int(row[9]),
        actor_refs=frozenset(str(value) for value in (row[10] or ())),
    )


def _json_list(value: Any, name: str) -> list[dict[str, object]]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise RuntimeError(f"invalid persisted {name}")
    return value
