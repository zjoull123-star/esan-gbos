from __future__ import annotations

import hashlib
import hmac
from dataclasses import replace
from datetime import datetime, timedelta
from threading import RLock
from typing import Any, Literal

from ..models import (
    IdempotencyConflict,
    Mailbox,
    MailboxChangeReceipt,
    MailboxConnectorProjection,
    RevisionConflict,
    TenantScope,
    ValidationError,
    canonical_digest,
    require_scope,
    stable_ref,
)
from ..postgres import (
    Connection,
    RestrictedTextDecryptor,
    RestrictedTextEncryptor,
    decrypt_restricted_text,
    encrypt_restricted_text,
    redacted_database_errors,
    site_transaction,
)


class InMemoryMailboxRepository:
    def __init__(self) -> None:
        self._mailboxes: dict[tuple[str, str], Mailbox] = {}
        self._idempotency: dict[tuple[str, str], MailboxChangeReceipt] = {}
        self._lock = RLock()

    def get(self, scope: TenantScope, mailbox_ref: str) -> Mailbox | None:
        return self._mailboxes.get((scope.site_id, mailbox_ref))

    def list(self, scope: TenantScope) -> tuple[Mailbox, ...]:
        return tuple(
            sorted(
                (value for (site, _), value in self._mailboxes.items() if site == scope.site_id),
                key=lambda value: value.mailbox_ref,
            )
        )

    def upsert(
        self,
        scope: TenantScope,
        mailbox: Mailbox,
        *,
        expected_revision: int,
        actor_ref: str,
        request_id: str,
        idempotency_key: str,
    ) -> MailboxChangeReceipt:
        require_scope(scope, site_id=mailbox.site_id, processing_purpose=mailbox.business_purpose)
        payload_digest = canonical_digest(mailbox.to_wire())
        idempotency = (scope.site_id, idempotency_key)
        key = (scope.site_id, mailbox.mailbox_ref)
        with self._lock:
            replay = self._idempotency.get(idempotency)
            if replay is not None:
                if replay.payload_digest != payload_digest:
                    raise IdempotencyConflict("mailbox idempotency payload drift")
                return replay
            current = self._mailboxes.get(key)
            current_revision = 0 if current is None else current.config_revision
            if expected_revision != current_revision:
                raise RevisionConflict("mailbox revision conflict")
            durable = replace(mailbox, config_revision=current_revision + 1)
            receipt = MailboxChangeReceipt(
                mailbox=durable,
                config_publication_ref=stable_ref(
                    "MCP", scope.site_id, mailbox.mailbox_ref, str(durable.config_revision)
                ),
                request_id=request_id,
                idempotency_key=idempotency_key,
                payload_digest=payload_digest,
            )
            self._mailboxes[key] = durable
            self._idempotency[idempotency] = receipt
            return receipt


class PostgresMailboxRepository:
    """SQL statements stay in the mailbox-focused adapter."""

    GET_SQL = """
        SELECT mailbox_ref, site_id, address_display_ciphertext, provider,
               provider_account_ref, observer_connector_instance_ref, entry_role,
               business_purpose, default_team_ref, account_owner_user_ref, priority,
               inbound_enabled, outbound_enabled, credential_ref, status,
               config_revision, observer_config_projection_receipt
          FROM email_gateway.mailboxes
         WHERE site_id = %s AND business_purpose = %s AND mailbox_ref = %s
    """

    LIST_SQL = """
        SELECT mailbox_ref, site_id, address_display_ciphertext, provider,
               provider_account_ref, observer_connector_instance_ref, entry_role,
               business_purpose, default_team_ref, account_owner_user_ref, priority,
               inbound_enabled, outbound_enabled, credential_ref, status,
               config_revision, observer_config_projection_receipt
          FROM email_gateway.mailboxes
         WHERE site_id = %s AND business_purpose = %s
         ORDER BY mailbox_ref
    """

    def __init__(
        self,
        connection: Connection,
        *,
        encrypt_restricted_text: RestrictedTextEncryptor,
        decrypt_restricted_text: RestrictedTextDecryptor,
    ) -> None:
        self.connection = connection
        self._encrypt = encrypt_restricted_text
        self._decrypt = decrypt_restricted_text

    def __repr__(self) -> str:
        return "PostgresMailboxRepository(connection=<redacted>, cipher=<redacted>)"

    def get(self, scope: TenantScope, mailbox_ref: str) -> Mailbox | None:
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            cursor.execute(
                self.GET_SQL,
                (scope.site_id, scope.processing_purpose, mailbox_ref),
            )
            row = cursor.fetchone()
            return None if row is None else self._mailbox_from_row(row)

    def list(self, scope: TenantScope) -> tuple[Mailbox, ...]:
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            cursor.execute(self.LIST_SQL, (scope.site_id, scope.processing_purpose))
            return tuple(self._mailbox_from_row(row) for row in cursor.fetchall())

    def upsert(
        self,
        scope: TenantScope,
        mailbox: Mailbox,
        *,
        expected_revision: int,
        actor_ref: str,
        request_id: str,
        idempotency_key: str,
    ) -> MailboxChangeReceipt:
        require_scope(
            scope,
            site_id=mailbox.site_id,
            processing_purpose=mailbox.business_purpose,
        )
        _positive_or_zero(expected_revision, "expected revision")
        _safe_identifier(actor_ref, "actor ref")
        _safe_identifier(request_id, "request id")
        _safe_identifier(idempotency_key, "idempotency key")
        payload_digest = canonical_digest(mailbox.to_wire())
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            cursor.execute(
                """
                SELECT pg_advisory_xact_lock(
                    hashtextextended(%s || chr(31) || %s || chr(31) || %s, 0)
                )
                """,
                (scope.site_id, scope.processing_purpose, mailbox.mailbox_ref),
            )
            cursor.execute(
                """
                SELECT config_publication_ref, mailbox_ref, mailbox_config_revision,
                       request_id, idempotency_key, payload_digest
                  FROM email_gateway.mailbox_config_outbox
                 WHERE site_id = %s AND processing_purpose = %s
                   AND idempotency_key = %s
                """,
                (scope.site_id, scope.processing_purpose, idempotency_key),
            )
            replay = cursor.fetchone()
            if replay is not None:
                if str(replay[5]) != payload_digest:
                    raise IdempotencyConflict("mailbox idempotency payload drift")
                cursor.execute(
                    self.GET_SQL,
                    (scope.site_id, scope.processing_purpose, str(replay[1])),
                )
                replay_mailbox_row = cursor.fetchone()
                if replay_mailbox_row is None:
                    raise RevisionConflict("mailbox replay state unavailable")
                return MailboxChangeReceipt(
                    mailbox=self._mailbox_from_row(replay_mailbox_row),
                    config_publication_ref=str(replay[0]),
                    request_id=str(replay[3]),
                    idempotency_key=str(replay[4]),
                    payload_digest=str(replay[5]),
                )

            cursor.execute(
                self.GET_SQL + " FOR UPDATE",
                (scope.site_id, scope.processing_purpose, mailbox.mailbox_ref),
            )
            current_row = cursor.fetchone()
            current_revision = 0 if current_row is None else int(current_row[15])
            if expected_revision != current_revision:
                raise RevisionConflict("mailbox revision conflict")
            next_revision = current_revision + 1
            protected_address = encrypt_restricted_text(self._encrypt, mailbox.address_display)
            if current_row is None:
                cursor.execute(
                    """
                    INSERT INTO email_gateway.mailboxes (
                        site_id, mailbox_ref, address_display_ciphertext, provider,
                        provider_account_ref, observer_connector_instance_ref, entry_role,
                        business_purpose, default_team_ref, account_owner_user_ref, priority,
                        inbound_enabled, outbound_enabled, credential_ref, status,
                        config_revision, observer_config_projection_receipt,
                        created_by, updated_by
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        scope.site_id,
                        mailbox.mailbox_ref,
                        protected_address,
                        mailbox.provider,
                        mailbox.provider_account_ref,
                        mailbox.observer_connector_instance_ref,
                        mailbox.entry_role,
                        mailbox.business_purpose,
                        mailbox.default_team_ref,
                        mailbox.account_owner_user_ref,
                        mailbox.priority,
                        mailbox.inbound_enabled,
                        mailbox.outbound_enabled,
                        mailbox.credential_ref,
                        mailbox.status,
                        next_revision,
                        mailbox.observer_config_projection_receipt,
                        actor_ref,
                        actor_ref,
                    ),
                )
            else:
                cursor.execute(
                    """
                    UPDATE email_gateway.mailboxes
                       SET address_display_ciphertext = %s,
                           provider = %s,
                           provider_account_ref = %s,
                           observer_connector_instance_ref = %s,
                           entry_role = %s,
                           default_team_ref = %s,
                           account_owner_user_ref = %s,
                           priority = %s,
                           inbound_enabled = %s,
                           outbound_enabled = %s,
                           credential_ref = %s,
                           status = %s,
                           config_revision = %s,
                           observer_config_projection_receipt = %s,
                           updated_by = %s,
                           updated_at = now()
                     WHERE site_id = %s AND business_purpose = %s
                       AND mailbox_ref = %s AND config_revision = %s
                    """,
                    (
                        protected_address,
                        mailbox.provider,
                        mailbox.provider_account_ref,
                        mailbox.observer_connector_instance_ref,
                        mailbox.entry_role,
                        mailbox.default_team_ref,
                        mailbox.account_owner_user_ref,
                        mailbox.priority,
                        mailbox.inbound_enabled,
                        mailbox.outbound_enabled,
                        mailbox.credential_ref,
                        mailbox.status,
                        next_revision,
                        mailbox.observer_config_projection_receipt,
                        actor_ref,
                        scope.site_id,
                        scope.processing_purpose,
                        mailbox.mailbox_ref,
                        current_revision,
                    ),
                )
            durable = replace(mailbox, config_revision=next_revision)
            config_publication_ref = stable_ref(
                "MCP", scope.site_id, mailbox.mailbox_ref, str(next_revision)
            )
            cursor.execute(
                """
                INSERT INTO email_gateway.mailbox_config_outbox (
                    site_id, config_publication_ref, mailbox_ref,
                    mailbox_config_revision, processing_purpose, request_id,
                    idempotency_key, payload_digest
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    scope.site_id,
                    config_publication_ref,
                    mailbox.mailbox_ref,
                    next_revision,
                    scope.processing_purpose,
                    request_id,
                    idempotency_key,
                    payload_digest,
                ),
            )
            return MailboxChangeReceipt(
                mailbox=durable,
                config_publication_ref=config_publication_ref,
                request_id=request_id,
                idempotency_key=idempotency_key,
                payload_digest=payload_digest,
            )

    def claim(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> MailboxConfigOutboxClaim | None:
        _safe_identifier(worker_id, "worker id")
        _aware(now, "now")
        _lease_duration(lease_duration)
        lease_expires_at = now + lease_duration
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            cursor.execute(
                """
                UPDATE email_gateway.mailbox_config_outbox AS outbox
                   SET status = 'dead_letter',
                       safe_error_code = 'superseded_revision',
                       lease_owner = NULL,
                       lease_expires_at = NULL,
                       updated_at = %s
                  FROM email_gateway.mailboxes AS mailbox
                 WHERE mailbox.site_id = outbox.site_id
                   AND mailbox.mailbox_ref = outbox.mailbox_ref
                   AND outbox.site_id = %s
                   AND outbox.processing_purpose = %s
                   AND mailbox.business_purpose = %s
                   AND outbox.status IN ('queued', 'retry')
                   AND outbox.mailbox_config_revision < mailbox.config_revision
                """,
                (
                    now,
                    scope.site_id,
                    scope.processing_purpose,
                    scope.processing_purpose,
                ),
            )
            cursor.execute(
                """
                WITH candidate AS (
                    SELECT outbox.site_id, outbox.config_publication_ref
                      FROM email_gateway.mailbox_config_outbox AS outbox
                      JOIN email_gateway.mailboxes AS mailbox
                        ON mailbox.site_id = outbox.site_id
                       AND mailbox.mailbox_ref = outbox.mailbox_ref
                       AND mailbox.config_revision = outbox.mailbox_config_revision
                     WHERE outbox.site_id = %s
                       AND outbox.processing_purpose = %s
                       AND mailbox.business_purpose = %s
                       AND outbox.attempt < 5
                       AND (
                           (outbox.status IN ('queued', 'retry')
                            AND outbox.next_attempt_at <= %s)
                           OR (outbox.status = 'leased' AND outbox.lease_expires_at <= %s)
                       )
                     ORDER BY outbox.next_attempt_at, outbox.created_at,
                              outbox.config_publication_ref
                     FOR UPDATE OF outbox SKIP LOCKED
                     LIMIT 1
                )
                UPDATE email_gateway.mailbox_config_outbox AS outbox
                   SET status = 'leased',
                       attempt = outbox.attempt + 1,
                       lease_owner = %s,
                       lease_expires_at = %s,
                       lease_generation = outbox.lease_generation + 1,
                       safe_error_code = NULL,
                       updated_at = %s
                  FROM candidate
                 WHERE outbox.site_id = candidate.site_id
                   AND outbox.config_publication_ref = candidate.config_publication_ref
                RETURNING outbox.config_publication_ref, outbox.mailbox_ref,
                          outbox.mailbox_config_revision, outbox.processing_purpose,
                          outbox.request_id, outbox.idempotency_key, outbox.payload_digest,
                          outbox.status, outbox.attempt, outbox.lease_owner,
                          outbox.lease_expires_at, outbox.lease_generation,
                          outbox.activation_not_before
                """,
                (
                    scope.site_id,
                    scope.processing_purpose,
                    scope.processing_purpose,
                    now,
                    now,
                    worker_id,
                    lease_expires_at,
                    now,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            cursor.execute(
                """
                SELECT mailbox_ref, config_revision,
                       observer_connector_instance_ref, provider, entry_role,
                       business_purpose, default_team_ref, credential_ref,
                       inbound_enabled, outbound_enabled, status
                  FROM email_gateway.mailboxes
                 WHERE site_id = %s AND business_purpose = %s AND mailbox_ref = %s
                """,
                (scope.site_id, scope.processing_purpose, str(row[1])),
            )
            mailbox_row = cursor.fetchone()
            if mailbox_row is None or int(mailbox_row[1]) != int(row[2]):
                raise RevisionConflict("config publication mailbox revision drift")
            attempt = int(row[8])
            generation = int(row[11])
            return MailboxConfigOutboxClaim(
                site_id=scope.site_id,
                config_publication_ref=str(row[0]),
                mailbox_ref=str(mailbox_row[0]),
                mailbox_config_revision=int(mailbox_row[1]),
                observer_connector_instance_ref=str(mailbox_row[2]),
                provider=str(mailbox_row[3]),
                entry_role=str(mailbox_row[4]),
                business_purpose=str(mailbox_row[5]),
                default_team_ref=str(mailbox_row[6]),
                credential_ref=str(mailbox_row[7]),
                inbound_enabled=bool(mailbox_row[8]),
                outbound_enabled=bool(mailbox_row[9]),
                mailbox_status=str(mailbox_row[10]),
                activation_not_before=row[12],
                processing_purpose=str(row[3]),
                request_id=str(row[4]),
                idempotency_key=str(row[5]),
                payload_digest=str(row[6]),
                status="leased",
                attempt=attempt,
                lease_owner=str(row[9]),
                lease_expires_at=row[10],
                lease_generation=generation,
                fence_token=_fence_token(
                    site_id=scope.site_id,
                    publication_ref=str(row[0]),
                    worker_id=worker_id,
                    attempt=attempt,
                    generation=generation,
                ),
            )

    def heartbeat(
        self,
        scope: TenantScope,
        config_publication_ref: str,
        *,
        worker_id: str,
        expected_attempt: int,
        fence_token: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> None:
        generation = _validate_transition(
            scope,
            config_publication_ref,
            worker_id=worker_id,
            expected_attempt=expected_attempt,
            fence_token=fence_token,
            now=now,
        )
        _lease_duration(lease_duration)
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            cursor.execute(
                """
                UPDATE email_gateway.mailbox_config_outbox
                   SET lease_expires_at = %s, updated_at = %s
                 WHERE site_id = %s AND processing_purpose = %s
                   AND config_publication_ref = %s AND status = 'leased'
                   AND lease_owner = %s AND attempt = %s AND lease_generation = %s
                   AND lease_expires_at > %s
                RETURNING config_publication_ref
                """,
                (
                    now + lease_duration,
                    now,
                    scope.site_id,
                    scope.processing_purpose,
                    config_publication_ref,
                    worker_id,
                    expected_attempt,
                    generation,
                    now,
                ),
            )
            if cursor.fetchone() is None:
                raise ConfigLeaseConflict("config publication lease transition rejected")

    def mark_delivered(
        self,
        scope: TenantScope,
        config_publication_ref: str,
        *,
        worker_id: str,
        expected_attempt: int,
        fence_token: str,
        receipt_ref: str,
        now: datetime,
    ) -> None:
        _safe_identifier(receipt_ref, "receipt ref")
        generation = _validate_transition(
            scope,
            config_publication_ref,
            worker_id=worker_id,
            expected_attempt=expected_attempt,
            fence_token=fence_token,
            now=now,
        )
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            cursor.execute(
                """
                SELECT status, receipt_ref
                  FROM email_gateway.mailbox_config_outbox
                 WHERE site_id = %s AND processing_purpose = %s
                   AND config_publication_ref = %s
                """,
                (scope.site_id, scope.processing_purpose, config_publication_ref),
            )
            current = cursor.fetchone()
            if current is not None and str(current[0]) == "delivered":
                if str(current[1]) != receipt_ref:
                    raise ConfigLeaseConflict("config publication receipt drift")
                return
            cursor.execute(
                """
                UPDATE email_gateway.mailbox_config_outbox
                   SET status = 'delivered', receipt_ref = %s,
                       lease_owner = NULL, lease_expires_at = NULL,
                       safe_error_code = NULL, updated_at = %s
                 WHERE site_id = %s AND processing_purpose = %s
                   AND config_publication_ref = %s AND status = 'leased'
                   AND lease_owner = %s AND attempt = %s AND lease_generation = %s
                   AND lease_expires_at > %s
                RETURNING config_publication_ref
                """,
                (
                    receipt_ref,
                    now,
                    scope.site_id,
                    scope.processing_purpose,
                    config_publication_ref,
                    worker_id,
                    expected_attempt,
                    generation,
                    now,
                ),
            )
            if cursor.fetchone() is None:
                raise ConfigLeaseConflict("config publication lease transition rejected")

    def mark_failed(
        self,
        scope: TenantScope,
        config_publication_ref: str,
        *,
        worker_id: str,
        expected_attempt: int,
        fence_token: str,
        now: datetime,
        retry_at: datetime,
        error_code: str,
    ) -> Literal["retry", "dead_letter"]:
        generation = _validate_transition(
            scope,
            config_publication_ref,
            worker_id=worker_id,
            expected_attempt=expected_attempt,
            fence_token=fence_token,
            now=now,
        )
        _aware(retry_at, "retry at")
        if retry_at <= now:
            raise ValidationError("invalid retry time")
        _safe_code(error_code)
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            cursor.execute(
                """
                UPDATE email_gateway.mailbox_config_outbox
                   SET status = CASE WHEN attempt >= 5 THEN 'dead_letter' ELSE 'retry' END,
                       next_attempt_at = %s, safe_error_code = %s,
                       lease_owner = NULL, lease_expires_at = NULL, updated_at = %s
                 WHERE site_id = %s AND processing_purpose = %s
                   AND config_publication_ref = %s AND status = 'leased'
                   AND lease_owner = %s AND attempt = %s AND lease_generation = %s
                   AND lease_expires_at > %s
                RETURNING status
                """,
                (
                    retry_at,
                    error_code,
                    now,
                    scope.site_id,
                    scope.processing_purpose,
                    config_publication_ref,
                    worker_id,
                    expected_attempt,
                    generation,
                    now,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise ConfigLeaseConflict("config publication lease transition rejected")
            status = str(row[0])
            if status not in {"retry", "dead_letter"}:
                raise ValidationError("invalid config publication safe state")
            return status  # type: ignore[return-value]

    def _mailbox_from_row(self, row: tuple[Any, ...]) -> Mailbox:
        if len(row) != 17:
            raise ValidationError("invalid mailbox database row")
        return Mailbox(
            mailbox_ref=str(row[0]),
            site_id=str(row[1]),
            address_display=decrypt_restricted_text(self._decrypt, row[2]),
            provider=str(row[3]),
            provider_account_ref=str(row[4]),
            observer_connector_instance_ref=str(row[5]),
            entry_role=str(row[6]),
            business_purpose=str(row[7]),
            default_team_ref=str(row[8]),
            account_owner_user_ref=str(row[9]),
            priority=int(row[10]),
            inbound_enabled=bool(row[11]),
            outbound_enabled=bool(row[12]),
            credential_ref=str(row[13]),
            status=str(row[14]),
            config_revision=int(row[15]),
            observer_config_projection_receipt=(None if row[16] is None else str(row[16])),
        )


class ConfigLeaseConflict(ValidationError):
    """A stale worker no longer owns the mailbox configuration lease."""


class MailboxConfigOutboxClaim:
    __slots__ = (
        "site_id",
        "config_publication_ref",
        "mailbox_ref",
        "mailbox_config_revision",
        "observer_connector_instance_ref",
        "provider",
        "entry_role",
        "business_purpose",
        "default_team_ref",
        "credential_ref",
        "inbound_enabled",
        "outbound_enabled",
        "mailbox_status",
        "activation_not_before",
        "processing_purpose",
        "request_id",
        "idempotency_key",
        "payload_digest",
        "status",
        "attempt",
        "lease_owner",
        "lease_expires_at",
        "lease_generation",
        "fence_token",
    )

    def __init__(
        self,
        *,
        site_id: str,
        config_publication_ref: str,
        mailbox_ref: str,
        mailbox_config_revision: int,
        observer_connector_instance_ref: str,
        provider: str,
        entry_role: str,
        business_purpose: str,
        default_team_ref: str,
        credential_ref: str,
        inbound_enabled: bool,
        outbound_enabled: bool,
        mailbox_status: str,
        activation_not_before: datetime,
        processing_purpose: str,
        request_id: str,
        idempotency_key: str,
        payload_digest: str,
        status: str,
        attempt: int,
        lease_owner: str,
        lease_expires_at: datetime,
        lease_generation: int,
        fence_token: str,
    ) -> None:
        self.site_id = site_id
        self.config_publication_ref = config_publication_ref
        self.mailbox_ref = mailbox_ref
        self.mailbox_config_revision = mailbox_config_revision
        self.observer_connector_instance_ref = observer_connector_instance_ref
        self.provider = provider
        self.entry_role = entry_role
        self.business_purpose = business_purpose
        self.default_team_ref = default_team_ref
        self.credential_ref = credential_ref
        self.inbound_enabled = inbound_enabled
        self.outbound_enabled = outbound_enabled
        self.mailbox_status = mailbox_status
        _aware(activation_not_before, "activation not before")
        self.activation_not_before = activation_not_before
        self.processing_purpose = processing_purpose
        self.request_id = request_id
        self.idempotency_key = idempotency_key
        self.payload_digest = payload_digest
        self.status = status
        self.attempt = attempt
        self.lease_owner = lease_owner
        self.lease_expires_at = lease_expires_at
        self.lease_generation = lease_generation
        self.fence_token = fence_token

    def __repr__(self) -> str:
        return (
            "MailboxConfigOutboxClaim("
            f"config_publication_ref={self.config_publication_ref!r}, "
            f"mailbox_ref={self.mailbox_ref!r}, status={self.status!r}, "
            f"attempt={self.attempt}, lease_generation={self.lease_generation}, "
            "credential_ref=<redacted>, fence_token=<redacted>)"
        )

    def to_connector_projection_wire(self) -> dict[str, object]:
        return MailboxConnectorProjection(
            site_id=self.site_id,
            observer_connector_instance_ref=self.observer_connector_instance_ref,
            provider_kind=self.provider,
            entry_role=self.entry_role,
            business_purpose=self.business_purpose,
            team_ref=self.default_team_ref,
            credential_ref=self.credential_ref,
            inbound_enabled=self.inbound_enabled,
            mailbox_ref=self.mailbox_ref,
            mailbox_config_revision=self.mailbox_config_revision,
            activation_not_before=self.activation_not_before,
            projection_revision=self.mailbox_config_revision,
        ).to_wire()


class PostgresMailboxConfigOutboxRepository(PostgresMailboxRepository):
    """Worker-role configuration relay repository with no Restricted reveal capability."""

    def __init__(self, connection: Connection) -> None:
        def unavailable_encryptor(_: str) -> bytes:
            raise ValidationError("restricted text access unavailable")

        def unavailable_decryptor(_: bytes) -> str:
            raise ValidationError("restricted text access unavailable")

        super().__init__(
            connection,
            encrypt_restricted_text=unavailable_encryptor,
            decrypt_restricted_text=unavailable_decryptor,
        )

    def __repr__(self) -> str:
        return "PostgresMailboxConfigOutboxRepository(connection=<redacted>)"


def _safe_identifier(value: object, name: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValidationError(f"invalid {name}")


def _safe_code(value: object) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 80
        or not value[0].islower()
        or any(not (char.islower() or char.isdigit() or char == "_") for char in value)
    ):
        raise ValidationError("invalid safe error code")


def _positive_or_zero(value: object, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValidationError(f"invalid {name}")


def _aware(value: object, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"invalid {name}")


def _lease_duration(value: object) -> None:
    if not isinstance(value, timedelta) or not timedelta(0) < value <= timedelta(hours=1):
        raise ValidationError("invalid config publication lease duration")


def _fence_token(
    *,
    site_id: str,
    publication_ref: str,
    worker_id: str,
    attempt: int,
    generation: int,
) -> str:
    digest = hashlib.sha256(
        f"mailbox-config-fence-v1\x1f{site_id}\x1f{publication_ref}\x1f{worker_id}"
        f"\x1f{attempt}\x1f{generation}".encode()
    ).hexdigest()
    return f"v1:{attempt}:{generation}:{digest}"


def _validate_transition(
    scope: TenantScope,
    publication_ref: str,
    *,
    worker_id: str,
    expected_attempt: int,
    fence_token: str,
    now: datetime,
) -> int:
    _safe_identifier(publication_ref, "config publication ref")
    _safe_identifier(worker_id, "worker id")
    _positive_or_zero(expected_attempt, "expected attempt")
    if expected_attempt < 1:
        raise ConfigLeaseConflict("config publication lease transition rejected")
    _aware(now, "now")
    try:
        prefix, attempt_text, generation_text, supplied_digest = fence_token.split(":")
        attempt = int(attempt_text)
        generation = int(generation_text)
    except AttributeError, TypeError, ValueError:
        raise ConfigLeaseConflict("config publication lease transition rejected") from None
    expected = _fence_token(
        site_id=scope.site_id,
        publication_ref=publication_ref,
        worker_id=worker_id,
        attempt=expected_attempt,
        generation=generation,
    )
    if (
        prefix != "v1"
        or attempt != expected_attempt
        or generation < 1
        or len(supplied_digest) != 64
        or not hmac.compare_digest(expected, fence_token)
    ):
        raise ConfigLeaseConflict("config publication lease transition rejected")
    return generation


__all__ = [
    "ConfigLeaseConflict",
    "InMemoryMailboxRepository",
    "MailboxConfigOutboxClaim",
    "PostgresMailboxConfigOutboxRepository",
    "PostgresMailboxRepository",
]
