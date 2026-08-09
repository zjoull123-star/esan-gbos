from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Protocol

from .identity_resolution import IdentityResolutionRepository
from .models import TenantScope, _require_aware
from .storage import Connection

_IDENTITY_REF = re.compile(
    r"^extid:v1:(email|wecom|whatsapp|phone|manual_import):"
    r"([A-Za-z0-9][A-Za-z0-9._~-]{0,127})$"
)
_PHONE_LIKE_IDENTITY_TAIL = re.compile(r"^[0-9][0-9 ()-]{7,}[0-9]$")
_MAPPING_REF = re.compile(r"^EID-[0-9A-HJKMNP-TV-Z]{26}$")
_SUGGESTION_TYPE = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")


class InvalidCursor(ValueError):
    """The cursor is malformed or was issued for another scope/filter set."""


class ScopeMismatch(PermissionError):
    """The communication is outside the caller's team scope."""


class RawAccessDenied(PermissionError):
    """Raw communication content is not available under the caller's policy."""


class CommunicationNotFound(LookupError):
    """The site-local observation does not exist."""


@dataclass(frozen=True, slots=True, repr=False)
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

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(team_refs={sorted(self.team_refs)!r}, "
            "actor_ref=<redacted>, "
            f"allow_all_teams={self.allow_all_teams!r}, "
            f"can_read_raw={self.can_read_raw!r}, "
            f"can_read_restricted_raw={self.can_read_restricted_raw!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
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

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(observation_id={self.observation_id!r}, "
            f"channel={self.channel!r}, occurred_at={self.occurred_at!r}, "
            f"classification={self.classification!r}, "
            f"review_status={self.review_status!r}, team_ref={self.team_ref!r}, "
            "party_ref=<redacted>, actor_refs=<redacted>, "
            f"evidence_count={self.evidence_count})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ParticipantIdentityView:
    identity_ref: str
    provider: str
    status: str
    mapping_ref: str | None = None
    mapping_revision: int | None = None
    target_type: str | None = None

    def __post_init__(self) -> None:
        parsed = _parse_external_subject_ref(self.identity_ref)
        if parsed is None or parsed[0] != self.provider:
            raise ValueError("invalid participant identity view")
        if self.status not in {"unresolved", "confirmed", "revoked"}:
            raise ValueError("invalid participant identity status")
        projection_fields = (
            self.mapping_ref,
            self.mapping_revision,
            self.target_type,
        )
        if self.status == "unresolved":
            if any(value is not None for value in projection_fields):
                raise ValueError("unresolved participant identity has projection metadata")
        elif (
            not isinstance(self.mapping_ref, str)
            or _MAPPING_REF.fullmatch(self.mapping_ref) is None
            or isinstance(self.mapping_revision, bool)
            or not isinstance(self.mapping_revision, int)
            or not 1 <= self.mapping_revision <= 2_147_483_647
            or self.target_type not in {"User", "Party"}
        ):
            raise ValueError("invalid participant identity projection metadata")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(identity_ref=<redacted>, "
            f"provider={self.provider!r}, status={self.status!r}, "
            f"mapping_ref={self.mapping_ref!r}, "
            f"mapping_revision={self.mapping_revision!r}, "
            f"target_type={self.target_type!r})"
        )

    def as_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "identity_ref": self.identity_ref,
            "provider": self.provider,
            "status": self.status,
        }
        if self.mapping_ref is not None:
            value.update(
                {
                    "mapping_ref": self.mapping_ref,
                    "mapping_revision": self.mapping_revision,
                    "target_type": self.target_type,
                }
            )
        return value


@dataclass(frozen=True, slots=True, repr=False)
class CommunicationDetail:
    summary: CommunicationSummary
    evidence: tuple[dict[str, str], ...]
    fact_proposals: tuple[dict[str, object], ...]
    association_suggestions: tuple[dict[str, object], ...]
    model: dict[str, str]
    original_text: str | None
    participant_identities: tuple[ParticipantIdentityView, ...] = ()
    connector_account_user_ref: str | None = None
    raw_access_allowed: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.model, dict)
            or set(self.model) != {"name", "version"}
            or self.model.get("name") != "deepseek-v4-flash"
            or not self.model.get("version")
        ):
            raise ValueError("invalid communication model metadata")
        if (
            not isinstance(self.participant_identities, tuple)
            or not all(
                isinstance(item, ParticipantIdentityView) for item in self.participant_identities
            )
            or len({item.identity_ref for item in self.participant_identities})
            != len(self.participant_identities)
        ):
            raise ValueError("invalid participant identity views")
        if self.connector_account_user_ref is not None and not _bounded_protected_ref(
            self.connector_account_user_ref
        ):
            raise ValueError("invalid connector account user reference")
        _association_suggestions_with_keys(
            self.summary.observation_id,
            self.association_suggestions,
        )

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(summary={self.summary!r}, "
            f"evidence_count={len(self.evidence)}, "
            f"fact_proposal_count={len(self.fact_proposals)}, "
            f"association_suggestion_count={len(self.association_suggestions)}, "
            "participant_identities=<redacted>, "
            "connector_account_user_ref=<redacted>, "
            f"model={self.model!r}, original_text=<redacted>, "
            f"raw_access_allowed={self.raw_access_allowed!r})"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            **self.summary.as_dict(),
            "evidence": list(self.evidence),
            "fact_proposals": list(self.fact_proposals),
            "association_suggestions": list(
                _association_suggestions_with_keys(
                    self.summary.observation_id,
                    self.association_suggestions,
                )
            ),
            "participant_identities": [
                identity.as_dict() for identity in self.participant_identities
            ],
            "connector_account_user_ref": self.connector_account_user_ref,
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
        access: CommunicationAccess,
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
        if detail.summary.review_status != "AI Draft" or any(
            proposal.get("status") != "proposed" for proposal in detail.fact_proposals
        ):
            raise ValueError("communication projection requires AI Draft/proposed state")
        if detail.original_text is not None:
            raise ValueError("communication projection cannot persist original text")
        fact_proposals = json.dumps(
            detail.fact_proposals,
            sort_keys=True,
            separators=(",", ":"),
        )
        association_suggestions = json.dumps(
            tuple(
                _association_suggestion_payload(value) for value in detail.association_suggestions
            ),
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._connection.transaction(), self._connection.cursor() as cursor:
            _set_site(cursor, scope)
            cursor.execute(
                """
                INSERT INTO observer.communication_projections AS existing (
                  site_id, observation_event_id, summary_zh,
                  original_language, review_status, model_name,
                  model_version, fact_proposals, association_suggestions,
                  projected_at
                )
                SELECT %s, event.event_id, %s, %s, %s, %s, %s,
                       %s::jsonb, %s::jsonb, %s
                FROM observer.observation_events AS event
                WHERE event.site_id = %s
                  AND event.processing_purpose = %s
                  AND event.event_id = %s
                ON CONFLICT (site_id, observation_event_id)
                DO NOTHING
                RETURNING observation_event_id
                """,
                (
                    scope.site_id,
                    detail.summary.summary_zh,
                    detail.summary.original_language,
                    detail.summary.review_status,
                    detail.model["name"],
                    detail.model["version"],
                    fact_proposals,
                    association_suggestions,
                    projected_at,
                    scope.site_id,
                    scope.processing_purpose,
                    detail.summary.observation_id,
                ),
            )
            if cursor.fetchone() is None:
                cursor.execute(
                    """
                    SELECT
                      projection.summary_zh IS NOT DISTINCT FROM %s
                      AND projection.original_language IS NOT DISTINCT FROM %s
                      AND projection.review_status IS NOT DISTINCT FROM %s
                      AND projection.model_name IS NOT DISTINCT FROM %s
                      AND projection.model_version IS NOT DISTINCT FROM %s
                      AND projection.fact_proposals IS NOT DISTINCT FROM %s::jsonb
                      AND projection.association_suggestions
                          IS NOT DISTINCT FROM %s::jsonb
                    FROM observer.communication_projections AS projection
                    JOIN observer.observation_events AS event
                      ON event.site_id = projection.site_id
                     AND event.event_id = projection.observation_event_id
                    WHERE projection.site_id = %s
                      AND projection.observation_event_id = %s
                      AND event.processing_purpose = %s
                    """,
                    (
                        detail.summary.summary_zh,
                        detail.summary.original_language,
                        detail.summary.review_status,
                        detail.model["name"],
                        detail.model["version"],
                        fact_proposals,
                        association_suggestions,
                        scope.site_id,
                        detail.summary.observation_id,
                        scope.processing_purpose,
                    ),
                )
                existing = cursor.fetchone()
                if existing is None:
                    raise CommunicationNotFound(detail.summary.observation_id)
                if existing[0] is not True:
                    raise ValueError("communication projection idempotency conflict")

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
            "event.processing_purpose = %s",
            "(event.retention_until IS NULL OR event.retention_until > current_timestamp)",
        ]
        params: list[Any] = [scope.site_id, scope.processing_purpose]
        if not access.allow_all_teams:
            predicates.append(_confirmed_access_predicate())
            params.extend(_confirmed_access_params(access))
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
        access: CommunicationAccess,
        raw_policy: str,
    ) -> CommunicationDetail | None:
        if raw_policy not in {"omit", "nonrestricted", "all"}:
            raise ValueError("invalid raw policy")
        predicates = [
            "event.site_id = %s",
            "event.event_id = %s",
            "event.processing_purpose = %s",
            "(event.retention_until IS NULL OR event.retention_until > current_timestamp)",
        ]
        params: list[Any] = [scope.site_id, observation_id, scope.processing_purpose]
        if not access.allow_all_teams:
            predicates.append(_confirmed_access_predicate())
            params.extend(_confirmed_access_params(access))
        with self._connection.transaction(), self._connection.cursor() as cursor:
            _set_site(cursor, scope)
            cursor.execute(
                f"""
                SELECT {_COMMUNICATION_COLUMNS},
                       projection.fact_proposals,
                       projection.association_suggestions,
                       projection.model_name,
                       projection.model_version,
                       (
                         SELECT connector.account_user_ref
                         FROM observer.connector_instances AS connector
                         WHERE connector.site_id = event.site_id
                           AND connector.connector = event.connector
                           AND connector.connector_instance_id =
                               event.connector_instance_id
                       )
                FROM observer.observation_events AS event
                JOIN observer.communication_projections AS projection
                  ON projection.site_id = event.site_id
                 AND projection.observation_event_id = event.event_id
                WHERE {" AND ".join(predicates)}
                """,
                tuple(params),
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
            cursor.execute(
                """
                SELECT DISTINCT
                       participant.identity_ref,
                       split_part(participant.identity_ref, ':', 3),
                       COALESCE(latest.status, 'unresolved'),
                       latest.mapping_ref,
                       latest.mapping_revision,
                       latest.target_type
                FROM observer.participants AS participant
                LEFT JOIN LATERAL (
                  SELECT resolution.status,
                         resolution.mapping_ref,
                         resolution.mapping_revision,
                         resolution.target_type
                  FROM observer.participant_identity_resolutions AS resolution
                  WHERE resolution.site_id = participant.site_id
                    AND resolution.identity_provider = split_part(
                        participant.identity_ref, ':', 3
                    )
                    AND resolution.external_subject_ref = participant.identity_ref
                    AND resolution.team_ref = CAST(%s AS text)
                  ORDER BY resolution.mapping_revision DESC
                  LIMIT 1
                ) AS latest ON true
                WHERE participant.site_id = %s
                  AND participant.event_id = %s
                  AND participant.identity_ref ~ (
                      '^extid:v1:(email|wecom|whatsapp|phone|manual_import):'
                      || '[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$'
                  )
                  AND participant.identity_ref !~ (
                      '^extid:v1:(email|wecom|whatsapp|phone|manual_import):'
                      || '[0-9][0-9 ()-]{7,}[0-9]$'
                  )
                ORDER BY participant.identity_ref ASC
                """,
                (row[7], scope.site_id, observation_id),
            )
            participant_identity_rows = cursor.fetchall()
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
            participant_identities=tuple(
                _participant_identity_from_row(identity_row)
                for identity_row in participant_identity_rows
            ),
            connector_account_user_ref=(None if row[15] is None else str(row[15])),
        )


@dataclass(frozen=True, slots=True)
class _InMemoryCommunicationRecord:
    scope: TenantScope
    detail: CommunicationDetail
    participant_refs: tuple[str, ...]


class InMemoryCommunicationRepository:
    """Policy-parity communication repository for unit tests and offline use."""

    __slots__ = ("_identity_repository", "_records")

    def __init__(
        self,
        *,
        identity_repository: IdentityResolutionRepository,
    ) -> None:
        self._identity_repository = identity_repository
        self._records: dict[tuple[str, str, str], _InMemoryCommunicationRecord] = {}

    def __repr__(self) -> str:
        return "InMemoryCommunicationRepository(records=<redacted>)"

    def put(
        self,
        scope: TenantScope,
        detail: CommunicationDetail,
        *,
        participant_refs: tuple[str, ...],
    ) -> None:
        if not isinstance(participant_refs, tuple) or any(
            not isinstance(value, str) or not value or len(value) > 256
            for value in participant_refs
        ):
            raise ValueError("invalid participant refs")
        key = (scope.site_id, scope.processing_purpose, detail.summary.observation_id)
        if key in self._records:
            raise ValueError("communication already exists")
        self._records[key] = _InMemoryCommunicationRecord(
            scope=scope,
            detail=detail,
            participant_refs=participant_refs,
        )

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
        rows: list[CommunicationSummary] = []
        for record in self._records.values():
            if record.scope != scope:
                continue
            summary = self._project_summary(record)
            if not access.allows(summary.team_ref, summary.actor_refs):
                continue
            if channel is not None and summary.channel != channel:
                continue
            if classification is not None and summary.classification != classification:
                continue
            if review_status is not None and summary.review_status != review_status:
                continue
            if (
                before is not None
                and (
                    summary.occurred_at,
                    summary.observation_id,
                )
                >= before
            ):
                continue
            rows.append(summary)
        rows.sort(
            key=lambda item: (item.occurred_at, item.observation_id),
            reverse=True,
        )
        return tuple(rows[:limit])

    def get_communication(
        self,
        scope: TenantScope,
        observation_id: str,
        *,
        access: CommunicationAccess,
        raw_policy: str,
    ) -> CommunicationDetail | None:
        if raw_policy not in {"omit", "nonrestricted", "all"}:
            raise ValueError("invalid raw policy")
        record = self._records.get((scope.site_id, scope.processing_purpose, observation_id))
        if record is None:
            return None
        summary = self._project_summary(record)
        allowed = access.allows(summary.team_ref, summary.actor_refs)
        include_original = allowed and (
            raw_policy == "all"
            or (raw_policy == "nonrestricted" and summary.classification != "Restricted")
        )
        return replace(
            record.detail,
            summary=summary,
            original_text=(record.detail.original_text if include_original else None),
            participant_identities=self._project_participant_identities(record),
        )

    def _project_summary(
        self,
        record: _InMemoryCommunicationRecord,
    ) -> CommunicationSummary:
        actor_refs: set[str] = set()
        party_ref: str | None = None
        for participant_ref in record.participant_refs:
            parsed = _parse_external_subject_ref(participant_ref)
            if parsed is None:
                continue
            provider, subject_ref = parsed
            resolution = self._identity_repository.latest(
                record.scope,
                provider,
                subject_ref,
            )
            if (
                resolution is None
                or resolution.status != "confirmed"
                or resolution.team_ref != record.detail.summary.team_ref
            ):
                continue
            if resolution.target_type == "User":
                actor_refs.add(resolution.target_ref)
            elif resolution.target_type == "Party" and party_ref is None:
                party_ref = resolution.target_ref
        return replace(
            record.detail.summary,
            party_ref=party_ref,
            actor_refs=frozenset(actor_refs),
        )

    def _project_participant_identities(
        self,
        record: _InMemoryCommunicationRecord,
    ) -> tuple[ParticipantIdentityView, ...]:
        identities: list[ParticipantIdentityView] = []
        seen: set[str] = set()
        for participant_ref in sorted(record.participant_refs):
            parsed = _parse_external_subject_ref(participant_ref)
            if parsed is None or participant_ref in seen:
                continue
            seen.add(participant_ref)
            provider, subject_ref = parsed
            resolution = self._identity_repository.latest(
                record.scope,
                provider,
                subject_ref,
            )
            if resolution is None or resolution.team_ref != record.detail.summary.team_ref:
                identities.append(
                    ParticipantIdentityView(
                        identity_ref=participant_ref,
                        provider=provider,
                        status="unresolved",
                    )
                )
                continue
            identities.append(
                ParticipantIdentityView(
                    identity_ref=participant_ref,
                    provider=provider,
                    status=resolution.status,
                    mapping_ref=resolution.mapping_ref,
                    mapping_revision=resolution.mapping_revision,
                    target_type=resolution.target_type,
                )
            )
        return tuple(identities)


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
            access=access,
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
            association_suggestions=_association_suggestions_with_keys(
                detail.summary.observation_id,
                detail.association_suggestions,
            ),
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
    (
      SELECT latest_party.target_ref
      FROM observer.participants AS party_participant
      CROSS JOIN LATERAL (
        SELECT resolution.target_ref,
               resolution.target_type,
               resolution.status,
               resolution.team_ref
        FROM observer.participant_identity_resolutions AS resolution
        WHERE resolution.site_id = party_participant.site_id
          AND resolution.external_subject_ref = party_participant.identity_ref
          AND resolution.identity_provider = split_part(
              party_participant.identity_ref, ':', 3
          )
        ORDER BY resolution.mapping_revision DESC
        LIMIT 1
      ) AS latest_party
      WHERE party_participant.site_id = event.site_id
        AND party_participant.event_id = event.event_id
        AND latest_party.status = 'confirmed'
        AND latest_party.target_type = 'Party'
        AND latest_party.team_ref = event.team_ref
      ORDER BY party_participant.participant_id ASC
      LIMIT 1
    ),
    (
      SELECT count(*)
      FROM observer.event_evidence AS evidence_count
      WHERE evidence_count.site_id = event.site_id
        AND evidence_count.event_id = event.event_id
    ),
    ARRAY(
      SELECT DISTINCT latest_actor.target_ref
      FROM observer.participants AS actor_participant
      CROSS JOIN LATERAL (
        SELECT resolution.target_ref,
               resolution.target_type,
               resolution.status,
               resolution.team_ref
        FROM observer.participant_identity_resolutions AS resolution
        WHERE resolution.site_id = actor_participant.site_id
          AND resolution.external_subject_ref = actor_participant.identity_ref
          AND resolution.identity_provider = split_part(
              actor_participant.identity_ref, ':', 3
          )
        ORDER BY resolution.mapping_revision DESC
        LIMIT 1
      ) AS latest_actor
      WHERE actor_participant.site_id = event.site_id
        AND actor_participant.event_id = event.event_id
        AND latest_actor.status = 'confirmed'
        AND latest_actor.target_type = 'User'
        AND latest_actor.team_ref = event.team_ref
      ORDER BY latest_actor.target_ref ASC
    )
"""


def _confirmed_access_predicate() -> str:
    return """
        (
          event.team_ref = ANY(%s)
          OR (
            CAST(%s AS text) IS NOT NULL
            AND EXISTS (
              SELECT 1
              FROM observer.participants AS actor
              CROSS JOIN LATERAL (
                SELECT resolution.target_ref,
                       resolution.target_type,
                       resolution.status,
                       resolution.team_ref
                FROM observer.participant_identity_resolutions AS resolution
                WHERE resolution.site_id = actor.site_id
                  AND resolution.external_subject_ref = actor.identity_ref
                  AND resolution.identity_provider = split_part(
                      actor.identity_ref, ':', 3
                  )
                ORDER BY resolution.mapping_revision DESC
                LIMIT 1
              ) AS latest_actor
              WHERE actor.site_id = event.site_id
                AND actor.event_id = event.event_id
                AND latest_actor.status = 'confirmed'
                AND latest_actor.target_type = 'User'
                AND latest_actor.team_ref = event.team_ref
                AND latest_actor.target_ref = %s
            )
          )
        )
    """


def _confirmed_access_params(access: CommunicationAccess) -> list[Any]:
    return [sorted(access.team_refs), access.actor_ref, access.actor_ref]


def _parse_external_subject_ref(value: str) -> tuple[str, str] | None:
    match = _IDENTITY_REF.fullmatch(value)
    if match is None or _PHONE_LIKE_IDENTITY_TAIL.fullmatch(match.group(2)):
        return None
    return match.group(1), value


def _participant_identity_from_row(row: tuple[Any, ...]) -> ParticipantIdentityView:
    if len(row) != 6:
        raise RuntimeError("invalid persisted participant identity view")
    return ParticipantIdentityView(
        identity_ref=str(row[0]),
        provider=str(row[1]),
        status=str(row[2]),
        mapping_ref=None if row[3] is None else str(row[3]),
        mapping_revision=None if row[4] is None else int(row[4]),
        target_type=None if row[5] is None else str(row[5]),
    )


def _association_suggestions_with_keys(
    observation_id: str,
    suggestions: tuple[dict[str, object], ...],
) -> tuple[dict[str, object], ...]:
    if not isinstance(suggestions, tuple) or len(suggestions) > 100:
        raise ValueError("invalid association suggestions")
    keyed: list[dict[str, object]] = []
    for suggestion in suggestions:
        payload = _association_suggestion_payload(suggestion)
        material = json.dumps(
            {"observation_id": observation_id, "suggestion": payload},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        keyed.append(
            {
                **payload,
                "suggestion_key": f"suggestion:v1:{hashlib.sha256(material).hexdigest()}",
            }
        )
    return tuple(keyed)


def _association_suggestion_payload(
    suggestion: dict[str, object],
) -> dict[str, object]:
    if not isinstance(suggestion, dict) or not set(suggestion).issubset(
        {"type", "target_ref", "confidence", "suggestion_key"}
    ):
        raise ValueError("invalid association suggestion")
    if not {"type", "target_ref", "confidence"}.issubset(suggestion):
        raise ValueError("invalid association suggestion")
    suggestion_type = suggestion["type"]
    target_ref = suggestion["target_ref"]
    confidence = suggestion["confidence"]
    if (
        not isinstance(suggestion_type, str)
        or _SUGGESTION_TYPE.fullmatch(suggestion_type) is None
        or not _bounded_protected_ref(target_ref)
        or isinstance(confidence, bool)
        or not isinstance(confidence, int | float)
        or not math.isfinite(confidence)
        or not 0 <= confidence <= 1
    ):
        raise ValueError("invalid association suggestion")
    return {
        "type": suggestion_type,
        "target_ref": target_ref,
        "confidence": float(confidence),
    }


def _bounded_protected_ref(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 256
        and value == value.strip()
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


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
