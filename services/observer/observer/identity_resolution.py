from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from .models import TenantScope, _require_aware
from .storage import Connection

_PROVIDERS = frozenset({"email", "wecom", "whatsapp", "phone", "manual_import"})
_MAPPING_REF = re.compile(r"^EID-[0-9A-HJKMNP-TV-Z]{26}$")
_TEAM_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SUBJECT_TAIL = re.compile(r"^[A-Za-z0-9_-]{43}$")
_MAX_REVISION = 2_147_483_647

_RESOLUTION_COLUMNS = """
    site_id, identity_provider, external_subject_ref, mapping_ref,
    mapping_revision, team_ref, target_type, target_ref, status,
    resolved_at, recorded_at
"""


class IdentityResolutionConflict(ValueError):
    """A resolution replay, revision, or identity fence was rejected."""


@dataclass(frozen=True, slots=True, repr=False)
class ParticipantIdentityResolution:
    site_id: str
    identity_provider: str
    external_subject_ref: str
    mapping_ref: str
    mapping_revision: int
    team_ref: str
    target_type: str
    target_ref: str
    status: str
    resolved_at: datetime
    recorded_at: datetime

    def __post_init__(self) -> None:
        try:
            TenantScope(self.site_id, "observation_processing")
        except TypeError, ValueError:
            raise ValueError("invalid resolution site_id") from None
        if self.identity_provider not in _PROVIDERS:
            raise ValueError("invalid identity provider")
        prefix = f"extid:v1:{self.identity_provider}:"
        subject_tail = (
            self.external_subject_ref[len(prefix) :]
            if isinstance(self.external_subject_ref, str)
            and self.external_subject_ref.startswith(prefix)
            else ""
        )
        if (
            not isinstance(self.external_subject_ref, str)
            or len(self.external_subject_ref) > 160
            or not self.external_subject_ref.startswith(prefix)
            or not _SUBJECT_TAIL.fullmatch(subject_tail)
        ):
            raise ValueError("invalid external subject reference")
        if not isinstance(self.mapping_ref, str) or not _MAPPING_REF.fullmatch(self.mapping_ref):
            raise ValueError("invalid mapping reference")
        if (
            isinstance(self.mapping_revision, bool)
            or not isinstance(self.mapping_revision, int)
            or not 1 <= self.mapping_revision <= _MAX_REVISION
        ):
            raise ValueError("invalid mapping revision")
        if not isinstance(self.team_ref, str) or not _TEAM_REF.fullmatch(self.team_ref):
            raise ValueError("invalid resolution team")
        if self.target_type not in {"User", "Party"}:
            raise ValueError("invalid resolution target type")
        if not _protected_ref_is_valid(self.target_ref):
            raise ValueError("invalid protected target reference")
        if self.status not in {"confirmed", "revoked"}:
            raise ValueError("invalid resolution status")
        _require_aware(self.resolved_at, "resolved_at")
        _require_aware(self.recorded_at, "recorded_at")
        if self.recorded_at < self.resolved_at:
            raise ValueError("invalid resolution timestamps")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(site_id={self.site_id!r}, "
            f"identity_provider={self.identity_provider!r}, "
            "external_subject_ref=<redacted>, "
            f"mapping_ref={self.mapping_ref!r}, "
            f"mapping_revision={self.mapping_revision}, "
            f"team_ref={self.team_ref!r}, target_type={self.target_type!r}, "
            "target_ref=<redacted>, "
            f"status={self.status!r}, resolved_at={self.resolved_at!r}, "
            f"recorded_at={self.recorded_at!r})"
        )


class IdentityResolutionRepository(Protocol):
    def record(
        self,
        scope: TenantScope,
        resolution: ParticipantIdentityResolution,
    ) -> ParticipantIdentityResolution: ...

    def latest(
        self,
        scope: TenantScope,
        identity_provider: str,
        external_subject_ref: str,
    ) -> ParticipantIdentityResolution | None: ...

    def history(
        self,
        scope: TenantScope,
        identity_provider: str,
        external_subject_ref: str,
    ) -> tuple[ParticipantIdentityResolution, ...]: ...


class InMemoryIdentityResolutionRepository:
    __slots__ = ("_records",)

    def __init__(self) -> None:
        self._records: dict[tuple[str, str, str], list[ParticipantIdentityResolution]] = {}

    def __repr__(self) -> str:
        return "InMemoryIdentityResolutionRepository(records=<redacted>)"

    def record(
        self,
        scope: TenantScope,
        resolution: ParticipantIdentityResolution,
    ) -> ParticipantIdentityResolution:
        _validate_scope_resolution(scope, resolution)
        key = (
            scope.site_id,
            resolution.identity_provider,
            resolution.external_subject_ref,
        )
        records = self._records.setdefault(key, [])
        existing = next(
            (item for item in records if item.mapping_revision == resolution.mapping_revision),
            None,
        )
        if existing is not None:
            if _authoritative_values(existing) == _authoritative_values(resolution):
                return existing
            raise IdentityResolutionConflict("identity resolution revision conflict")
        if records:
            _validate_transition(records[-1], resolution)
        records.append(resolution)
        records.sort(key=lambda item: item.mapping_revision)
        return resolution

    def latest(
        self,
        scope: TenantScope,
        identity_provider: str,
        external_subject_ref: str,
    ) -> ParticipantIdentityResolution | None:
        _validate_lookup(scope, identity_provider, external_subject_ref)
        records = self._records.get(
            (scope.site_id, identity_provider, external_subject_ref),
            (),
        )
        return records[-1] if records else None

    def history(
        self,
        scope: TenantScope,
        identity_provider: str,
        external_subject_ref: str,
    ) -> tuple[ParticipantIdentityResolution, ...]:
        _validate_lookup(scope, identity_provider, external_subject_ref)
        return tuple(
            self._records.get(
                (scope.site_id, identity_provider, external_subject_ref),
                (),
            )
        )


class PostgresIdentityResolutionRepository:
    __slots__ = ("_connection",)

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def __repr__(self) -> str:
        return "PostgresIdentityResolutionRepository(connection=<redacted>)"

    def record(
        self,
        scope: TenantScope,
        resolution: ParticipantIdentityResolution,
    ) -> ParticipantIdentityResolution:
        _validate_scope_resolution(scope, resolution)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            _set_site(cursor, scope)
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (
                    "\x1f".join(
                        (
                            scope.site_id,
                            resolution.identity_provider,
                            resolution.external_subject_ref,
                        )
                    ),
                ),
            )
            cursor.fetchone()
            cursor.execute(
                f"""
                SELECT {_RESOLUTION_COLUMNS}
                FROM observer.participant_identity_resolutions
                WHERE site_id = %s
                  AND identity_provider = %s
                  AND external_subject_ref = %s
                ORDER BY mapping_revision ASC
                FOR UPDATE
                """,
                (
                    scope.site_id,
                    resolution.identity_provider,
                    resolution.external_subject_ref,
                ),
            )
            records = tuple(_resolution_from_row(row) for row in cursor.fetchall())
            existing = next(
                (item for item in records if item.mapping_revision == resolution.mapping_revision),
                None,
            )
            if existing is not None:
                if _authoritative_values(existing) == _authoritative_values(resolution):
                    return existing
                raise IdentityResolutionConflict("identity resolution revision conflict")
            if records:
                _validate_transition(records[-1], resolution)
            cursor.execute(
                f"""
                INSERT INTO observer.participant_identity_resolutions (
                    site_id, identity_provider, external_subject_ref,
                    mapping_ref, mapping_revision, team_ref, target_type,
                    target_ref, status, resolved_at, recorded_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING {_RESOLUTION_COLUMNS}
                """,
                _resolution_values(resolution),
            )
            row = cursor.fetchone()
            if row is None:
                raise IdentityResolutionConflict("identity resolution write rejected")
            recorded = _resolution_from_row(row)
            from .identity_projection_outbox import enqueue_resolution_projections

            enqueue_resolution_projections(cursor, recorded)
            return recorded

    def latest(
        self,
        scope: TenantScope,
        identity_provider: str,
        external_subject_ref: str,
    ) -> ParticipantIdentityResolution | None:
        _validate_lookup(scope, identity_provider, external_subject_ref)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            _set_site(cursor, scope)
            cursor.execute(
                f"""
                SELECT {_RESOLUTION_COLUMNS}
                FROM observer.participant_identity_resolutions
                WHERE site_id = %s
                  AND identity_provider = %s
                  AND external_subject_ref = %s
                ORDER BY mapping_revision DESC
                LIMIT 1
                """,
                (scope.site_id, identity_provider, external_subject_ref),
            )
            row = cursor.fetchone()
            return None if row is None else _resolution_from_row(row)

    def history(
        self,
        scope: TenantScope,
        identity_provider: str,
        external_subject_ref: str,
    ) -> tuple[ParticipantIdentityResolution, ...]:
        _validate_lookup(scope, identity_provider, external_subject_ref)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            _set_site(cursor, scope)
            cursor.execute(
                f"""
                SELECT {_RESOLUTION_COLUMNS}
                FROM observer.participant_identity_resolutions
                WHERE site_id = %s
                  AND identity_provider = %s
                  AND external_subject_ref = %s
                ORDER BY mapping_revision ASC
                """,
                (scope.site_id, identity_provider, external_subject_ref),
            )
            return tuple(_resolution_from_row(row) for row in cursor.fetchall())


def _validate_transition(
    latest: ParticipantIdentityResolution,
    candidate: ParticipantIdentityResolution,
) -> None:
    if candidate.mapping_revision < latest.mapping_revision:
        raise IdentityResolutionConflict("stale identity resolution revision")
    if (
        candidate.mapping_ref != latest.mapping_ref
        or candidate.team_ref != latest.team_ref
        or candidate.target_type != latest.target_type
        or candidate.target_ref != latest.target_ref
    ):
        raise IdentityResolutionConflict("identity resolution mapping conflict")
    if candidate.resolved_at < latest.resolved_at or candidate.recorded_at < latest.recorded_at:
        raise IdentityResolutionConflict("stale identity resolution timestamp")
    if latest.status == "revoked" and candidate.status == "confirmed":
        raise IdentityResolutionConflict("identity resolution transition rejected")


def _validate_scope_resolution(
    scope: TenantScope,
    resolution: ParticipantIdentityResolution,
) -> None:
    if resolution.site_id != scope.site_id:
        raise IdentityResolutionConflict("identity resolution site scope conflict")


def _validate_lookup(
    scope: TenantScope,
    identity_provider: str,
    external_subject_ref: str,
) -> None:
    if identity_provider not in _PROVIDERS:
        raise ValueError("invalid identity provider")
    prefix = f"extid:v1:{identity_provider}:"
    subject_tail = (
        external_subject_ref[len(prefix) :]
        if isinstance(external_subject_ref, str) and external_subject_ref.startswith(prefix)
        else ""
    )
    if (
        not isinstance(external_subject_ref, str)
        or len(external_subject_ref) > 160
        or not external_subject_ref.startswith(prefix)
        or not _SUBJECT_TAIL.fullmatch(subject_tail)
    ):
        raise ValueError("invalid external subject reference")
    if not isinstance(scope, TenantScope):
        raise TypeError("scope must be TenantScope")


def _protected_ref_is_valid(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 256
        and value == value.strip()
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _resolution_values(
    resolution: ParticipantIdentityResolution,
) -> tuple[Any, ...]:
    return (
        resolution.site_id,
        resolution.identity_provider,
        resolution.external_subject_ref,
        resolution.mapping_ref,
        resolution.mapping_revision,
        resolution.team_ref,
        resolution.target_type,
        resolution.target_ref,
        resolution.status,
        resolution.resolved_at,
        resolution.recorded_at,
    )


def _authoritative_values(
    resolution: ParticipantIdentityResolution,
) -> tuple[Any, ...]:
    return _resolution_values(resolution)[:-1]


def _resolution_from_row(row: tuple[Any, ...]) -> ParticipantIdentityResolution:
    if len(row) != 11:
        raise RuntimeError("invalid persisted identity resolution")
    return ParticipantIdentityResolution(
        site_id=str(row[0]),
        identity_provider=str(row[1]),
        external_subject_ref=str(row[2]),
        mapping_ref=str(row[3]),
        mapping_revision=int(row[4]),
        team_ref=str(row[5]),
        target_type=str(row[6]),
        target_ref=str(row[7]),
        status=str(row[8]),
        resolved_at=row[9],
        recorded_at=row[10],
    )


def _set_site(cursor: Any, scope: TenantScope) -> None:
    cursor.execute("SELECT set_config('app.site_id', %s, true)", (scope.site_id,))
