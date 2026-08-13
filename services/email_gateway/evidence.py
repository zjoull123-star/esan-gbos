"""Inbox-bound evidence authorization and the exact Observer reveal transport."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Protocol
from urllib import error as urlerror
from urllib import request as urlrequest

from .models import GatewayActorScope, TenantScope
from .postgres import Connection, redacted_database_errors, site_transaction

OBSERVER_REVEAL_URL = "http://observer-api:8003/internal/v1/bff/evidence/reveal"
_MAX_RESPONSE_BYTES = 262_144


class EvidenceBindingAuthority(Protocol):
    def authorize(
        self,
        scope: TenantScope,
        actor: GatewayActorScope,
        *,
        inbox_item_ref: str,
        evidence_ref: str,
    ) -> str: ...


class PostgresEvidenceBindingAuthority:
    """Checks site/team/inbox/evidence in Gateway SQL before any Observer call."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def __repr__(self) -> str:
        return "PostgresEvidenceBindingAuthority(connection=<redacted>)"

    def authorize(
        self,
        scope: TenantScope,
        actor: GatewayActorScope,
        *,
        inbox_item_ref: str,
        evidence_ref: str,
    ) -> str:
        wildcard = actor.team_refs == ("*",) and bool(
            {"GBOS Admin", "CEO"}.intersection(actor.roles)
        )
        with redacted_database_errors(), site_transaction(self._connection, scope) as cursor:
            cursor.execute(
                """
                SELECT inbox.team_ref
                  FROM email_gateway.inbox_items AS inbox
                  JOIN email_gateway.channel_messages AS message
                    ON message.site_id = inbox.site_id
                   AND message.message_ref = inbox.message_ref
                  JOIN email_gateway.mailboxes AS mailbox
                    ON mailbox.site_id = inbox.site_id
                   AND mailbox.mailbox_ref = inbox.mailbox_ref
                 WHERE inbox.site_id = %s
                   AND inbox.inbox_item_ref = %s
                   AND %s = ANY(message.evidence_refs)
                   AND mailbox.business_purpose = %s
                   AND (%s OR inbox.team_ref = ANY(%s))
                 LIMIT 1
                """,
                (
                    scope.site_id,
                    inbox_item_ref,
                    evidence_ref,
                    scope.processing_purpose,
                    wildcard,
                    list(actor.team_refs),
                ),
            )
            row = cursor.fetchone()
        if row is None:
            raise PermissionError("evidence binding is outside actor scope")
        return str(row[0])


class _NoRedirect(urlrequest.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


class ObserverEvidenceRevealClient:
    def __init__(
        self,
        *,
        bearer_token: str,
        auth_ref: str,
        opener: Any | None = None,
    ) -> None:
        if (
            not bearer_token
            or bearer_token != bearer_token.strip()
            or len(bearer_token) > 4096
            or auth_ref != "gateway-mailbox-projection-v1"
        ):
            raise ValueError("Observer evidence credentials are invalid")
        self._bearer_token = bearer_token
        self._auth_ref = auth_ref
        self._opener = opener or urlrequest.build_opener(urlrequest.ProxyHandler({}), _NoRedirect())

    def __repr__(self) -> str:
        return "ObserverEvidenceRevealClient(token=<redacted>, auth_ref=<redacted>)"

    def reveal(
        self,
        *,
        site_id: str,
        request_id: str,
        authorization: Mapping[str, object],
    ) -> dict[str, object]:
        raw = json.dumps(
            {"authorization": dict(authorization)},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        request = urlrequest.Request(
            OBSERVER_REVEAL_URL,
            data=raw,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._bearer_token}",
                "X-GBOS-Local-Auth-Ref": self._auth_ref,
                "X-Site-ID": site_id,
                "X-Processing-Purpose": "email_evidence_reveal",
                "X-Request-ID": request_id,
            },
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=3.0) as response:
                body = response.read(_MAX_RESPONSE_BYTES + 1)
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
                if (
                    response.status != 200
                    or content_type != "application/json"
                    or len(body) > _MAX_RESPONSE_BYTES
                ):
                    raise ValueError("Observer evidence reveal rejected")
        except ValueError:
            raise
        except (OSError, TimeoutError, urlerror.URLError) as error:
            raise ValueError("Observer evidence reveal rejected") from error
        try:
            envelope = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Observer evidence response rejected") from error
        if not isinstance(envelope, dict) or set(envelope) != {"site_id", "data", "meta"}:
            raise ValueError("Observer evidence response rejected")
        data = envelope.get("data")
        meta = envelope.get("meta")
        if (
            envelope.get("site_id") != site_id
            or not isinstance(data, dict)
            or set(data) != {"content", "media_type"}
            or not isinstance(data.get("content"), str)
            or not isinstance(meta, dict)
            or meta.get("request_id") != request_id
        ):
            raise ValueError("Observer evidence response rejected")
        return {"content": data["content"], "media_type": data["media_type"]}


__all__ = [
    "EvidenceBindingAuthority",
    "OBSERVER_REVEAL_URL",
    "ObserverEvidenceRevealClient",
    "PostgresEvidenceBindingAuthority",
]
