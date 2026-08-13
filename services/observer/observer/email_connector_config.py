"""Idempotent, site-scoped application of Gateway mailbox connector projections."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .models import TenantScope, _require_aware, stable_ulid
from .storage import Connection

_SITE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,139}$")
_ULID_REF = re.compile(r"^(?P<prefix>[A-Z]{3})-[0-9A-HJKMNP-TV-Z]{26}$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_EMAIL_IDENTITY_REF = re.compile(r"^extid:v1:email:[A-Za-z0-9_-]{43}$")
_SECRET_REF = re.compile(r"^secretref:v1/[A-Za-z0-9][A-Za-z0-9._/-]*$")
_PROVIDERS = frozenset({"imap_smtp", "wecom_app_mail"})
_ENTRY_ROLES = frozenset({"primary", "workflow", "migration", "selective_archive"})
_BUSINESS_PURPOSES = frozenset(
    {
        "business_operations",
        "observation_processing",
        "entity_resolution",
        "customer_service",
        "sales_follow_up",
        "procurement_coordination",
        "product_sample_management",
        "risk_review",
        "metric_reporting",
        "audit_compliance",
    }
)
_PROJECTION_V1_FIELDS = frozenset(
    {
        "site_id",
        "observer_connector_instance_ref",
        "provider_kind",
        "entry_role",
        "business_purpose",
        "team_ref",
        "credential_ref",
        "inbound_enabled",
        "activation_watermark",
        "projection_revision",
        "projection_digest",
    }
)
_PROJECTION_V2_FIELDS = _PROJECTION_V1_FIELDS | {"mailbox_address_identity_ref"}
_WATERMARK_FIELDS = frozenset({"mailbox_id", "mailbox_config_revision", "not_before"})


class EmailConnectorConfigConflict(ValueError):
    """A projection revision or publication reference was reused inconsistently."""


class EmailConnectorConfigUnavailable(RuntimeError):
    """The protected configuration repository could not complete the request."""


@dataclass(frozen=True, slots=True, repr=False)
class EmailConnectorConfigProjection:
    site_id: str
    observer_connector_instance_ref: str
    provider_kind: str
    entry_role: str
    business_purpose: str
    team_ref: str
    credential_ref: str
    inbound_enabled: bool
    mailbox_ref: str
    mailbox_config_revision: int
    activation_not_before: datetime
    projection_revision: int
    projection_digest: str
    mailbox_address_identity_ref: str | None = None

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> EmailConnectorConfigProjection:
        if not isinstance(value, Mapping) or set(value) not in {
            _PROJECTION_V1_FIELDS,
            _PROJECTION_V2_FIELDS,
        }:
            raise ValueError("invalid mailbox connector projection")
        watermark = value.get("activation_watermark")
        if not isinstance(watermark, Mapping) or set(watermark) != _WATERMARK_FIELDS:
            raise ValueError("invalid activation watermark")
        site_id = value.get("site_id")
        instance_ref = value.get("observer_connector_instance_ref")
        provider_kind = value.get("provider_kind")
        entry_role = value.get("entry_role")
        business_purpose = value.get("business_purpose")
        team_ref = value.get("team_ref")
        credential_ref = value.get("credential_ref")
        inbound_enabled = value.get("inbound_enabled")
        mailbox_ref = watermark.get("mailbox_id")
        mailbox_revision = watermark.get("mailbox_config_revision")
        not_before = watermark.get("not_before")
        projection_revision = value.get("projection_revision")
        projection_digest = value.get("projection_digest")
        mailbox_address_identity_ref = value.get("mailbox_address_identity_ref")
        if not isinstance(site_id, str) or _SITE.fullmatch(site_id) is None:
            raise ValueError("invalid projection site")
        validated_instance_ref = _require_ref(instance_ref, "OCI", "connector instance ref")
        if provider_kind not in _PROVIDERS:
            raise ValueError("invalid projection provider")
        if entry_role not in _ENTRY_ROLES:
            raise ValueError("invalid projection entry role")
        if business_purpose not in _BUSINESS_PURPOSES:
            raise ValueError("invalid projection business purpose")
        validated_team_ref = _require_ref(team_ref, "TEM", "team ref")
        if (
            not isinstance(credential_ref, str)
            or len(credential_ref) > 128
            or _SECRET_REF.fullmatch(credential_ref) is None
        ):
            raise ValueError("invalid credential reference")
        if not isinstance(inbound_enabled, bool):
            raise ValueError("invalid inbound switch")
        validated_mailbox_ref = _require_ref(mailbox_ref, "MBX", "mailbox ref")
        if (
            not isinstance(mailbox_revision, int)
            or isinstance(mailbox_revision, bool)
            or not 1 <= mailbox_revision <= 2_147_483_647
            or projection_revision != mailbox_revision
        ):
            raise ValueError("invalid projection revision")
        if not isinstance(not_before, str) or not 20 <= len(not_before) <= 35:
            raise ValueError("invalid activation time")
        try:
            activation_not_before = datetime.fromisoformat(not_before.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("invalid activation time") from exc
        _require_aware(activation_not_before, "activation_not_before")
        if not isinstance(projection_digest, str) or _DIGEST.fullmatch(projection_digest) is None:
            raise ValueError("invalid projection digest")
        if set(value) == _PROJECTION_V2_FIELDS and (
            not isinstance(mailbox_address_identity_ref, str)
            or _EMAIL_IDENTITY_REF.fullmatch(mailbox_address_identity_ref) is None
        ):
            raise ValueError("invalid mailbox address identity ref")
        digest_payload = {key: value[key] for key in value if key != "projection_digest"}
        if not _constant_digest_equal(projection_digest, _canonical_digest(digest_payload)):
            raise ValueError("projection digest mismatch")
        return cls(
            site_id=site_id,
            observer_connector_instance_ref=validated_instance_ref,
            provider_kind=provider_kind,
            entry_role=entry_role,
            business_purpose=business_purpose,
            team_ref=validated_team_ref,
            credential_ref=credential_ref,
            inbound_enabled=inbound_enabled,
            mailbox_ref=validated_mailbox_ref,
            mailbox_config_revision=mailbox_revision,
            activation_not_before=activation_not_before.astimezone(UTC),
            projection_revision=projection_revision,
            projection_digest=projection_digest,
            mailbox_address_identity_ref=(
                mailbox_address_identity_ref
                if isinstance(mailbox_address_identity_ref, str)
                else None
            ),
        )

    def comparable(self) -> tuple[object, ...]:
        return (
            self.site_id,
            self.observer_connector_instance_ref,
            self.provider_kind,
            self.entry_role,
            self.business_purpose,
            self.team_ref,
            self.credential_ref,
            self.inbound_enabled,
            self.mailbox_ref,
            self.mailbox_config_revision,
            self.activation_not_before,
            self.projection_revision,
            self.projection_digest,
            self.mailbox_address_identity_ref,
        )

    def __repr__(self) -> str:
        return (
            "EmailConnectorConfigProjection("
            f"site_id={self.site_id!r}, mailbox_ref={self.mailbox_ref!r}, "
            f"provider_kind={self.provider_kind!r}, "
            f"projection_revision={self.projection_revision}, "
            "credential_ref=<redacted>, mailbox_address_identity_ref=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class EmailConnectorConfigReceipt:
    receipt_ref: str
    config_publication_ref: str
    payload_digest: str
    projection_revision: int
    replayed: bool

    def to_wire(self) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "receipt_ref": self.receipt_ref,
            "config_publication_ref": self.config_publication_ref,
            "payload_digest": self.payload_digest,
        }

    def __repr__(self) -> str:
        return (
            "EmailConnectorConfigReceipt("
            f"receipt_ref={self.receipt_ref!r}, "
            f"config_publication_ref={self.config_publication_ref!r}, "
            f"projection_revision={self.projection_revision}, "
            f"replayed={self.replayed})"
        )


class InMemoryEmailConnectorConfigRepository:
    """Deterministic test repository with the same revision fences as PostgreSQL."""

    def __init__(self) -> None:
        self._rows: dict[
            tuple[str, str, int],
            tuple[str, EmailConnectorConfigProjection],
        ] = {}

    @property
    def projections(self) -> tuple[EmailConnectorConfigProjection, ...]:
        return tuple(value[1] for value in self._rows.values())

    def apply(
        self,
        *,
        config_publication_ref: str,
        projection: Mapping[str, object],
        projected_at: datetime,
    ) -> EmailConnectorConfigReceipt:
        _require_ref(config_publication_ref, "MCP", "config publication ref")
        _require_aware(projected_at, "projected_at")
        candidate = EmailConnectorConfigProjection.from_wire(projection)
        key = (candidate.site_id, candidate.mailbox_ref, candidate.projection_revision)
        existing = self._rows.get(key)
        if existing is not None:
            if existing != (config_publication_ref, candidate):
                raise EmailConnectorConfigConflict("mailbox projection revision conflict")
            return _receipt(config_publication_ref, candidate, replayed=True)
        related = [
            row
            for row in self._rows.values()
            if row[1].site_id == candidate.site_id and row[1].mailbox_ref == candidate.mailbox_ref
        ]
        if related:
            latest = max((row[1] for row in related), key=lambda row: row.projection_revision)
            if (
                candidate.projection_revision <= latest.projection_revision
                or candidate.observer_connector_instance_ref
                != latest.observer_connector_instance_ref
            ):
                raise EmailConnectorConfigConflict("mailbox projection fence rejected")
        if any(row[0] == config_publication_ref for row in self._rows.values()):
            raise EmailConnectorConfigConflict("config publication reference conflict")
        self._rows[key] = (config_publication_ref, candidate)
        return _receipt(config_publication_ref, candidate, replayed=False)


class PostgresEmailConnectorConfigRepository:
    """Apply one Gateway projection atomically using only the Observer connection."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def __repr__(self) -> str:
        return "PostgresEmailConnectorConfigRepository(connection=<redacted>)"

    def apply(
        self,
        *,
        config_publication_ref: str,
        projection: Mapping[str, object],
        projected_at: datetime,
    ) -> EmailConnectorConfigReceipt:
        _require_ref(config_publication_ref, "MCP", "config publication ref")
        _require_aware(projected_at, "projected_at")
        candidate = EmailConnectorConfigProjection.from_wire(projection)
        scope = TenantScope(candidate.site_id, "observation_processing")
        try:
            with self._connection.transaction(), self._connection.cursor() as cursor:
                cursor.execute("SELECT set_config('app.site_id', %s, true)", (scope.site_id,))
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"email-config:{scope.site_id}:{candidate.mailbox_ref}",),
                )
                cursor.execute(
                    """
                    SELECT config_publication_ref, connector_instance_id, provider_kind,
                           entry_role, business_purpose, team_ref, credential_ref,
                           inbound_enabled, activation_not_before,
                           projection_revision, projection_digest,
                           mailbox_address_identity_ref
                      FROM observer.email_connector_config_projections
                     WHERE site_id = %s AND mailbox_id = %s
                       AND mailbox_config_revision = %s
                    """,
                    (
                        scope.site_id,
                        candidate.mailbox_ref,
                        candidate.projection_revision,
                    ),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    if not _row_matches(existing, config_publication_ref, candidate):
                        raise EmailConnectorConfigConflict("mailbox projection revision conflict")
                    return _receipt(config_publication_ref, candidate, replayed=True)
                cursor.execute(
                    """
                    SELECT config_publication_ref, connector_instance_id,
                           projection_revision
                      FROM observer.email_connector_config_projections
                     WHERE site_id = %s AND mailbox_id = %s
                     ORDER BY projection_revision DESC
                     LIMIT 1
                    """,
                    (scope.site_id, candidate.mailbox_ref),
                )
                latest = cursor.fetchone()
                if latest is not None and (
                    candidate.projection_revision <= int(latest[2])
                    or candidate.observer_connector_instance_ref != str(latest[1])
                ):
                    raise EmailConnectorConfigConflict("mailbox projection fence rejected")
                cursor.execute(
                    """
                    SELECT 1
                      FROM observer.email_connector_config_projections
                     WHERE site_id = %s AND config_publication_ref = %s
                    """,
                    (scope.site_id, config_publication_ref),
                )
                if cursor.fetchone() is not None:
                    raise EmailConnectorConfigConflict("config publication reference conflict")
                status = "healthy" if candidate.inbound_enabled else "paused"
                task_type = _agent_task_type(candidate.business_purpose)
                cursor.execute(
                    """
                    INSERT INTO observer.connector_instances (
                        site_id, connector, connector_instance_id, status,
                        registered_at, updated_at, team_ref, agent_task_type,
                        account_user_ref
                    ) VALUES (%s, 'email', %s, %s, %s, %s, %s, %s, NULL)
                    ON CONFLICT (site_id, connector, connector_instance_id)
                    DO UPDATE SET status = EXCLUDED.status,
                                  updated_at = EXCLUDED.updated_at,
                                  team_ref = EXCLUDED.team_ref,
                                  agent_task_type = EXCLUDED.agent_task_type,
                                  account_user_ref = NULL
                    """,
                    (
                        scope.site_id,
                        candidate.observer_connector_instance_ref,
                        status,
                        projected_at,
                        projected_at,
                        candidate.team_ref,
                        task_type,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO observer.connector_checkpoints (
                        site_id, connector, connector_instance_id, checkpoint_id,
                        checkpoint_version, replay_window_seconds, status, updated_at
                    ) VALUES (%s, 'email', %s, %s, 0, 0, %s, %s)
                    ON CONFLICT (site_id, connector, connector_instance_id)
                    DO UPDATE SET status = EXCLUDED.status,
                                  updated_at = EXCLUDED.updated_at
                    """,
                    (
                        scope.site_id,
                        candidate.observer_connector_instance_ref,
                        f"email:{candidate.observer_connector_instance_ref}",
                        status,
                        projected_at,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO observer.email_connector_config_projections (
                        site_id, mailbox_id, mailbox_config_revision,
                        config_publication_ref, connector, connector_instance_id,
                        provider_kind, entry_role, business_purpose, team_ref,
                        credential_ref, inbound_enabled, activation_watermark,
                        activation_not_before, projection_revision,
                        projection_digest, projected_at,
                        mailbox_address_identity_ref
                    ) VALUES (
                        %s, %s, %s, %s, 'email', %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        scope.site_id,
                        candidate.mailbox_ref,
                        candidate.mailbox_config_revision,
                        config_publication_ref,
                        candidate.observer_connector_instance_ref,
                        candidate.provider_kind,
                        candidate.entry_role,
                        candidate.business_purpose,
                        candidate.team_ref,
                        candidate.credential_ref,
                        candidate.inbound_enabled,
                        json.dumps(
                            {
                                "mailbox_id": candidate.mailbox_ref,
                                "mailbox_config_revision": candidate.mailbox_config_revision,
                                "not_before": candidate.activation_not_before.isoformat().replace(
                                    "+00:00", "Z"
                                ),
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        candidate.activation_not_before,
                        candidate.projection_revision,
                        candidate.projection_digest.removeprefix("sha256:"),
                        projected_at,
                        candidate.mailbox_address_identity_ref,
                    ),
                )
                if candidate.inbound_enabled:
                    from .identity_projection_outbox import seed_current_resolutions_for_config

                    seed_current_resolutions_for_config(
                        cursor,
                        site_id=scope.site_id,
                        team_ref=candidate.team_ref,
                        processing_purpose=candidate.business_purpose,
                    )
        except EmailConnectorConfigConflict:
            raise
        except Exception as exc:
            raise EmailConnectorConfigUnavailable(
                "email connector configuration unavailable"
            ) from exc
        return _receipt(config_publication_ref, candidate, replayed=False)


def _require_ref(value: object, prefix: str, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid {name}")
    match = _ULID_REF.fullmatch(value)
    if match is None or match.group("prefix") != prefix:
        raise ValueError(f"invalid {name}")
    return value


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _constant_digest_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)


def _receipt(
    publication_ref: str,
    projection: EmailConnectorConfigProjection,
    *,
    replayed: bool,
) -> EmailConnectorConfigReceipt:
    return EmailConnectorConfigReceipt(
        receipt_ref="OCP-"
        + stable_ulid(
            "email-config-projection-receipt",
            projection.site_id,
            publication_ref,
            projection.projection_digest,
        ),
        config_publication_ref=publication_ref,
        payload_digest=projection.projection_digest,
        projection_revision=projection.projection_revision,
        replayed=replayed,
    )


def _row_matches(
    row: tuple[Any, ...],
    publication_ref: str,
    projection: EmailConnectorConfigProjection,
) -> bool:
    return (
        str(row[0]) == publication_ref
        and str(row[1]) == projection.observer_connector_instance_ref
        and str(row[2]) == projection.provider_kind
        and str(row[3]) == projection.entry_role
        and str(row[4]) == projection.business_purpose
        and str(row[5]) == projection.team_ref
        and str(row[6]) == projection.credential_ref
        and bool(row[7]) is projection.inbound_enabled
        and row[8] == projection.activation_not_before
        and int(row[9]) == projection.projection_revision
        and str(row[10]) == projection.projection_digest.removeprefix("sha256:")
        and (None if row[11] is None else str(row[11])) == projection.mailbox_address_identity_ref
    )


def _agent_task_type(business_purpose: str) -> str | None:
    return {
        "sales_follow_up": "sales",
        "procurement_coordination": "purchase",
        "product_sample_management": "product_sample",
    }.get(business_purpose)


__all__ = [
    "EmailConnectorConfigConflict",
    "EmailConnectorConfigProjection",
    "EmailConnectorConfigReceipt",
    "EmailConnectorConfigUnavailable",
    "InMemoryEmailConnectorConfigRepository",
    "PostgresEmailConnectorConfigRepository",
]
