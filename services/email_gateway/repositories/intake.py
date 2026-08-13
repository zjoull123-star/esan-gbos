from __future__ import annotations

from threading import RLock

from ..models import (
    ChannelMessage,
    EmailMessagePublication,
    IdempotencyConflict,
    InboxItem,
    IntakeResult,
    Mailbox,
    PublicationParticipant,
    PublicationReceipt,
    TenantScope,
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


class InMemoryIntakeRepository:
    def __init__(self) -> None:
        self._receipts: dict[tuple[str, str], PublicationReceipt] = {}
        self._messages: dict[tuple[str, str], ChannelMessage] = {}
        self._inbox: dict[tuple[str, str], InboxItem] = {}
        self._delivery_receipts: dict[tuple[str, str, str], PublicationReceipt] = {}
        self._lock = RLock()

    def accept(
        self,
        scope: TenantScope,
        publication: EmailMessagePublication,
        mailbox: Mailbox,
    ) -> IntakeResult:
        receipt_key = (scope.site_id, publication.publication_ref)
        with self._lock:
            replay = self._receipts.get(receipt_key)
            if replay is not None:
                if replay.payload_digest != publication.payload_digest:
                    raise IdempotencyConflict("publication payload drift")
                return IntakeResult(
                    replay,
                    self._messages[(scope.site_id, replay.message_ref)],
                    self._inbox[(scope.site_id, replay.inbox_item_ref)],
                )
            delivery_key = (
                scope.site_id,
                publication.mailbox_ref,
                publication.observer_delivery_ref,
            )
            delivery_replay = self._delivery_receipts.get(delivery_key)
            if delivery_replay is not None:
                if delivery_replay.payload_digest != publication.payload_digest:
                    raise IdempotencyConflict("mailbox delivery publication drift")
                return IntakeResult(
                    delivery_replay,
                    self._messages[(scope.site_id, delivery_replay.message_ref)],
                    self._inbox[(scope.site_id, delivery_replay.inbox_item_ref)],
                )
            message_identity = publication.message_id_digest or canonical_digest(
                {"publication_id": publication.publication_ref}
            )
            message_ref = stable_ref("MSG", scope.site_id, message_identity)
            message_key = (scope.site_id, message_ref)
            message = self._messages.get(message_key)
            if message is None:
                message = ChannelMessage(
                    message_ref=message_ref,
                    site_id=scope.site_id,
                    direction="inbound",
                    received_at=publication.received_at,
                    participants=publication.participants,
                    subject_projection=publication.subject_projection,
                    subject_digest=publication.subject_fact_digest,
                    message_id_digest=message_identity,
                    in_reply_to_digest=publication.in_reply_to_digest,
                    references_digests=publication.references_digests,
                    evidence_refs=publication.evidence_refs,
                    provider=mailbox.provider,
                    observer_delivery_refs=(publication.observer_delivery_ref,),
                    revision=1,
                )
            elif (
                message.subject_digest != publication.subject_fact_digest
                or message.participants != publication.participants
            ):
                raise IdempotencyConflict("message identity fact drift")
            inbox = InboxItem.new(
                site_id=scope.site_id,
                mailbox_ref=mailbox.mailbox_ref,
                message_ref=message_ref,
                team_ref=mailbox.default_team_ref,
                received_at=publication.received_at,
            )
            existing_inbox = self._inbox.get((scope.site_id, inbox.inbox_item_ref))
            if existing_inbox is not None:
                inbox = existing_inbox
            receipt = PublicationReceipt(
                receipt_ref=stable_ref("EGR", scope.site_id, publication.publication_ref),
                publication_ref=publication.publication_ref,
                site_id=scope.site_id,
                mailbox_ref=mailbox.mailbox_ref,
                observer_delivery_ref=publication.observer_delivery_ref,
                message_ref=message_ref,
                inbox_item_ref=inbox.inbox_item_ref,
                payload_digest=publication.payload_digest,
                received_at=publication.received_at,
            )
            self._messages[message_key] = message
            self._inbox[(scope.site_id, inbox.inbox_item_ref)] = inbox
            self._receipts[receipt_key] = receipt
            self._delivery_receipts[delivery_key] = receipt
            return IntakeResult(receipt, message, inbox)

    def counts(self, scope: TenantScope) -> tuple[int, int, int]:
        return (
            sum(site == scope.site_id for site, _ in self._receipts),
            sum(site == scope.site_id for site, _ in self._messages),
            sum(site == scope.site_id for site, _ in self._inbox),
        )


class PostgresIntakeRepository:
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
        return "PostgresIntakeRepository(connection=<redacted>, cipher=<redacted>)"

    def accept(
        self,
        scope: TenantScope,
        publication: EmailMessagePublication,
        mailbox: Mailbox,
    ) -> IntakeResult:
        require_scope(
            scope,
            site_id=publication.site_id,
            processing_purpose=publication.processing_purpose,
        )
        require_scope(
            scope,
            site_id=mailbox.site_id,
            processing_purpose=mailbox.business_purpose,
        )
        if publication.mailbox_ref != mailbox.mailbox_ref:
            raise IdempotencyConflict("publication mailbox binding drift")
        if publication.mailbox_config_revision != mailbox.config_revision:
            raise IdempotencyConflict("publication mailbox revision drift")
        if publication.observer_connector_instance_ref != mailbox.observer_connector_instance_ref:
            raise IdempotencyConflict("publication connector binding drift")
        if mailbox.status != "active" or not mailbox.inbound_enabled:
            raise IdempotencyConflict("publication mailbox is not active")

        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            cursor.execute(
                """
                SELECT config_revision, observer_connector_instance_ref, status,
                       inbound_enabled, provider, default_team_ref
                  FROM email_gateway.mailboxes
                 WHERE site_id = %s AND business_purpose = %s AND mailbox_ref = %s
                 FOR SHARE
                """,
                (scope.site_id, scope.processing_purpose, mailbox.mailbox_ref),
            )
            durable_mailbox = cursor.fetchone()
            if durable_mailbox is None:
                raise IdempotencyConflict("publication mailbox is unavailable")
            if (
                int(durable_mailbox[0]) != publication.mailbox_config_revision
                or str(durable_mailbox[1]) != publication.observer_connector_instance_ref
                or str(durable_mailbox[2]) != "active"
                or not bool(durable_mailbox[3])
                or str(durable_mailbox[4]) != mailbox.provider
                or str(durable_mailbox[5]) != mailbox.default_team_ref
            ):
                raise IdempotencyConflict("publication mailbox durable state drift")

            replay = self._find_receipt(
                cursor,
                scope,
                publication_ref=publication.publication_ref,
                mailbox_ref=publication.mailbox_ref,
                observer_delivery_ref=publication.observer_delivery_ref,
            )
            if replay is not None:
                if (
                    replay.mailbox_ref != publication.mailbox_ref
                    or replay.observer_delivery_ref != publication.observer_delivery_ref
                ):
                    raise IdempotencyConflict("publication receipt binding drift")
                if replay.payload_digest != publication.payload_digest:
                    raise IdempotencyConflict("publication payload drift")
                return self._result(cursor, scope, replay)

            message_identity = publication.message_id_digest or canonical_digest(
                {"publication_id": publication.publication_ref}
            )
            message_ref = stable_ref("MSG", scope.site_id, message_identity)
            inbox = InboxItem.new(
                site_id=scope.site_id,
                mailbox_ref=mailbox.mailbox_ref,
                message_ref=message_ref,
                team_ref=mailbox.default_team_ref,
                received_at=publication.received_at,
            )
            subject_ciphertext = (
                None
                if publication.subject_projection is None
                else encrypt_restricted_text(self._encrypt, publication.subject_projection)
            )
            cursor.execute(
                """
                INSERT INTO email_gateway.channel_messages (
                    site_id, message_ref, direction, received_at,
                    subject_projection_ciphertext, subject_digest, message_id_digest,
                    in_reply_to_digest, references_digests, evidence_refs, provider, revision
                ) VALUES (%s, %s, 'inbound', %s, %s, %s, %s, %s, %s, %s, %s, 1)
                ON CONFLICT DO NOTHING
                RETURNING message_ref
                """,
                (
                    scope.site_id,
                    message_ref,
                    publication.received_at,
                    subject_ciphertext,
                    publication.subject_fact_digest,
                    message_identity,
                    publication.in_reply_to_digest,
                    list(publication.references_digests),
                    list(publication.evidence_refs),
                    mailbox.provider,
                ),
            )
            inserted_message = cursor.fetchone() is not None
            if inserted_message:
                for ordinal, participant in enumerate(publication.participants, start=1):
                    cursor.execute(
                        """
                        INSERT INTO email_gateway.message_participants (
                            site_id, message_ref, role, identity_ref, ordinal
                        ) VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            scope.site_id,
                            message_ref,
                            participant.role,
                            participant.identity_ref,
                            ordinal,
                        ),
                    )
            else:
                existing = self._load_message(cursor, scope, message_ref)
                if existing is None or not _same_message_facts(existing, publication):
                    raise IdempotencyConflict("message identity fact drift")

            cursor.execute(
                """
                INSERT INTO email_gateway.inbox_items (
                    site_id, inbox_item_ref, mailbox_ref, message_ref, team_ref,
                    assignee_user_ref, priority, sla_due_at, state, conversation_ref,
                    business_links, revision, received_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, NULL, 0, NULL,
                          'identity_pending', NULL, '{}', 1, %s, %s)
                ON CONFLICT (site_id, mailbox_ref, message_ref) DO NOTHING
                """,
                (
                    scope.site_id,
                    inbox.inbox_item_ref,
                    inbox.mailbox_ref,
                    inbox.message_ref,
                    inbox.team_ref,
                    inbox.received_at,
                    inbox.updated_at,
                ),
            )
            receipt = PublicationReceipt(
                receipt_ref=stable_ref("EGR", scope.site_id, publication.publication_ref),
                publication_ref=publication.publication_ref,
                site_id=scope.site_id,
                mailbox_ref=mailbox.mailbox_ref,
                observer_delivery_ref=publication.observer_delivery_ref,
                message_ref=message_ref,
                inbox_item_ref=inbox.inbox_item_ref,
                payload_digest=publication.payload_digest,
                received_at=publication.received_at,
            )
            cursor.execute(
                """
                INSERT INTO email_gateway.publication_receipts (
                    site_id, receipt_ref, publication_ref, mailbox_ref,
                    observer_delivery_ref, message_ref, inbox_item_ref,
                    payload_digest, received_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    receipt.site_id,
                    receipt.receipt_ref,
                    receipt.publication_ref,
                    receipt.mailbox_ref,
                    receipt.observer_delivery_ref,
                    receipt.message_ref,
                    receipt.inbox_item_ref,
                    receipt.payload_digest,
                    receipt.received_at,
                ),
            )
            durable_receipt = self._find_receipt(
                cursor,
                scope,
                publication_ref=publication.publication_ref,
                mailbox_ref=publication.mailbox_ref,
                observer_delivery_ref=publication.observer_delivery_ref,
            )
            if durable_receipt is None:
                raise IdempotencyConflict("publication receipt conflict")
            if (
                durable_receipt.mailbox_ref != publication.mailbox_ref
                or durable_receipt.observer_delivery_ref != publication.observer_delivery_ref
            ):
                raise IdempotencyConflict("publication receipt binding drift")
            if durable_receipt.payload_digest != publication.payload_digest:
                raise IdempotencyConflict("publication payload drift")
            return self._result(cursor, scope, durable_receipt)

    def _find_receipt(
        self,
        cursor: object,
        scope: TenantScope,
        *,
        publication_ref: str,
        mailbox_ref: str,
        observer_delivery_ref: str,
    ) -> PublicationReceipt | None:
        cursor.execute(  # type: ignore[attr-defined]
            """
            SELECT receipt_ref, publication_ref, site_id, mailbox_ref,
                   observer_delivery_ref, message_ref, inbox_item_ref,
                   payload_digest, received_at
              FROM email_gateway.publication_receipts
             WHERE site_id = %s
               AND (publication_ref = %s OR
                    (mailbox_ref = %s AND observer_delivery_ref = %s))
             ORDER BY CASE WHEN publication_ref = %s THEN 0 ELSE 1 END
             LIMIT 1
            """,
            (
                scope.site_id,
                publication_ref,
                mailbox_ref,
                observer_delivery_ref,
                publication_ref,
            ),
        )
        row = cursor.fetchone()  # type: ignore[attr-defined]
        if row is None:
            return None
        return PublicationReceipt(
            receipt_ref=str(row[0]),
            publication_ref=str(row[1]),
            site_id=str(row[2]),
            mailbox_ref=str(row[3]),
            observer_delivery_ref=str(row[4]),
            message_ref=str(row[5]),
            inbox_item_ref=str(row[6]),
            payload_digest=str(row[7]),
            received_at=row[8],
        )

    def _result(
        self, cursor: object, scope: TenantScope, receipt: PublicationReceipt
    ) -> IntakeResult:
        message = self._load_message(cursor, scope, receipt.message_ref)
        inbox = self._load_inbox(cursor, scope, receipt.inbox_item_ref)
        if message is None or inbox is None:
            raise IdempotencyConflict("publication durable result unavailable")
        return IntakeResult(receipt=receipt, message=message, inbox_item=inbox)

    def _load_message(
        self, cursor: object, scope: TenantScope, message_ref: str
    ) -> ChannelMessage | None:
        cursor.execute(  # type: ignore[attr-defined]
            """
            SELECT message_ref, site_id, direction, received_at,
                   subject_projection_ciphertext, subject_digest, message_id_digest,
                   in_reply_to_digest, references_digests, evidence_refs, provider, revision
              FROM email_gateway.channel_messages
             WHERE site_id = %s AND message_ref = %s
            """,
            (scope.site_id, message_ref),
        )
        row = cursor.fetchone()  # type: ignore[attr-defined]
        if row is None:
            return None
        cursor.execute(  # type: ignore[attr-defined]
            """
            SELECT role, identity_ref
              FROM email_gateway.message_participants
             WHERE site_id = %s AND message_ref = %s
             ORDER BY ordinal
            """,
            (scope.site_id, message_ref),
        )
        participants = tuple(
            PublicationParticipant(role=str(item[0]), identity_ref=str(item[1]))
            for item in cursor.fetchall()  # type: ignore[attr-defined]
        )
        cursor.execute(  # type: ignore[attr-defined]
            """
            SELECT observer_delivery_ref
              FROM email_gateway.publication_receipts
             WHERE site_id = %s AND message_ref = %s
             ORDER BY observer_delivery_ref
            """,
            (scope.site_id, message_ref),
        )
        delivery_refs = tuple(str(item[0]) for item in cursor.fetchall())  # type: ignore[attr-defined]
        return ChannelMessage(
            message_ref=str(row[0]),
            site_id=str(row[1]),
            direction=str(row[2]),
            received_at=row[3],
            participants=participants,
            subject_projection=(
                None if row[4] is None else decrypt_restricted_text(self._decrypt, row[4])
            ),
            subject_digest=str(row[5]),
            message_id_digest=str(row[6]),
            in_reply_to_digest=None if row[7] is None else str(row[7]),
            references_digests=tuple(str(item) for item in row[8]),
            evidence_refs=tuple(str(item) for item in row[9]),
            provider=str(row[10]),
            observer_delivery_refs=delivery_refs,
            revision=int(row[11]),
        )

    @staticmethod
    def _load_inbox(cursor: object, scope: TenantScope, inbox_item_ref: str) -> InboxItem | None:
        cursor.execute(  # type: ignore[attr-defined]
            """
            SELECT inbox_item_ref, site_id, mailbox_ref, message_ref, team_ref,
                   assignee_user_ref, priority, sla_due_at, state, conversation_ref,
                   business_links, revision, received_at, updated_at
              FROM email_gateway.inbox_items
             WHERE site_id = %s AND inbox_item_ref = %s
            """,
            (scope.site_id, inbox_item_ref),
        )
        row = cursor.fetchone()  # type: ignore[attr-defined]
        if row is None:
            return None
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


def _same_message_facts(existing: ChannelMessage, publication: EmailMessagePublication) -> bool:
    return (
        existing.participants == publication.participants
        and existing.subject_digest == publication.subject_fact_digest
        and existing.message_id_digest
        == (
            publication.message_id_digest
            or canonical_digest({"publication_id": publication.publication_ref})
        )
        and existing.in_reply_to_digest == publication.in_reply_to_digest
        and existing.references_digests == publication.references_digests
    )


__all__ = ["InMemoryIntakeRepository", "PostgresIntakeRepository"]
