from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from ..models import GatewayActorScope, TenantScope, ValidationError
from ..phase1_read import (
    Page,
    Phase1InboxItem,
    Phase1Mailbox,
    decode_cursor,
    encode_cursor,
)
from ..postgres import (
    Connection,
    RestrictedTextDecryptor,
    decrypt_restricted_text,
    redacted_database_errors,
    site_transaction,
)

_WILDCARD_ROLES = frozenset({"CEO", "GBOS Admin"})
_PHASE1_STATES = frozenset({"identity_pending", "unassigned"})


def _page_size(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 50:
        raise ValidationError("invalid page size")


def _wildcard(actor: GatewayActorScope) -> bool:
    return actor.team_refs == ("*",) and bool(_WILDCARD_ROLES.intersection(actor.roles))


def _authorized(item: Phase1InboxItem, actor: GatewayActorScope) -> bool:
    return item.site_id == actor.site_id and (_wildcard(actor) or item.team_ref in actor.team_refs)


class InMemoryPhase1ReadRepository:
    def __init__(
        self,
        *,
        site_id: str = "alpha.example",
        mailboxes: tuple[Phase1Mailbox, ...] = (),
        inbox_items: tuple[Phase1InboxItem, ...] = (),
    ) -> None:
        self.site_id = site_id
        self.mailboxes = tuple(
            item
            if item.site_id == site_id
            else Phase1Mailbox(
                mailbox_ref=item.mailbox_ref,
                observer_connector_instance_ref=item.observer_connector_instance_ref,
                display_label=item.display_label,
                provider_kind=item.provider_kind,
                business_mode=item.business_mode,
                business_purpose=item.business_purpose,
                default_team_ref=item.default_team_ref,
                account_owner_user_ref=item.account_owner_user_ref,
                inbound_enabled=item.inbound_enabled,
                outbound_enabled=item.outbound_enabled,
                status=item.status,
                config_revision=item.config_revision,
                site_id=site_id,
            )
            for item in mailboxes
        )
        self.inbox_items = tuple(
            item
            if item.site_id == site_id
            else Phase1InboxItem(
                inbox_item_ref=item.inbox_item_ref,
                mailbox_label=item.mailbox_label,
                mailbox_role=item.mailbox_role,
                received_at=item.received_at,
                state=item.state,
                safe_summary=item.safe_summary,
                team_ref=item.team_ref,
                assignee_user_ref=item.assignee_user_ref,
                identity_state=item.identity_state,
                revision=item.revision,
                site_id=site_id,
            )
            for item in inbox_items
        )

    def list_mailboxes(
        self, site_id: str, *, page_size: int, cursor: str | None
    ) -> Page[Phase1Mailbox]:
        _page_size(page_size)
        rows = sorted(
            (item for item in self.mailboxes if item.site_id == site_id),
            key=lambda item: item.mailbox_ref,
        )
        start = 0
        if cursor is not None:
            (reference,) = decode_cursor(cursor, "mailbox", 1)
            anchors = [index for index, item in enumerate(rows) if item.mailbox_ref == reference]
            if not anchors:
                raise ValidationError("invalid cursor")
            start = anchors[0] + 1
        page = rows[start : start + page_size]
        more = start + page_size < len(rows)
        next_cursor = encode_cursor("mailbox", page[-1].mailbox_ref) if page and more else None
        return Page(tuple(page), next_cursor)

    def get_mailbox(self, site_id: str, mailbox_ref: str) -> Phase1Mailbox | None:
        return next(
            (
                item
                for item in self.mailboxes
                if item.site_id == site_id and item.mailbox_ref == mailbox_ref
            ),
            None,
        )

    def mailboxes_for_health(self, site_id: str) -> tuple[Phase1Mailbox, ...]:
        rows = tuple(item for item in self.mailboxes if item.site_id == site_id)
        if len(rows) > 100:
            raise ValidationError("mailbox health scope is unbounded")
        return rows

    def list_inbox(
        self,
        actor: GatewayActorScope,
        *,
        state: str | None,
        page_size: int,
        cursor: str | None,
    ) -> Page[Phase1InboxItem]:
        _page_size(page_size)
        if state is not None and state not in _PHASE1_STATES:
            raise ValidationError("invalid inbox state")
        rows = sorted(
            (
                item
                for item in self.inbox_items
                if _authorized(item, actor) and (state is None or item.state == state)
            ),
            key=lambda item: (item.received_at, item.inbox_item_ref),
            reverse=True,
        )
        start = 0
        if cursor is not None:
            received_at, reference = decode_cursor(cursor, "inbox", 2)
            anchors = [
                index
                for index, item in enumerate(rows)
                if item.inbox_item_ref == reference and item.received_at.isoformat() == received_at
            ]
            if not anchors:
                raise ValidationError("invalid cursor")
            start = anchors[0] + 1
        page = rows[start : start + page_size]
        more = start + page_size < len(rows)
        next_cursor = (
            encode_cursor("inbox", page[-1].received_at.isoformat(), page[-1].inbox_item_ref)
            if page and more
            else None
        )
        return Page(tuple(page), next_cursor)

    def get_inbox(self, actor: GatewayActorScope, inbox_item_ref: str) -> Phase1InboxItem | None:
        return next(
            (
                item
                for item in self.inbox_items
                if item.inbox_item_ref == inbox_item_ref and _authorized(item, actor)
            ),
            None,
        )


class PostgresPhase1ReadRepository:
    _MAILBOX_SELECT = """
        SELECT mailbox_ref, address_display_ciphertext, provider, entry_role,
               business_purpose, default_team_ref, account_owner_user_ref,
               inbound_enabled, outbound_enabled, status, config_revision,
               observer_connector_instance_ref
          FROM email_gateway.mailboxes
    """
    _INBOX_SELECT = """
        SELECT inbox.inbox_item_ref, mailbox.address_display_ciphertext,
               mailbox.entry_role, inbox.received_at, inbox.state,
               message.subject_projection_ciphertext, inbox.team_ref,
               inbox.assignee_user_ref,
               COALESCE((
                   SELECT (array_agg(
                       projection.status
                       ORDER BY projection.external_identity_revision DESC
                   ))[1]
                     FROM email_gateway.message_participants AS participant
                     JOIN email_gateway.identity_projection_receipts AS projection
                       ON projection.site_id = participant.site_id
                      AND projection.opaque_address_ref = participant.identity_ref
                    WHERE participant.site_id = inbox.site_id
                      AND participant.message_ref = inbox.message_ref
                      AND participant.role = 'from'
               ), 'unknown') AS identity_state,
               inbox.revision
          FROM email_gateway.inbox_items AS inbox
          JOIN email_gateway.mailboxes AS mailbox
            ON mailbox.site_id = inbox.site_id AND mailbox.mailbox_ref = inbox.mailbox_ref
          JOIN email_gateway.channel_messages AS message
            ON message.site_id = inbox.site_id AND message.message_ref = inbox.message_ref
    """

    def __init__(
        self,
        connection: Connection,
        *,
        decrypt_restricted_text: RestrictedTextDecryptor,
    ) -> None:
        self.connection = connection
        self._decrypt = decrypt_restricted_text

    def list_mailboxes(
        self, site_id: str, *, page_size: int, cursor: str | None
    ) -> Page[Phase1Mailbox]:
        _page_size(page_size)
        anchor = None if cursor is None else decode_cursor(cursor, "mailbox", 1)[0]
        scope = TenantScope(site_id, "business_operations")
        with redacted_database_errors(), site_transaction(self.connection, scope) as db:
            if anchor is not None:
                db.execute(
                    "SELECT 1 FROM email_gateway.mailboxes WHERE site_id = %s AND mailbox_ref = %s",
                    (site_id, anchor),
                )
                if db.fetchone() is None:
                    raise ValidationError("invalid cursor")
            query = self._MAILBOX_SELECT + " WHERE site_id = %s"
            params: tuple[object, ...] = (site_id,)
            if anchor is not None:
                query += " AND mailbox_ref > %s"
                params += (anchor,)
            db.execute(query + " ORDER BY mailbox_ref LIMIT %s", (*params, page_size + 1))
            rows = db.fetchall()
        items = tuple(self._mailbox(site_id, row) for row in rows[:page_size])
        next_cursor = (
            encode_cursor("mailbox", items[-1].mailbox_ref)
            if len(rows) > page_size and items
            else None
        )
        return Page(items, next_cursor)

    def get_mailbox(self, site_id: str, mailbox_ref: str) -> Phase1Mailbox | None:
        scope = TenantScope(site_id, "business_operations")
        with redacted_database_errors(), site_transaction(self.connection, scope) as db:
            db.execute(
                self._MAILBOX_SELECT + " WHERE site_id = %s AND mailbox_ref = %s",
                (site_id, mailbox_ref),
            )
            row = db.fetchone()
        return None if row is None else self._mailbox(site_id, row)

    def mailboxes_for_health(self, site_id: str) -> tuple[Phase1Mailbox, ...]:
        scope = TenantScope(site_id, "business_operations")
        with redacted_database_errors(), site_transaction(self.connection, scope) as db:
            db.execute(
                self._MAILBOX_SELECT + " WHERE site_id = %s ORDER BY mailbox_ref LIMIT 101",
                (site_id,),
            )
            rows = db.fetchall()
        if len(rows) > 100:
            raise ValidationError("mailbox health scope is unbounded")
        return tuple(self._mailbox(site_id, row) for row in rows)

    def list_inbox(
        self,
        actor: GatewayActorScope,
        *,
        state: str | None,
        page_size: int,
        cursor: str | None,
    ) -> Page[Phase1InboxItem]:
        _page_size(page_size)
        if state is not None and state not in _PHASE1_STATES:
            raise ValidationError("invalid inbox state")
        wildcard = _wildcard(actor)
        anchor_time: datetime | None = None
        anchor_ref: str | None = None
        if cursor is not None:
            raw_time, anchor_ref = decode_cursor(cursor, "inbox", 2)
            try:
                anchor_time = datetime.fromisoformat(raw_time)
            except ValueError:
                raise ValidationError("invalid cursor") from None
        scope = TenantScope(actor.site_id, "business_operations")
        predicate = """
            WHERE inbox.site_id = %s
              AND inbox.state IN ('identity_pending', 'unassigned')
              AND (%s OR inbox.team_ref = ANY(%s))
        """
        base_params: tuple[object, ...] = (
            actor.site_id,
            wildcard,
            list(actor.team_refs),
        )
        if state is not None:
            predicate += " AND inbox.state = %s"
            base_params += (state,)
        with redacted_database_errors(), site_transaction(self.connection, scope) as db:
            if anchor_ref is not None:
                db.execute(
                    "SELECT 1 FROM email_gateway.inbox_items AS inbox "
                    + predicate
                    + " AND inbox.inbox_item_ref = %s AND inbox.received_at = %s",
                    (*base_params, anchor_ref, anchor_time),
                )
                if db.fetchone() is None:
                    raise ValidationError("invalid cursor")
            query = self._INBOX_SELECT + predicate
            params = base_params
            if anchor_ref is not None:
                query += " AND (inbox.received_at, inbox.inbox_item_ref) < (%s, %s)"
                params += (anchor_time, anchor_ref)
            db.execute(
                query + " ORDER BY inbox.received_at DESC, inbox.inbox_item_ref DESC LIMIT %s",
                (*params, page_size + 1),
            )
            rows = db.fetchall()
        items = tuple(self._inbox(actor.site_id, row) for row in rows[:page_size])
        next_cursor = (
            encode_cursor("inbox", items[-1].received_at.isoformat(), items[-1].inbox_item_ref)
            if len(rows) > page_size and items
            else None
        )
        return Page(items, next_cursor)

    def get_inbox(self, actor: GatewayActorScope, inbox_item_ref: str) -> Phase1InboxItem | None:
        wildcard = _wildcard(actor)
        scope = TenantScope(actor.site_id, "business_operations")
        with redacted_database_errors(), site_transaction(self.connection, scope) as db:
            db.execute(
                self._INBOX_SELECT
                + """
                    WHERE inbox.site_id = %s
                      AND inbox.inbox_item_ref = %s
                      AND inbox.state IN ('identity_pending', 'unassigned')
                      AND (%s OR inbox.team_ref = ANY(%s))
                """,
                (actor.site_id, inbox_item_ref, wildcard, list(actor.team_refs)),
            )
            row = db.fetchone()
        return None if row is None else self._inbox(actor.site_id, row)

    def _mailbox(self, site_id: str, row: tuple[object, ...]) -> Phase1Mailbox:
        return Phase1Mailbox(
            mailbox_ref=str(row[0]),
            observer_connector_instance_ref=str(row[11]),
            display_label=decrypt_restricted_text(self._decrypt, row[1]),
            provider_kind=str(row[2]),
            business_mode=str(row[3]),
            business_purpose=str(row[4]),
            default_team_ref=str(row[5]),
            account_owner_user_ref=str(row[6]),
            inbound_enabled=bool(row[7]),
            outbound_enabled=bool(row[8]),
            status=str(row[9]),
            config_revision=int(cast(Any, row[10])),
            site_id=site_id,
        )

    def _inbox(self, site_id: str, row: tuple[object, ...]) -> Phase1InboxItem:
        summary = (
            "新邮件" if row[5] is None else decrypt_restricted_text(self._decrypt, row[5])[:500]
        )
        return Phase1InboxItem(
            inbox_item_ref=str(row[0]),
            mailbox_label=decrypt_restricted_text(self._decrypt, row[1]),
            mailbox_role=str(row[2]),
            received_at=cast(datetime, row[3]),
            state=str(row[4]),
            safe_summary=summary,
            team_ref=str(row[6]),
            assignee_user_ref=None if row[7] is None else str(row[7]),
            identity_state=str(row[8]),
            revision=int(cast(Any, row[9])),
            site_id=site_id,
        )


__all__ = ["InMemoryPhase1ReadRepository", "PostgresPhase1ReadRepository"]
