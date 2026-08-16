"""Content-minimized durable wake signals for Observer email connectors."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

from .models import TenantScope, _require_aware, stable_ulid
from .storage import Connection

_SITE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,139}$")
_REF = re.compile(r"^(?P<prefix>[A-Z]{3})-[0-9A-HJKMNP-TV-Z]{26}$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_IDEMPOTENCY = re.compile(r"^email-signal:[a-f0-9]{64}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "site_id",
        "signal_kind",
        "observer_connector_instance_ref",
        "activation_watermark",
        "count_hint",
        "callback_timestamp",
        "payload_digest",
        "nonce_digest",
        "replay_key_digest",
        "idempotency_key",
    }
)
_WATERMARK_FIELDS = frozenset({"mailbox_id", "mailbox_config_revision", "not_before"})


class EmailSignalConflict(ValueError):
    """A replay or idempotency identity was reused with different content."""


class EmailSignalUnavailable(RuntimeError):
    """The protected signal repository could not complete a request."""


@dataclass(frozen=True, slots=True, repr=False)
class CurrentEmailConnectorConfig:
    site_id: str
    observer_connector_instance_ref: str
    provider_kind: str
    mailbox_ref: str
    mailbox_config_revision: int
    activation_not_before: datetime
    inbound_enabled: bool

    def __post_init__(self) -> None:
        if (
            _SITE.fullmatch(self.site_id) is None
            or _require_ref(self.observer_connector_instance_ref, "OCI") is None
            or self.provider_kind not in {"wecom_app_mail", "imap_smtp"}
            or _require_ref(self.mailbox_ref, "MBX") is None
            or isinstance(self.mailbox_config_revision, bool)
            or not isinstance(self.mailbox_config_revision, int)
            or not 1 <= self.mailbox_config_revision <= 2_147_483_647
            or not isinstance(self.inbound_enabled, bool)
        ):
            raise ValueError("invalid current email connector config")
        _require_aware(self.activation_not_before, "activation_not_before")


@dataclass(frozen=True, slots=True, repr=False)
class EmailSignalRequest:
    site_id: str
    signal_kind: Literal["callback", "reconciliation"]
    observer_connector_instance_ref: str
    mailbox_ref: str
    mailbox_config_revision: int
    activation_not_before: datetime
    count_hint: int | None
    callback_timestamp: datetime | None
    payload_digest: str
    nonce_digest: str | None
    replay_key_digest: str
    idempotency_key: str

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> EmailSignalRequest:
        if not isinstance(value, Mapping) or set(value) != _REQUEST_FIELDS:
            raise ValueError("invalid email signal request")
        if value.get("schema_version") != "1.0":
            raise ValueError("invalid email signal request")
        watermark = value.get("activation_watermark")
        if not isinstance(watermark, Mapping) or set(watermark) != _WATERMARK_FIELDS:
            raise ValueError("invalid email signal activation watermark")
        site_id = value.get("site_id")
        signal_kind = value.get("signal_kind")
        instance_ref = value.get("observer_connector_instance_ref")
        mailbox_ref = watermark.get("mailbox_id")
        revision = watermark.get("mailbox_config_revision")
        not_before = _wire_datetime(watermark.get("not_before"), "activation_not_before")
        count_hint = value.get("count_hint")
        callback_value = value.get("callback_timestamp")
        callback_timestamp = (
            None if callback_value is None else _wire_datetime(callback_value, "callback_timestamp")
        )
        payload_digest = value.get("payload_digest")
        nonce_digest = value.get("nonce_digest")
        replay_key_digest = value.get("replay_key_digest")
        idempotency_key = value.get("idempotency_key")
        if (
            not isinstance(site_id, str)
            or _SITE.fullmatch(site_id) is None
            or signal_kind not in {"callback", "reconciliation"}
            or not isinstance(instance_ref, str)
            or _require_ref(instance_ref, "OCI") is None
            or not isinstance(mailbox_ref, str)
            or _require_ref(mailbox_ref, "MBX") is None
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or not 1 <= revision <= 2_147_483_647
            or not isinstance(payload_digest, str)
            or _DIGEST.fullmatch(payload_digest) is None
            or not isinstance(replay_key_digest, str)
            or _DIGEST.fullmatch(replay_key_digest) is None
            or not isinstance(idempotency_key, str)
            or _IDEMPOTENCY.fullmatch(idempotency_key) is None
        ):
            raise ValueError("invalid email signal request")
        if signal_kind == "callback":
            if (
                isinstance(count_hint, bool)
                or not isinstance(count_hint, int)
                or not 0 <= count_hint <= 4_294_967_295
                or callback_timestamp is None
                or not isinstance(nonce_digest, str)
                or _DIGEST.fullmatch(nonce_digest) is None
            ):
                raise ValueError("invalid callback signal")
        elif count_hint is not None or callback_timestamp is not None or nonce_digest is not None:
            raise ValueError("invalid reconciliation signal")
        return cls(
            site_id=site_id,
            signal_kind=cast(Literal["callback", "reconciliation"], signal_kind),
            observer_connector_instance_ref=instance_ref,
            mailbox_ref=mailbox_ref,
            mailbox_config_revision=revision,
            activation_not_before=not_before,
            count_hint=cast(int | None, count_hint),
            callback_timestamp=callback_timestamp,
            payload_digest=payload_digest,
            nonce_digest=cast(str | None, nonce_digest),
            replay_key_digest=replay_key_digest,
            idempotency_key=idempotency_key,
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "site_id": self.site_id,
            "signal_kind": self.signal_kind,
            "observer_connector_instance_ref": self.observer_connector_instance_ref,
            "activation_watermark": {
                "mailbox_id": self.mailbox_ref,
                "mailbox_config_revision": self.mailbox_config_revision,
                "not_before": _wire_time(self.activation_not_before),
            },
            "count_hint": self.count_hint,
            "callback_timestamp": (
                None if self.callback_timestamp is None else _wire_time(self.callback_timestamp)
            ),
            "payload_digest": self.payload_digest,
            "nonce_digest": self.nonce_digest,
            "replay_key_digest": self.replay_key_digest,
            "idempotency_key": self.idempotency_key,
        }

    @property
    def signal_digest(self) -> str:
        return _canonical_digest(self.to_wire())

    def __repr__(self) -> str:
        return (
            "EmailSignalRequest("
            f"site_id={self.site_id!r}, signal_kind={self.signal_kind!r}, "
            f"mailbox_config_revision={self.mailbox_config_revision}, "
            "identifiers=<redacted>, digests=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class EmailSignalReceipt:
    signal_receipt_ref: str
    payload_digest: str

    def to_wire(self) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "signal_receipt_ref": self.signal_receipt_ref,
            "payload_digest": self.payload_digest,
        }

    def __repr__(self) -> str:
        return "EmailSignalReceipt(signal_receipt_ref=<redacted>, payload_digest=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class EmailSignalLease:
    request: EmailSignalRequest
    signal_ref: str
    worker_id: str
    attempt_count: int
    lease_generation: int
    lease_expires_at: datetime

    def __repr__(self) -> str:
        return (
            "EmailSignalLease("
            f"signal_kind={self.request.signal_kind!r}, attempt_count={self.attempt_count}, "
            f"lease_generation={self.lease_generation}, identifiers=<redacted>)"
        )


class InMemoryEmailSignalRepository:
    def __init__(self, *, configs: Sequence[CurrentEmailConnectorConfig]) -> None:
        self._configs = tuple(configs)
        self._signals: dict[str, tuple[EmailSignalRequest, EmailSignalReceipt]] = {}
        self._idempotency: dict[str, str] = {}

    @property
    def signals(self) -> tuple[EmailSignalRequest, ...]:
        return tuple(value[0] for value in self._signals.values())

    def accept(
        self,
        scope: TenantScope,
        *,
        request: EmailSignalRequest,
        accepted_at: datetime,
    ) -> EmailSignalReceipt:
        _require_aware(accepted_at, "accepted_at")
        _validate_binding(scope, request, self._configs)
        existing = self._signals.get(request.replay_key_digest)
        if existing is not None:
            if existing[0] != request:
                raise EmailSignalConflict("signal replay digest drift")
            return existing[1]
        existing_replay = self._idempotency.get(request.idempotency_key)
        if existing_replay is not None:
            raise EmailSignalConflict("signal idempotency drift")
        receipt = _receipt(request)
        self._signals[request.replay_key_digest] = (request, receipt)
        self._idempotency[request.idempotency_key] = request.replay_key_digest
        return receipt


class PostgresEmailSignalRepository:
    """Accept and lease signals through the current Observer application role."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def __repr__(self) -> str:
        return "PostgresEmailSignalRepository(connection=<redacted>)"

    def preflight(self) -> None:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*) = 2,
                       bool_and(c.relrowsecurity), bool_and(c.relforcerowsecurity),
                       bool_and(has_table_privilege(current_user, c.oid, 'SELECT')),
                       bool_or(has_table_privilege(current_user, c.oid, 'DELETE'))
                  FROM pg_class AS c
                  JOIN pg_namespace AS n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'observer'
                   AND c.relname IN ('email_signals', 'email_signal_work')
                """
            )
            row = cursor.fetchone()
        if row != (True, True, True, True, False):
            raise ValueError("email signal repository preflight failed")

    def accept(
        self,
        scope: TenantScope,
        *,
        request: EmailSignalRequest,
        accepted_at: datetime,
    ) -> EmailSignalReceipt:
        _require_aware(accepted_at, "accepted_at")
        if scope.site_id != request.site_id:
            raise PermissionError("signal site scope mismatch")
        receipt = _receipt(request)
        try:
            with self._connection.transaction(), self._connection.cursor() as cursor:
                _set_site(cursor, scope)
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"email-signal:{scope.site_id}:{request.replay_key_digest}",),
                )
                config = _current_config(cursor, request)
                _validate_binding(scope, request, (config,))
                cursor.execute(
                    """
                    SELECT signal_ref, payload_digest, signal_digest
                      FROM observer.email_signals
                     WHERE site_id = %s
                       AND (replay_key_digest = %s OR idempotency_key = %s)
                    """,
                    (
                        scope.site_id,
                        request.replay_key_digest.removeprefix("sha256:"),
                        request.idempotency_key,
                    ),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    if not hmac.compare_digest(
                        str(existing[2]), request.signal_digest.removeprefix("sha256:")
                    ):
                        raise EmailSignalConflict("signal replay digest drift")
                    return EmailSignalReceipt(
                        signal_receipt_ref=str(existing[0]),
                        payload_digest="sha256:" + str(existing[1]),
                    )
                cursor.execute(
                    """
                    INSERT INTO observer.email_signals (
                        site_id, signal_ref, signal_kind, connector,
                        connector_instance_id, mailbox_id, mailbox_config_revision,
                        activation_not_before, count_hint, callback_timestamp,
                        payload_digest, signal_digest, nonce_digest,
                        replay_key_digest, idempotency_key, accepted_at
                    ) VALUES (
                        %s, %s, %s, 'email', %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        scope.site_id,
                        receipt.signal_receipt_ref,
                        request.signal_kind,
                        request.observer_connector_instance_ref,
                        request.mailbox_ref,
                        request.mailbox_config_revision,
                        request.activation_not_before,
                        request.count_hint,
                        request.callback_timestamp,
                        request.payload_digest.removeprefix("sha256:"),
                        request.signal_digest.removeprefix("sha256:"),
                        (
                            None
                            if request.nonce_digest is None
                            else request.nonce_digest.removeprefix("sha256:")
                        ),
                        request.replay_key_digest.removeprefix("sha256:"),
                        request.idempotency_key,
                        accepted_at,
                    ),
                )
                cursor.execute(
                    "SELECT observer.enqueue_email_signal_work(%s, %s, %s)",
                    (scope.site_id, receipt.signal_receipt_ref, accepted_at),
                )
                enqueued = cursor.fetchone()
                if enqueued != (True,):
                    raise EmailSignalUnavailable("email signal work enqueue failed")
        except EmailSignalConflict:
            raise
        except PermissionError:
            raise
        except Exception as exc:
            raise EmailSignalUnavailable("email signal repository unavailable") from exc
        return receipt


def _current_config(cursor: Any, request: EmailSignalRequest) -> CurrentEmailConnectorConfig:
    cursor.execute(
        """
        SELECT site_id, connector_instance_id, provider_kind, mailbox_id,
               mailbox_config_revision, activation_not_before, inbound_enabled
          FROM observer.email_connector_config_projections
         WHERE site_id = %s AND mailbox_id = %s
         ORDER BY projection_revision DESC
         LIMIT 1
        """,
        (request.site_id, request.mailbox_ref),
    )
    row = cursor.fetchone()
    if row is None:
        raise PermissionError("current email connector config unavailable")
    return CurrentEmailConnectorConfig(
        site_id=str(row[0]),
        observer_connector_instance_ref=str(row[1]),
        provider_kind=str(row[2]),
        mailbox_ref=str(row[3]),
        mailbox_config_revision=int(row[4]),
        activation_not_before=cast(datetime, row[5]),
        inbound_enabled=bool(row[6]),
    )


def _validate_binding(
    scope: TenantScope,
    request: EmailSignalRequest,
    configs: Sequence[CurrentEmailConnectorConfig],
) -> None:
    if scope.site_id != request.site_id:
        raise PermissionError("signal site scope mismatch")
    related = [
        config
        for config in configs
        if config.site_id == request.site_id and config.mailbox_ref == request.mailbox_ref
    ]
    if not related:
        raise PermissionError("current email connector config unavailable")
    current = max(related, key=lambda item: item.mailbox_config_revision)
    if (
        current.provider_kind != "wecom_app_mail"
        or not current.inbound_enabled
        or current.observer_connector_instance_ref != request.observer_connector_instance_ref
        or current.mailbox_config_revision != request.mailbox_config_revision
        or current.activation_not_before.astimezone(UTC)
        != request.activation_not_before.astimezone(UTC)
    ):
        raise PermissionError("email signal config binding rejected")


def _receipt(request: EmailSignalRequest) -> EmailSignalReceipt:
    return EmailSignalReceipt(
        signal_receipt_ref="ESG-"
        + stable_ulid(
            "observer-email-signal",
            request.site_id,
            request.replay_key_digest,
            request.signal_digest,
        ),
        payload_digest=request.payload_digest,
    )


def _wire_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not 20 <= len(value) <= 35:
        raise ValueError(f"invalid {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        _require_aware(parsed, field)
    except ValueError as exc:
        raise ValueError(f"invalid {field}") from exc
    return parsed.astimezone(UTC)


def _wire_time(value: datetime) -> str:
    _require_aware(value, "datetime")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _require_ref(value: object, prefix: str) -> str | None:
    if not isinstance(value, str):
        return None
    match = _REF.fullmatch(value)
    return value if match is not None and match.group("prefix") == prefix else None


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _set_site(cursor: Any, scope: TenantScope) -> None:
    cursor.execute("SELECT set_config('app.site_id', %s, true)", (scope.site_id,))


__all__ = [
    "CurrentEmailConnectorConfig",
    "EmailSignalConflict",
    "EmailSignalLease",
    "EmailSignalReceipt",
    "EmailSignalRequest",
    "EmailSignalUnavailable",
    "InMemoryEmailSignalRepository",
    "PostgresEmailSignalRepository",
]
