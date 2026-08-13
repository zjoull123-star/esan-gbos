"""Authenticated HTTP boundaries for publication intake and the Phase 1 BFF."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from typing import Annotated, Any, Protocol, cast

from fastapi import Body, FastAPI, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .conversations import ConversationService
from .drafts import DraftService
from .evidence import EvidenceBindingAuthority, ObserverEvidenceRevealClient
from .mailboxes import MailboxRegistry
from .models import (
    AuthorizationError,
    Conversation,
    Draft,
    EmailMessagePublication,
    GatewayActorScope,
    IdempotencyConflict,
    InboxItem,
    IntakeResult,
    Mailbox,
    RevisionConflict,
    RoutingRule,
    ScopeViolation,
    TenantScope,
    ValidationError,
    canonical_digest,
    stable_ref,
)
from .operations import InboxOperations
from .outbound import CommandIngestService, CommandPublication
from .phase1_read import Phase1Mailbox, decode_cursor, encode_cursor
from .postgres import (
    Connection,
    RestrictedTextDecryptor,
    decrypt_restricted_text,
    redacted_database_errors,
    site_transaction,
)
from .repositories.workflow import InMemoryWorkflowRepository, PostgresWorkflowRepository
from .repository import ConnectorHealthReader, Phase1ReadRepository
from .security import CommandIngestAuthorization, GatewayAuthorizationIssuer

_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_AUTH_REF = "observer-email-publication-v1"
_PURPOSE = "observation_processing"
_BFF_AUTH_REF = "email-gateway-bff-v1"
_ADMIN_ROLES = frozenset({"Integration Admin", "GBOS Admin"})
_INBOX_ROLES = frozenset({"CEO", "Sales Manager", "Sales User", "Reviewer", "GBOS Admin"})
_COMMAND_ROLES = frozenset({"Sales Manager", "Sales User", "Reviewer", "GBOS Admin"})
_REVEAL_ROLES = frozenset({"Sales Manager", "Sales User", "Reviewer", "GBOS Admin"})
_WILDCARD_ROLES = frozenset({"CEO", "GBOS Admin"})
_ACTOR_FIELDS = frozenset({"actor_ref", "actor_roles", "allowed_team_refs"})
_MAX_REQUEST_BYTES = 262_144
_MAX_CURSOR_LENGTH = 512
_INBOX_STATES = frozenset(
    {
        "identity_pending",
        "unassigned",
        "assigned",
        "draft",
        "waiting_internal",
        "waiting_customer",
        "converted",
        "closed",
        "quarantined",
        "send_queued",
        "send_uncertain",
    }
)
_INBOX_SORTS = frozenset({"received_at_desc", "sla_due_at_asc"})


class EmailPublicationIntake(Protocol):
    def accept(self, scope: TenantScope, publication: EmailMessagePublication) -> IntakeResult: ...


class GovernedInboxRead(Protocol):
    def list_inbox_closed(
        self,
        actor: GatewayActorScope,
        *,
        state: str | None,
        mailbox_ref: str | None,
        sort: str,
        page_size: int,
        cursor: str | None,
    ) -> tuple[tuple[dict[str, object], ...], str | None]: ...

    def get_inbox_closed(
        self, actor: GatewayActorScope, inbox_item_ref: str
    ) -> dict[str, object] | None: ...


class GatewayAdminRepository(Protocol):
    def list_rules(self, site_id: str) -> tuple[RoutingRule, ...]: ...

    def upsert_rule(
        self,
        scope: TenantScope,
        rule: RoutingRule,
        *,
        expected_revision: int,
        request_id: str,
        idempotency_key: str,
    ) -> RoutingRule: ...


class WorkflowAuthority(Protocol):
    def authorize_inbox(
        self, actor: GatewayActorScope, inbox_item_ref: str
    ) -> tuple[TenantScope, str]: ...

    def authorize_conversation(
        self, actor: GatewayActorScope, conversation_ref: str
    ) -> TenantScope: ...


class _EmptyAdminRepository:
    def list_rules(self, _site_id: str) -> tuple[RoutingRule, ...]:
        return ()

    def upsert_rule(self, *_args: object, **_kwargs: object) -> RoutingRule:
        raise _BFFError("runtime_unavailable", 503)


class _FallbackWorkflowAuthority:
    def __init__(self, repository: Any) -> None:
        self._repository = repository

    def authorize_inbox(
        self, actor: GatewayActorScope, inbox_item_ref: str
    ) -> tuple[TenantScope, str]:
        scope = TenantScope(actor.site_id, "business_operations")
        inbox = self._repository.get_inbox(scope, inbox_item_ref)
        if inbox is None or (actor.team_refs != ("*",) and inbox.team_ref not in actor.team_refs):
            raise _BFFError("scope_mismatch", 403)
        return scope, inbox.team_ref

    def authorize_conversation(
        self, actor: GatewayActorScope, conversation_ref: str
    ) -> TenantScope:
        scope = TenantScope(actor.site_id, "business_operations")
        conversation = self._repository.get_conversation(scope, conversation_ref)
        if conversation is None or (
            actor.team_refs != ("*",) and conversation.team_ref not in actor.team_refs
        ):
            raise _BFFError("scope_mismatch", 403)
        return scope


class PostgresWorkflowAuthority:
    """Resolves one Gateway business purpose after applying actor team scope in SQL."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def authorize_inbox(
        self, actor: GatewayActorScope, inbox_item_ref: str
    ) -> tuple[TenantScope, str]:
        wildcard = actor.team_refs == ("*",) and bool(_WILDCARD_ROLES & set(actor.roles))
        scope = TenantScope(actor.site_id, "business_operations")
        with redacted_database_errors(), site_transaction(self._connection, scope) as db:
            db.execute(
                """
                SELECT mailbox.business_purpose, inbox.team_ref
                  FROM email_gateway.inbox_items AS inbox
                  JOIN email_gateway.mailboxes AS mailbox
                    ON mailbox.site_id = inbox.site_id
                   AND mailbox.mailbox_ref = inbox.mailbox_ref
                 WHERE inbox.site_id = %s
                   AND inbox.inbox_item_ref = %s
                   AND (%s OR inbox.team_ref = ANY(%s))
                 LIMIT 1
                """,
                (actor.site_id, inbox_item_ref, wildcard, list(actor.team_refs)),
            )
            row = db.fetchone()
        if row is None:
            raise AuthorizationError("inbox is outside actor scope")
        return TenantScope(actor.site_id, str(row[0])), str(row[1])

    def authorize_conversation(
        self, actor: GatewayActorScope, conversation_ref: str
    ) -> TenantScope:
        wildcard = actor.team_refs == ("*",) and bool(_WILDCARD_ROLES & set(actor.roles))
        scope = TenantScope(actor.site_id, "business_operations")
        with redacted_database_errors(), site_transaction(self._connection, scope) as db:
            db.execute(
                """
                SELECT min(mailbox.business_purpose), count(DISTINCT mailbox.business_purpose)
                  FROM email_gateway.conversations AS conversation
                  JOIN email_gateway.conversation_messages AS member
                    ON member.site_id = conversation.site_id
                   AND member.conversation_ref = conversation.conversation_ref
                  JOIN email_gateway.inbox_items AS inbox
                    ON inbox.site_id = member.site_id
                   AND inbox.inbox_item_ref = member.inbox_item_ref
                  JOIN email_gateway.mailboxes AS mailbox
                    ON mailbox.site_id = inbox.site_id
                   AND mailbox.mailbox_ref = inbox.mailbox_ref
                 WHERE conversation.site_id = %s
                   AND conversation.conversation_ref = %s
                   AND (%s OR conversation.team_ref = ANY(%s))
                """,
                (actor.site_id, conversation_ref, wildcard, list(actor.team_refs)),
            )
            row = db.fetchone()
        if row is None or row[0] is None or int(row[1]) != 1:
            raise AuthorizationError("conversation is outside actor scope")
        return TenantScope(actor.site_id, str(row[0]))


class PostgresGovernedInboxRead:
    """Full queue projection with actor authorization in WHERE before LIMIT."""

    def __init__(
        self, connection: Connection, *, decrypt_restricted_text: RestrictedTextDecryptor
    ) -> None:
        self._connection = connection
        self._decrypt = decrypt_restricted_text

    def list_inbox_closed(
        self,
        actor: GatewayActorScope,
        *,
        state: str | None,
        mailbox_ref: str | None,
        sort: str,
        page_size: int,
        cursor: str | None,
    ) -> tuple[tuple[dict[str, object], ...], str | None]:
        wildcard = actor.team_refs == ("*",) and bool(_WILDCARD_ROLES & set(actor.roles))
        anchor = None if cursor is None else decode_cursor(cursor, f"inbox-{sort}", 2)
        scope = TenantScope(actor.site_id, "business_operations")
        where = [
            "inbox.site_id = %s",
            "(%s OR inbox.team_ref = ANY(%s))",
        ]
        params: list[object] = [actor.site_id, wildcard, list(actor.team_refs)]
        if state is not None:
            where.append("inbox.state = %s")
            params.append(state)
        if mailbox_ref is not None:
            where.append("inbox.mailbox_ref = %s")
            params.append(mailbox_ref)
        if anchor is not None:
            column = (
                "inbox.received_at"
                if sort == "received_at_desc"
                else "COALESCE(inbox.sla_due_at, 'infinity'::timestamptz)"
            )
            operator = "<" if sort == "received_at_desc" else ">"
            where.append(f"({column}, inbox.inbox_item_ref) {operator} (%s::timestamptz, %s)")
            params.extend(anchor)
        order = (
            "inbox.received_at DESC, inbox.inbox_item_ref DESC"
            if sort == "received_at_desc"
            else "inbox.sla_due_at ASC NULLS LAST, inbox.inbox_item_ref ASC"
        )
        query = f"""
            SELECT inbox.inbox_item_ref, mailbox.address_display_ciphertext,
                   mailbox.entry_role, inbox.received_at, inbox.state,
                   message.subject_projection_ciphertext, inbox.team_ref,
                   inbox.assignee_user_ref, inbox.revision, inbox.sla_due_at,
                   COALESCE((
                       SELECT (array_agg(projection.status ORDER BY
                               projection.external_identity_revision DESC))[1]
                         FROM email_gateway.message_participants AS participant
                         JOIN email_gateway.identity_projection_receipts AS projection
                           ON projection.site_id = participant.site_id
                          AND projection.opaque_address_ref = participant.identity_ref
                        WHERE participant.site_id = inbox.site_id
                          AND participant.message_ref = inbox.message_ref
                          AND participant.role = 'from'
                   ), 'unknown')
              FROM email_gateway.inbox_items AS inbox
              JOIN email_gateway.mailboxes AS mailbox
                ON mailbox.site_id = inbox.site_id
               AND mailbox.mailbox_ref = inbox.mailbox_ref
              JOIN email_gateway.channel_messages AS message
                ON message.site_id = inbox.site_id
               AND message.message_ref = inbox.message_ref
             WHERE {" AND ".join(where)}
             ORDER BY {order}
             LIMIT %s
        """
        with redacted_database_errors(), site_transaction(self._connection, scope) as db:
            db.execute(query, (*params, page_size + 1))
            rows = db.fetchall()
        items = tuple(self._wire(actor.site_id, row, detail=False) for row in rows[:page_size])
        next_cursor = None
        if len(rows) > page_size and rows[:page_size]:
            last = rows[page_size - 1]
            value = last[3] if sort == "received_at_desc" else last[9]
            encoded_value = "infinity" if value is None else value.isoformat()
            next_cursor = encode_cursor(f"inbox-{sort}", encoded_value, str(last[0]))
        return items, next_cursor

    def get_inbox_closed(
        self, actor: GatewayActorScope, inbox_item_ref: str
    ) -> dict[str, object] | None:
        wildcard = actor.team_refs == ("*",) and bool(_WILDCARD_ROLES & set(actor.roles))
        scope = TenantScope(actor.site_id, "business_operations")
        with redacted_database_errors(), site_transaction(self._connection, scope) as db:
            db.execute(
                """
                SELECT inbox.inbox_item_ref, mailbox.address_display_ciphertext,
                       mailbox.entry_role, inbox.received_at, inbox.state,
                       message.subject_projection_ciphertext, inbox.team_ref,
                       inbox.assignee_user_ref, inbox.revision, inbox.sla_due_at,
                       COALESCE((SELECT (array_agg(projection.status ORDER BY
                           projection.external_identity_revision DESC))[1]
                           FROM email_gateway.message_participants AS participant
                           JOIN email_gateway.identity_projection_receipts AS projection
                             ON projection.site_id = participant.site_id
                            AND projection.opaque_address_ref = participant.identity_ref
                          WHERE participant.site_id = inbox.site_id
                            AND participant.message_ref = inbox.message_ref
                            AND participant.role = 'from'), 'unknown')
                  FROM email_gateway.inbox_items AS inbox
                  JOIN email_gateway.mailboxes AS mailbox
                    ON mailbox.site_id = inbox.site_id
                   AND mailbox.mailbox_ref = inbox.mailbox_ref
                  JOIN email_gateway.channel_messages AS message
                    ON message.site_id = inbox.site_id
                   AND message.message_ref = inbox.message_ref
                 WHERE inbox.site_id = %s
                   AND inbox.inbox_item_ref = %s
                   AND (%s OR inbox.team_ref = ANY(%s))
                """,
                (actor.site_id, inbox_item_ref, wildcard, list(actor.team_refs)),
            )
            row = db.fetchone()
        return None if row is None else self._wire(actor.site_id, row, detail=True)

    def _wire(self, site_id: str, row: tuple[Any, ...], *, detail: bool) -> dict[str, object]:
        label = decrypt_restricted_text(self._decrypt, row[1])
        summary = "No subject"
        if row[5] is not None:
            summary = decrypt_restricted_text(self._decrypt, row[5])
        value: dict[str, object] = {
            "inbox_item_ref": str(row[0]),
            "mailbox_label": label,
            "mailbox_role": str(row[2]),
            "received_at": row[3].isoformat(),
            "state": str(row[4]),
            "safe_summary": summary,
            "team_ref": str(row[6]),
            "revision": int(row[8]),
        }
        if detail:
            value.update({"assignee_user_ref": row[7], "identity_state": str(row[10])})
        return value


class PostgresGatewayAdminRepository:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def list_rules(self, site_id: str) -> tuple[RoutingRule, ...]:
        scope = TenantScope(site_id, "business_operations")
        with redacted_database_errors(), site_transaction(self._connection, scope) as db:
            db.execute(
                """
                SELECT rule_ref, site_id, team_ref, mailbox_ref, owner_user_ref,
                       priority, revision, enabled
                  FROM email_gateway.routing_rules
                 WHERE site_id = %s
                 ORDER BY priority DESC, rule_ref
                 LIMIT 1001
                """,
                (site_id,),
            )
            rows = db.fetchall()
        return tuple(RoutingRule(*row) for row in rows)

    def upsert_rule(
        self,
        scope: TenantScope,
        rule: RoutingRule,
        *,
        expected_revision: int,
        request_id: str,
        idempotency_key: str,
    ) -> RoutingRule:
        digest = canonical_digest(_rule_wire(rule))
        with redacted_database_errors(), site_transaction(self._connection, scope) as db:
            db.execute(
                """
                SELECT rule_ref, payload_digest
                  FROM email_gateway.routing_rules
                 WHERE site_id = %s AND idempotency_key = %s
                """,
                (scope.site_id, idempotency_key),
            )
            replay = db.fetchone()
            if replay is not None:
                if str(replay[0]) != rule.rule_ref or str(replay[1]) != digest:
                    raise IdempotencyConflict("routing rule replay conflict")
                return rule
            db.execute(
                """
                SELECT revision FROM email_gateway.routing_rules
                 WHERE site_id = %s AND rule_ref = %s FOR UPDATE
                """,
                (scope.site_id, rule.rule_ref),
            )
            current = db.fetchone()
            actual = 0 if current is None else int(current[0])
            if actual != expected_revision:
                raise RevisionConflict("routing rule revision conflict")
            if current is None:
                db.execute(
                    """
                    INSERT INTO email_gateway.routing_rules (
                        site_id, rule_ref, team_ref, mailbox_ref, owner_user_ref,
                        priority, revision, enabled, request_id, idempotency_key,
                        payload_digest
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        rule.site_id,
                        rule.rule_ref,
                        rule.team_ref,
                        rule.mailbox_ref,
                        rule.owner_user_ref,
                        rule.priority,
                        rule.revision,
                        rule.enabled,
                        request_id,
                        idempotency_key,
                        digest,
                    ),
                )
            else:
                db.execute(
                    """
                    UPDATE email_gateway.routing_rules
                       SET team_ref = %s, mailbox_ref = %s, owner_user_ref = %s,
                           priority = %s, revision = %s, enabled = %s,
                           request_id = %s, idempotency_key = %s,
                           payload_digest = %s, updated_at = now()
                     WHERE site_id = %s AND rule_ref = %s AND revision = %s
                    """,
                    (
                        rule.team_ref,
                        rule.mailbox_ref,
                        rule.owner_user_ref,
                        rule.priority,
                        rule.revision,
                        rule.enabled,
                        request_id,
                        idempotency_key,
                        digest,
                        scope.site_id,
                        rule.rule_ref,
                        expected_revision,
                    ),
                )
        return rule


def build_email_publication_api(
    *,
    intake: EmailPublicationIntake,
    bearer_token: str,
    auth_ref: str,
) -> FastAPI:
    return create_email_gateway_app(
        intake=intake,
        publication_bearer_token=bearer_token,
        publication_auth_ref=auth_ref,
    )


def build_email_command_ingest_api(
    *,
    intake: CommandIngestService,
    bearer_token: str,
    auth_ref: str,
) -> FastAPI:
    """Build the isolated command-executor HTTP boundary."""

    command_auth = CommandIngestAuthorization(
        bearer_token=bearer_token,
        auth_ref=auth_ref,
    )
    application = FastAPI(
        title="ESAN GBOS Email Command Ingest",
        version="1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @application.middleware("http")
    async def command_no_store(_request: Any, call_next: Any) -> Response:
        response = cast(Response, await call_next(_request))
        response.headers["Cache-Control"] = "no-store"
        return response

    @application.post("/internal/v1/email-commands/accept")
    def accept_email_command(
        payload: Annotated[Any, Body()],
        authorization: str | None = Header(default=None),
        request_auth_ref: str | None = Header(default=None, alias="X-GBOS-Local-Auth-Ref"),
        site_id: str | None = Header(default=None, alias="X-Site-ID"),
        processing_purpose: str | None = Header(default=None, alias="X-Processing-Purpose"),
        audience: str | None = Header(default=None, alias="X-Audience"),
        granted_scope: str | None = Header(default=None, alias="X-GBOS-Scope"),
        payload_digest: str | None = Header(default=None, alias="X-Payload-Digest"),
        request_id: str | None = Header(default=None, alias="X-Request-ID"),
    ) -> dict[str, str]:
        if not command_auth.authorize(
            authorization=authorization,
            auth_ref=request_auth_ref,
            audience=audience,
            granted_scope=granted_scope,
        ):
            raise HTTPException(
                status_code=403,
                detail={"code": "email_command_scope_rejected"},
            )
        if (
            not isinstance(payload, dict)
            or set(payload)
            != {
                "publication_ref",
                "attempt",
                "generation",
                "fence_token",
                "payload_digest",
                "command",
            }
            or not isinstance(site_id, str)
            or not isinstance(processing_purpose, str)
            or not isinstance(request_id, str)
            or not request_id
            or payload_digest != payload.get("payload_digest")
            or not isinstance(payload.get("command"), dict)
        ):
            raise HTTPException(
                status_code=400,
                detail={"code": "email_command_request_invalid"},
            )
        try:
            publication = CommandPublication(
                publication_ref=payload["publication_ref"],
                attempt=payload["attempt"],
                generation=payload["generation"],
                fence_token=payload["fence_token"],
                payload_digest=payload["payload_digest"],
            )
            receipt = intake.accept(
                TenantScope(site_id, processing_purpose),
                publication=publication,
                command=payload["command"],
            )
        except IdempotencyConflict as error:
            raise HTTPException(
                status_code=409,
                detail={"code": "email_command_replay_conflict"},
            ) from error
        except ValueError as error:
            raise HTTPException(
                status_code=400,
                detail={"code": "email_command_rejected"},
            ) from error
        return receipt.to_wire()

    return application


class _BFFError(Exception):
    def __init__(self, code: str, status: int) -> None:
        super().__init__("email gateway request rejected")
        self.code = code
        self.status = status


def create_email_gateway_app(
    *,
    intake: EmailPublicationIntake,
    publication_bearer_token: str,
    publication_auth_ref: str,
    bff_bearer_token: str | None = None,
    bff_auth_ref: str | None = None,
    mailbox_registry: MailboxRegistry | None = None,
    read_repository: Phase1ReadRepository | None = None,
    connector_health_reader: ConnectorHealthReader | None = None,
    governed_inbox_read: GovernedInboxRead | None = None,
    workflow_repository: InMemoryWorkflowRepository | PostgresWorkflowRepository | None = None,
    inbox_operations: InboxOperations | None = None,
    conversation_service: ConversationService | None = None,
    draft_service: DraftService | None = None,
    admin_repository: GatewayAdminRepository | None = None,
    workflow_authority: WorkflowAuthority | None = None,
    evidence_authority: EvidenceBindingAuthority | None = None,
    evidence_client: ObserverEvidenceRevealClient | None = None,
    command_ingest_service: CommandIngestService | None = None,
    command_ingest_bearer_token: str | None = None,
    command_ingest_auth_ref: str | None = None,
    clock: Any | None = None,
) -> FastAPI:
    if (
        not publication_bearer_token
        or publication_bearer_token != publication_bearer_token.strip()
        or len(publication_bearer_token) > 4096
        or publication_auth_ref != _AUTH_REF
    ):
        raise ValueError("invalid email publication API credentials")
    bff_values = (
        bff_bearer_token,
        bff_auth_ref,
        mailbox_registry,
        read_repository,
        connector_health_reader,
    )
    bff_enabled = all(value is not None for value in bff_values)
    if any(value is not None for value in bff_values) and not bff_enabled:
        raise ValueError("incomplete Phase 1 BFF composition")
    if bff_enabled and (
        not isinstance(bff_bearer_token, str)
        or not bff_bearer_token
        or bff_bearer_token != bff_bearer_token.strip()
        or len(bff_bearer_token) > 4096
        or bff_auth_ref != _BFF_AUTH_REF
    ):
        raise ValueError("invalid Phase 1 BFF credentials")
    command_values = (
        command_ingest_service,
        command_ingest_bearer_token,
        command_ingest_auth_ref,
    )
    command_enabled = all(value is not None for value in command_values)
    if any(value is not None for value in command_values) and not command_enabled:
        raise ValueError("incomplete email command ingest composition")
    if command_enabled and (
        not isinstance(command_ingest_bearer_token, str)
        or not command_ingest_bearer_token
        or command_ingest_bearer_token != command_ingest_bearer_token.strip()
        or len(command_ingest_bearer_token) > 4096
        or command_ingest_auth_ref != "email-command-ingest-v1"
    ):
        raise ValueError("invalid email command ingest credentials")
    application = FastAPI(
        title="ESAN GBOS Email Gateway",
        version="1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @application.middleware("http")
    async def no_store(request: Any, call_next: Any) -> Response:
        if str(request.url.path).startswith("/internal/v1/bff/") and request.method == "POST":
            length = request.headers.get("content-length")
            if length is not None:
                try:
                    too_large = int(length) > _MAX_REQUEST_BYTES
                except ValueError:
                    too_large = True
                if too_large:
                    return JSONResponse(
                        status_code=400,
                        content={"error": {"code": "invalid_query"}},
                        headers={"Cache-Control": "no-store"},
                    )
        response = cast(Response, await call_next(request))
        response.headers["Cache-Control"] = "no-store"
        return response

    @application.exception_handler(_BFFError)
    async def bff_error(_request: Request, error: _BFFError) -> JSONResponse:
        return JSONResponse(
            status_code=error.status,
            content={"error": {"code": error.code}},
            headers={"Cache-Control": "no-store"},
        )

    @application.exception_handler(RequestValidationError)
    async def request_validation_error(
        request: Request, _error: RequestValidationError
    ) -> JSONResponse:
        if request.url.path.startswith("/internal/v1/bff/"):
            return JSONResponse(
                status_code=400,
                content={"error": {"code": "invalid_query"}},
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            status_code=422,
            content={"detail": "request validation rejected"},
            headers={"Cache-Control": "no-store"},
        )

    @application.exception_handler(RevisionConflict)
    async def revision_error(_request: Request, _error: RevisionConflict) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"error": {"code": "revision_conflict"}},
            headers={"Cache-Control": "no-store"},
        )

    if command_enabled:
        command_auth = CommandIngestAuthorization(
            bearer_token=cast(str, command_ingest_bearer_token),
            auth_ref=cast(str, command_ingest_auth_ref),
        )

        @application.post("/internal/v1/email-commands/accept")
        def accept_email_command(
            payload: Annotated[Any, Body()],
            authorization: str | None = Header(default=None),
            request_auth_ref: str | None = Header(default=None, alias="X-GBOS-Local-Auth-Ref"),
            site_id: str | None = Header(default=None, alias="X-Site-ID"),
            processing_purpose: str | None = Header(default=None, alias="X-Processing-Purpose"),
            audience: str | None = Header(default=None, alias="X-Audience"),
            granted_scope: str | None = Header(default=None, alias="X-GBOS-Scope"),
            payload_digest: str | None = Header(default=None, alias="X-Payload-Digest"),
            request_id: str | None = Header(default=None, alias="X-Request-ID"),
        ) -> dict[str, str]:
            if not command_auth.authorize(
                authorization=authorization,
                auth_ref=request_auth_ref,
                audience=audience,
                granted_scope=granted_scope,
            ):
                raise HTTPException(
                    status_code=403,
                    detail={"code": "email_command_scope_rejected"},
                )
            if (
                not isinstance(payload, dict)
                or set(payload)
                != {
                    "publication_ref",
                    "attempt",
                    "generation",
                    "fence_token",
                    "payload_digest",
                    "command",
                }
                or not isinstance(site_id, str)
                or not isinstance(processing_purpose, str)
                or not isinstance(request_id, str)
                or not request_id
                or payload_digest != payload.get("payload_digest")
                or not isinstance(payload.get("command"), dict)
            ):
                raise HTTPException(
                    status_code=400,
                    detail={"code": "email_command_request_invalid"},
                )
            try:
                publication = CommandPublication(
                    publication_ref=payload["publication_ref"],
                    attempt=payload["attempt"],
                    generation=payload["generation"],
                    fence_token=payload["fence_token"],
                    payload_digest=payload["payload_digest"],
                )
                receipt = cast(CommandIngestService, command_ingest_service).accept(
                    TenantScope(site_id, processing_purpose),
                    publication=publication,
                    command=payload["command"],
                )
            except IdempotencyConflict as error:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "email_command_replay_conflict"},
                ) from error
            except ValueError as error:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "email_command_rejected"},
                ) from error
            return receipt.to_wire()

    @application.exception_handler(IdempotencyConflict)
    async def idempotency_error(_request: Request, _error: IdempotencyConflict) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"error": {"code": "idempotency_conflict"}},
            headers={"Cache-Control": "no-store"},
        )

    @application.exception_handler(ValidationError)
    async def validation_error(_request: Request, error: ValidationError) -> JSONResponse:
        code = (
            "scope_mismatch"
            if isinstance(error, (AuthorizationError, ScopeViolation))
            else "invalid_query"
        )
        return JSONResponse(
            status_code=403 if code == "scope_mismatch" else 400,
            content={"error": {"code": code}},
            headers={"Cache-Control": "no-store"},
        )

    @application.exception_handler(PermissionError)
    async def permission_error(_request: Request, _error: PermissionError) -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content={"error": {"code": "scope_mismatch"}},
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/health")
    def health() -> dict[str, object]:
        return {
            "ready": True,
            "external_send": False,
            "provider_credentials_loaded": False,
        }

    @application.post("/internal/v1/email-publications/accept")
    def accept(
        payload: Annotated[Any, Body()],
        authorization: str | None = Header(default=None),
        request_auth_ref: str | None = Header(default=None, alias="X-GBOS-Local-Auth-Ref"),
        site_id: str | None = Header(default=None, alias="X-Site-ID"),
        processing_purpose: str | None = Header(default=None, alias="X-Processing-Purpose"),
        payload_digest: str | None = Header(default=None, alias="X-Payload-Digest"),
        request_id: str | None = Header(default=None, alias="X-Request-ID"),
    ) -> dict[str, object]:
        expected_authorization = f"Bearer {publication_bearer_token}"
        if authorization is None or not hmac.compare_digest(authorization, expected_authorization):
            raise HTTPException(status_code=401, detail={"code": "email_publication_unauthorized"})
        if request_auth_ref is None or not hmac.compare_digest(
            request_auth_ref, publication_auth_ref
        ):
            raise HTTPException(
                status_code=403, detail={"code": "email_publication_scope_rejected"}
            )
        if processing_purpose != _PURPOSE:
            raise HTTPException(
                status_code=403, detail={"code": "email_publication_scope_rejected"}
            )
        if (
            not isinstance(site_id, str)
            or not site_id
            or not isinstance(request_id, str)
            or not request_id
            or len(request_id) > 256
            or payload_digest is None
            or _DIGEST.fullmatch(payload_digest) is None
        ):
            raise HTTPException(
                status_code=400, detail={"code": "email_publication_request_invalid"}
            )
        calculated = _canonical_digest(payload)
        if not hmac.compare_digest(payload_digest, calculated):
            raise HTTPException(
                status_code=400, detail={"code": "email_publication_digest_mismatch"}
            )
        try:
            publication = EmailMessagePublication.from_wire(
                payload,
                processing_purpose=processing_purpose,
                payload_digest=payload_digest,
            )
            if publication.site_id != site_id:
                raise ValidationError("publication site mismatch")
            result = intake.accept(TenantScope(site_id, processing_purpose), publication)
        except ValidationError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "email_publication_rejected"},
            ) from exc
        return {
            "schema_version": "1.0",
            "receipt_ref": result.receipt.receipt_ref,
            "publication_id": result.receipt.publication_ref,
            "payload_digest": result.receipt.payload_digest,
        }

    if bff_enabled:
        assert bff_bearer_token is not None
        assert mailbox_registry is not None
        assert read_repository is not None
        assert connector_health_reader is not None
        active_clock = clock or (lambda: datetime.now(UTC))
        active_workflow = workflow_repository or InMemoryWorkflowRepository()
        active_operations = inbox_operations or InboxOperations(active_workflow)
        active_conversations = conversation_service or ConversationService(
            cast(Any, active_workflow)
        )
        active_drafts = draft_service or DraftService(cast(Any, active_workflow))
        active_admin = admin_repository or _EmptyAdminRepository()
        active_authority = workflow_authority or _FallbackWorkflowAuthority(active_workflow)
        authorization_issuer = GatewayAuthorizationIssuer(clock=active_clock)

        def command_context(
            *,
            payload: object,
            authorization: str | None,
            request_auth_ref: str | None,
            site_id: str | None,
            purpose: str | None,
            request_id: str | None,
            content_type: str | None,
            header_idempotency_key: str | None,
            operation_fields: set[str],
            optional_fields: set[str] | None = None,
            inbox_field: str = "inbox_item_ref",
        ) -> tuple[GatewayActorScope, dict[str, object], TenantScope, str]:
            _authorize_bff_headers(
                authorization=authorization,
                auth_ref=request_auth_ref,
                site_id=site_id,
                purpose=purpose,
                request_id=request_id,
                content_type=content_type,
                bearer_token=bff_bearer_token,
                expected_purpose="email_inbox_command",
            )
            actor, values = _actor_payload(
                payload,
                site_id=cast(str, site_id),
                operation_fields=operation_fields,
                optional_fields=optional_fields,
            )
            _require_command(actor)
            key = _bounded_text(values["idempotency_key"], "idempotency key", 256)
            _require_idempotency_header(header_idempotency_key, key)
            inbox_ref = _bounded_text(values[inbox_field], "inbox item ref", 140)
            scope, team_ref = active_authority.authorize_inbox(actor, inbox_ref)
            return actor, values, scope, team_ref

        @application.post("/internal/v1/bff/email-admin/mailboxes/list")
        def list_mailboxes(
            payload: Annotated[Any, Body()],
            authorization: str | None = Header(default=None),
            request_auth_ref: str | None = Header(default=None, alias="X-GBOS-Local-Auth-Ref"),
            site_id: str | None = Header(default=None, alias="X-Site-ID"),
            purpose: str | None = Header(default=None, alias="X-Processing-Purpose"),
            request_id: str | None = Header(default=None, alias="X-Request-ID"),
            content_type: str | None = Header(default=None, alias="Content-Type"),
        ) -> dict[str, object]:
            _authorize_bff_headers(
                authorization=authorization,
                auth_ref=request_auth_ref,
                site_id=site_id,
                purpose=purpose,
                request_id=request_id,
                content_type=content_type,
                bearer_token=bff_bearer_token,
                expected_purpose="email_mailbox_read",
            )
            actor, values = _actor_payload(
                payload,
                site_id=cast(str, site_id),
                operation_fields={"page_size"},
                optional_fields={"cursor"},
            )
            _require_admin(actor)
            page_size = _page_size(values["page_size"])
            cursor = _cursor(values.get("cursor"))
            page = read_repository.list_mailboxes(actor.site_id, page_size=page_size, cursor=cursor)
            return _success(
                actor.site_id,
                {
                    "mailboxes": [item.to_wire() for item in page.items],
                    "next_cursor": page.next_cursor,
                },
            )

        @application.post("/internal/v1/bff/email-admin/mailboxes/get")
        def get_mailbox(
            payload: Annotated[Any, Body()],
            authorization: str | None = Header(default=None),
            request_auth_ref: str | None = Header(default=None, alias="X-GBOS-Local-Auth-Ref"),
            site_id: str | None = Header(default=None, alias="X-Site-ID"),
            purpose: str | None = Header(default=None, alias="X-Processing-Purpose"),
            request_id: str | None = Header(default=None, alias="X-Request-ID"),
            content_type: str | None = Header(default=None, alias="Content-Type"),
        ) -> dict[str, object]:
            _authorize_bff_headers(
                authorization=authorization,
                auth_ref=request_auth_ref,
                site_id=site_id,
                purpose=purpose,
                request_id=request_id,
                content_type=content_type,
                bearer_token=bff_bearer_token,
                expected_purpose="email_mailbox_read",
            )
            actor, values = _actor_payload(
                payload,
                site_id=cast(str, site_id),
                operation_fields={"mailbox_ref"},
            )
            _require_admin(actor)
            reference = _bounded_text(values["mailbox_ref"], "mailbox ref", 140)
            mailbox = read_repository.get_mailbox(actor.site_id, reference)
            if mailbox is None:
                raise _BFFError("not_found", 404)
            return _success(actor.site_id, {"mailbox": mailbox.to_wire()})

        @application.post("/internal/v1/bff/email-admin/mailboxes/upsert")
        def upsert_mailbox(
            payload: Annotated[Any, Body()],
            authorization: str | None = Header(default=None),
            request_auth_ref: str | None = Header(default=None, alias="X-GBOS-Local-Auth-Ref"),
            site_id: str | None = Header(default=None, alias="X-Site-ID"),
            purpose: str | None = Header(default=None, alias="X-Processing-Purpose"),
            request_id: str | None = Header(default=None, alias="X-Request-ID"),
            content_type: str | None = Header(default=None, alias="Content-Type"),
            header_idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        ) -> dict[str, object]:
            _authorize_bff_headers(
                authorization=authorization,
                auth_ref=request_auth_ref,
                site_id=site_id,
                purpose=purpose,
                request_id=request_id,
                content_type=content_type,
                bearer_token=bff_bearer_token,
                expected_purpose="email_mailbox_admin",
            )
            required = {
                "display_label",
                "provider_kind",
                "business_mode",
                "business_purpose",
                "provider_account_ref",
                "observer_connector_instance_ref",
                "default_team_ref",
                "account_owner_user_ref",
                "priority",
                "credential_ref",
                "inbound_enabled",
                "outbound_enabled",
                "expected_revision",
                "idempotency_key",
            }
            actor, values = _actor_payload(
                payload,
                site_id=cast(str, site_id),
                operation_fields=required,
                optional_fields={"mailbox_ref"},
            )
            _require_admin(actor)
            idempotency_key = _bounded_text(values["idempotency_key"], "idempotency key", 256)
            _require_idempotency_header(header_idempotency_key, idempotency_key)
            expected_revision = _nonnegative_integer(
                values["expected_revision"], "expected revision"
            )
            business_purpose = _bounded_text(values["business_purpose"], "business purpose", 80)
            scope = TenantScope(actor.site_id, business_purpose)
            reference = (
                stable_ref("MBX", actor.site_id, idempotency_key)
                if "mailbox_ref" not in values
                else _bounded_text(values["mailbox_ref"], "mailbox ref", 140)
            )
            current = mailbox_registry.get(scope, reference)
            mailbox = Mailbox(
                mailbox_ref=reference,
                site_id=actor.site_id,
                address_display=_bounded_text(values["display_label"], "display label", 240),
                provider=_choice(
                    values["provider_kind"],
                    "provider kind",
                    {"fake", "imap_smtp", "wecom_app_mail"},
                ),
                provider_account_ref=_bounded_text(
                    values["provider_account_ref"], "provider account ref", 256
                ),
                observer_connector_instance_ref=_bounded_text(
                    values["observer_connector_instance_ref"],
                    "observer connector instance ref",
                    256,
                ),
                entry_role=_choice(
                    values["business_mode"],
                    "business mode",
                    {"primary", "selective_archive", "migration"},
                ),
                business_purpose=business_purpose,
                default_team_ref=_bounded_text(values["default_team_ref"], "default team ref", 256),
                account_owner_user_ref=_bounded_text(
                    values["account_owner_user_ref"], "account owner user ref", 256
                ),
                priority=_bounded_integer(values["priority"], "priority", 0, 1000),
                inbound_enabled=_boolean(values["inbound_enabled"], "inbound enabled"),
                outbound_enabled=_literal_false(values["outbound_enabled"], "outbound enabled"),
                credential_ref=_bounded_text(values["credential_ref"], "credential ref", 80),
                status="draft" if current is None else current.status,
                config_revision=max(1, expected_revision),
                observer_config_projection_receipt=(
                    None if current is None else current.observer_config_projection_receipt
                ),
            )
            receipt = mailbox_registry.upsert(
                scope,
                mailbox,
                expected_revision=expected_revision,
                actor_ref=actor.actor_ref,
                request_id=cast(str, request_id),
                idempotency_key=idempotency_key,
            )
            return _success(actor.site_id, {"mailbox": _mailbox_wire(receipt.mailbox)})

        @application.post("/internal/v1/bff/email-admin/mailboxes/status")
        def set_mailbox_status(
            payload: Annotated[Any, Body()],
            authorization: str | None = Header(default=None),
            request_auth_ref: str | None = Header(default=None, alias="X-GBOS-Local-Auth-Ref"),
            site_id: str | None = Header(default=None, alias="X-Site-ID"),
            purpose: str | None = Header(default=None, alias="X-Processing-Purpose"),
            request_id: str | None = Header(default=None, alias="X-Request-ID"),
            content_type: str | None = Header(default=None, alias="Content-Type"),
            header_idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        ) -> dict[str, object]:
            _authorize_bff_headers(
                authorization=authorization,
                auth_ref=request_auth_ref,
                site_id=site_id,
                purpose=purpose,
                request_id=request_id,
                content_type=content_type,
                bearer_token=bff_bearer_token,
                expected_purpose="email_mailbox_admin",
            )
            actor, values = _actor_payload(
                payload,
                site_id=cast(str, site_id),
                operation_fields={
                    "mailbox_ref",
                    "action",
                    "expected_revision",
                    "idempotency_key",
                },
            )
            _require_admin(actor)
            reference = _bounded_text(values["mailbox_ref"], "mailbox ref", 140)
            idempotency_key = _bounded_text(values["idempotency_key"], "idempotency key", 256)
            _require_idempotency_header(header_idempotency_key, idempotency_key)
            projection = read_repository.get_mailbox(actor.site_id, reference)
            if projection is None:
                raise _BFFError("not_found", 404)
            scope = TenantScope(actor.site_id, projection.business_purpose)
            current = mailbox_registry.get(scope, reference)
            if current is None:
                raise _BFFError("not_found", 404)
            action = _choice(values["action"], "action", {"enable", "pause", "revoke"})
            expected_revision = _nonnegative_integer(
                values["expected_revision"], "expected revision"
            )
            status, inbound = {
                "enable": ("active", True),
                "pause": ("paused", False),
                "revoke": ("revoked", False),
            }[action]
            receipt = mailbox_registry.upsert(
                scope,
                replace(
                    current,
                    status=status,
                    inbound_enabled=inbound,
                    outbound_enabled=False,
                    config_revision=expected_revision,
                ),
                expected_revision=expected_revision,
                actor_ref=actor.actor_ref,
                request_id=cast(str, request_id),
                idempotency_key=idempotency_key,
            )
            return _success(actor.site_id, {"mailbox": _mailbox_wire(receipt.mailbox)})

        @application.post("/internal/v1/bff/email-inbox/list")
        def list_inbox(
            payload: Annotated[Any, Body()],
            authorization: str | None = Header(default=None),
            request_auth_ref: str | None = Header(default=None, alias="X-GBOS-Local-Auth-Ref"),
            site_id: str | None = Header(default=None, alias="X-Site-ID"),
            purpose: str | None = Header(default=None, alias="X-Processing-Purpose"),
            request_id: str | None = Header(default=None, alias="X-Request-ID"),
            content_type: str | None = Header(default=None, alias="Content-Type"),
        ) -> dict[str, object]:
            _authorize_bff_headers(
                authorization=authorization,
                auth_ref=request_auth_ref,
                site_id=site_id,
                purpose=purpose,
                request_id=request_id,
                content_type=content_type,
                bearer_token=bff_bearer_token,
                expected_purpose="email_inbox_read",
            )
            actor, values = _actor_payload(
                payload,
                site_id=cast(str, site_id),
                operation_fields={"page_size"},
                optional_fields={"state", "mailbox_ref", "sort", "cursor"},
            )
            _require_inbox(actor)
            state = values.get("state")
            if state is not None:
                state = _choice(state, "state", set(_INBOX_STATES))
            mailbox_ref = values.get("mailbox_ref")
            if mailbox_ref is not None:
                mailbox_ref = _bounded_text(mailbox_ref, "mailbox ref", 140)
            sort = _choice(values.get("sort", "received_at_desc"), "sort", set(_INBOX_SORTS))
            page_size = _page_size(values["page_size"])
            cursor = _cursor(values.get("cursor"))
            if governed_inbox_read is not None:
                items, next_cursor = governed_inbox_read.list_inbox_closed(
                    actor,
                    state=state,
                    mailbox_ref=mailbox_ref,
                    sort=sort,
                    page_size=page_size,
                    cursor=cursor,
                )
                item_wires = list(items)
            else:
                if state not in {None, "identity_pending", "unassigned"} or mailbox_ref is not None:
                    raise _BFFError("runtime_unavailable", 503)
                page = read_repository.list_inbox(
                    actor,
                    state=state,
                    page_size=page_size,
                    cursor=cursor,
                )
                item_wires = [item.to_wire() for item in page.items]
                next_cursor = page.next_cursor
            return _success(
                actor.site_id,
                {
                    "inbox_items": item_wires,
                    "next_cursor": next_cursor,
                },
            )

        @application.post("/internal/v1/bff/email-inbox/get")
        def get_inbox(
            payload: Annotated[Any, Body()],
            authorization: str | None = Header(default=None),
            request_auth_ref: str | None = Header(default=None, alias="X-GBOS-Local-Auth-Ref"),
            site_id: str | None = Header(default=None, alias="X-Site-ID"),
            purpose: str | None = Header(default=None, alias="X-Processing-Purpose"),
            request_id: str | None = Header(default=None, alias="X-Request-ID"),
            content_type: str | None = Header(default=None, alias="Content-Type"),
        ) -> dict[str, object]:
            _authorize_bff_headers(
                authorization=authorization,
                auth_ref=request_auth_ref,
                site_id=site_id,
                purpose=purpose,
                request_id=request_id,
                content_type=content_type,
                bearer_token=bff_bearer_token,
                expected_purpose="email_inbox_read",
            )
            actor, values = _actor_payload(
                payload,
                site_id=cast(str, site_id),
                operation_fields={"inbox_item_ref"},
            )
            _require_inbox(actor)
            inbox_ref = _bounded_text(values["inbox_item_ref"], "inbox item ref", 140)
            if governed_inbox_read is not None:
                item_wire = governed_inbox_read.get_inbox_closed(actor, inbox_ref)
            else:
                phase1_item = read_repository.get_inbox(actor, inbox_ref)
                item_wire = None if phase1_item is None else phase1_item.to_wire()
            if item_wire is None:
                raise _BFFError("not_found", 404)
            return _success(actor.site_id, {"inbox_item": item_wire})

        @application.post("/internal/v1/bff/email-inbox/claim")
        def claim_inbox(
            payload: Annotated[Any, Body()],
            authorization: str | None = Header(default=None),
            request_auth_ref: str | None = Header(default=None, alias="X-GBOS-Local-Auth-Ref"),
            site_id: str | None = Header(default=None, alias="X-Site-ID"),
            purpose: str | None = Header(default=None, alias="X-Processing-Purpose"),
            request_id: str | None = Header(default=None, alias="X-Request-ID"),
            content_type: str | None = Header(default=None, alias="Content-Type"),
            header_idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        ) -> dict[str, object]:
            actor, values, scope, _team = command_context(
                payload=payload,
                authorization=authorization,
                request_auth_ref=request_auth_ref,
                site_id=site_id,
                purpose=purpose,
                request_id=request_id,
                content_type=content_type,
                header_idempotency_key=header_idempotency_key,
                operation_fields={"inbox_item_ref", "expected_revision", "idempotency_key"},
            )
            result = active_operations.claim(
                scope,
                actor=actor,
                actor_enabled=True,
                inbox_item_ref=_bounded_text(values["inbox_item_ref"], "inbox item ref", 140),
                expected_revision=_nonnegative_integer(
                    values["expected_revision"], "expected revision"
                ),
                request_id=cast(str, request_id),
                idempotency_key=_bounded_text(values["idempotency_key"], "idempotency key", 256),
                now=active_clock(),
            )
            return _success(actor.site_id, {"inbox_item": _workflow_inbox_wire(result)})

        @application.post("/internal/v1/bff/email-inbox/reassign")
        def reassign_inbox(
            payload: Annotated[Any, Body()],
            authorization: str | None = Header(default=None),
            request_auth_ref: str | None = Header(default=None, alias="X-GBOS-Local-Auth-Ref"),
            site_id: str | None = Header(default=None, alias="X-Site-ID"),
            purpose: str | None = Header(default=None, alias="X-Processing-Purpose"),
            request_id: str | None = Header(default=None, alias="X-Request-ID"),
            content_type: str | None = Header(default=None, alias="Content-Type"),
            header_idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        ) -> dict[str, object]:
            actor, values, scope, _team = command_context(
                payload=payload,
                authorization=authorization,
                request_auth_ref=request_auth_ref,
                site_id=site_id,
                purpose=purpose,
                request_id=request_id,
                content_type=content_type,
                header_idempotency_key=header_idempotency_key,
                operation_fields={
                    "inbox_item_ref",
                    "assignee_team_ref",
                    "assignee_enabled",
                    "expected_revision",
                    "idempotency_key",
                },
                optional_fields={"assignee_user_ref"},
            )
            assignee = values.get("assignee_user_ref")
            if assignee is not None:
                assignee = _bounded_text(assignee, "assignee user ref", 140)
            result = active_operations.reassign(
                scope,
                actor=actor,
                actor_enabled=True,
                inbox_item_ref=_bounded_text(values["inbox_item_ref"], "inbox item ref", 140),
                assignee_user_ref=assignee,
                assignee_team_ref=_bounded_text(
                    values["assignee_team_ref"], "assignee team ref", 140
                ),
                assignee_enabled=_boolean(values["assignee_enabled"], "assignee enabled"),
                expected_revision=_nonnegative_integer(
                    values["expected_revision"], "expected revision"
                ),
                request_id=cast(str, request_id),
                idempotency_key=_bounded_text(values["idempotency_key"], "idempotency key", 256),
                now=active_clock(),
            )
            return _success(actor.site_id, {"inbox_item": _workflow_inbox_wire(result)})

        @application.post("/internal/v1/bff/email-inbox/transition")
        def transition_inbox(
            payload: Annotated[Any, Body()],
            authorization: str | None = Header(default=None),
            request_auth_ref: str | None = Header(default=None, alias="X-GBOS-Local-Auth-Ref"),
            site_id: str | None = Header(default=None, alias="X-Site-ID"),
            purpose: str | None = Header(default=None, alias="X-Processing-Purpose"),
            request_id: str | None = Header(default=None, alias="X-Request-ID"),
            content_type: str | None = Header(default=None, alias="Content-Type"),
            header_idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        ) -> dict[str, object]:
            actor, values, scope, _team = command_context(
                payload=payload,
                authorization=authorization,
                request_auth_ref=request_auth_ref,
                site_id=site_id,
                purpose=purpose,
                request_id=request_id,
                content_type=content_type,
                header_idempotency_key=header_idempotency_key,
                operation_fields={
                    "inbox_item_ref",
                    "target_state",
                    "expected_revision",
                    "idempotency_key",
                },
            )
            result = active_operations.transition(
                scope,
                actor=actor,
                actor_enabled=True,
                inbox_item_ref=_bounded_text(values["inbox_item_ref"], "inbox item ref", 140),
                target_state=_choice(values["target_state"], "target state", set(_INBOX_STATES)),
                expected_revision=_nonnegative_integer(
                    values["expected_revision"], "expected revision"
                ),
                request_id=cast(str, request_id),
                idempotency_key=_bounded_text(values["idempotency_key"], "idempotency key", 256),
                now=active_clock(),
            )
            return _success(actor.site_id, {"inbox_item": _workflow_inbox_wire(result)})

        @application.post("/internal/v1/bff/email-inbox/merge")
        def merge_inbox(
            payload: Annotated[Any, Body()],
            authorization: str | None = Header(default=None),
            request_auth_ref: str | None = Header(default=None, alias="X-GBOS-Local-Auth-Ref"),
            site_id: str | None = Header(default=None, alias="X-Site-ID"),
            purpose: str | None = Header(default=None, alias="X-Processing-Purpose"),
            request_id: str | None = Header(default=None, alias="X-Request-ID"),
            content_type: str | None = Header(default=None, alias="Content-Type"),
            header_idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        ) -> dict[str, object]:
            _authorize_bff_headers(
                authorization=authorization,
                auth_ref=request_auth_ref,
                site_id=site_id,
                purpose=purpose,
                request_id=request_id,
                content_type=content_type,
                bearer_token=bff_bearer_token,
                expected_purpose="email_inbox_command",
            )
            actor, values = _actor_payload(
                payload,
                site_id=cast(str, site_id),
                operation_fields={
                    "suggestion_ref",
                    "left_inbox_item_ref",
                    "expected_suggestion_revision",
                    "expected_left_revision",
                    "expected_right_revision",
                    "idempotency_key",
                },
            )
            _require_command(actor)
            key = _bounded_text(values["idempotency_key"], "idempotency key", 256)
            _require_idempotency_header(header_idempotency_key, key)
            scope, _team = active_authority.authorize_inbox(
                actor,
                _bounded_text(values["left_inbox_item_ref"], "left inbox item ref", 140),
            )
            result = active_conversations.accept(
                scope,
                actor=actor,
                suggestion_ref=_bounded_text(values["suggestion_ref"], "suggestion ref", 140),
                expected_suggestion_revision=_nonnegative_integer(
                    values["expected_suggestion_revision"], "suggestion revision"
                ),
                expected_left_revision=_nonnegative_integer(
                    values["expected_left_revision"], "left revision"
                ),
                expected_right_revision=_nonnegative_integer(
                    values["expected_right_revision"], "right revision"
                ),
                request_id=cast(str, request_id),
                idempotency_key=key,
                now=active_clock(),
            )
            return _success(actor.site_id, {"conversation": _conversation_wire(result)})

        @application.post("/internal/v1/bff/email-inbox/split")
        def split_inbox(
            payload: Annotated[Any, Body()],
            authorization: str | None = Header(default=None),
            request_auth_ref: str | None = Header(default=None, alias="X-GBOS-Local-Auth-Ref"),
            site_id: str | None = Header(default=None, alias="X-Site-ID"),
            purpose: str | None = Header(default=None, alias="X-Processing-Purpose"),
            request_id: str | None = Header(default=None, alias="X-Request-ID"),
            content_type: str | None = Header(default=None, alias="Content-Type"),
            header_idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        ) -> dict[str, object]:
            _authorize_bff_headers(
                authorization=authorization,
                auth_ref=request_auth_ref,
                site_id=site_id,
                purpose=purpose,
                request_id=request_id,
                content_type=content_type,
                bearer_token=bff_bearer_token,
                expected_purpose="email_inbox_command",
            )
            actor, values = _actor_payload(
                payload,
                site_id=cast(str, site_id),
                operation_fields={
                    "conversation_ref",
                    "moved_inbox_item_refs",
                    "expected_revision",
                    "idempotency_key",
                },
            )
            _require_command(actor)
            key = _bounded_text(values["idempotency_key"], "idempotency key", 256)
            _require_idempotency_header(header_idempotency_key, key)
            conversation_ref = _bounded_text(values["conversation_ref"], "conversation ref", 140)
            scope = active_authority.authorize_conversation(actor, conversation_ref)
            conversation = active_workflow.get_conversation(scope, conversation_ref)
            if conversation is None:
                raise _BFFError("not_found", 404)
            moved = _bounded_text_list(
                values["moved_inbox_item_refs"], "moved inbox item refs", 100, 140
            )
            result = active_conversations.split(
                scope,
                actor=actor,
                conversation=conversation,
                moved_inbox_refs=moved,
                expected_revision=_nonnegative_integer(
                    values["expected_revision"], "expected revision"
                ),
                request_id=cast(str, request_id),
                idempotency_key=key,
                now=active_clock(),
            )
            return _success(actor.site_id, {"conversation": _conversation_wire(result)})

        @application.post("/internal/v1/bff/email-inbox/link-business")
        def link_business(
            payload: Annotated[Any, Body()],
            authorization: str | None = Header(default=None),
            request_auth_ref: str | None = Header(default=None, alias="X-GBOS-Local-Auth-Ref"),
            site_id: str | None = Header(default=None, alias="X-Site-ID"),
            purpose: str | None = Header(default=None, alias="X-Processing-Purpose"),
            request_id: str | None = Header(default=None, alias="X-Request-ID"),
            content_type: str | None = Header(default=None, alias="Content-Type"),
            header_idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        ) -> dict[str, object]:
            actor, values, scope, _team = command_context(
                payload=payload,
                authorization=authorization,
                request_auth_ref=request_auth_ref,
                site_id=site_id,
                purpose=purpose,
                request_id=request_id,
                content_type=content_type,
                header_idempotency_key=header_idempotency_key,
                operation_fields={
                    "inbox_item_ref",
                    "business_ref",
                    "authority_valid",
                    "authority_team_ref",
                    "expected_revision",
                    "idempotency_key",
                },
            )
            result = active_operations.link_business(
                scope,
                actor=actor,
                actor_enabled=True,
                inbox_item_ref=_bounded_text(values["inbox_item_ref"], "inbox item ref", 140),
                business_ref=_bounded_text(values["business_ref"], "business ref", 140),
                authority_valid=_boolean(values["authority_valid"], "authority valid"),
                authority_team_ref=_bounded_text(
                    values["authority_team_ref"], "authority team ref", 140
                ),
                expected_revision=_nonnegative_integer(
                    values["expected_revision"], "expected revision"
                ),
                request_id=cast(str, request_id),
                idempotency_key=_bounded_text(values["idempotency_key"], "idempotency key", 256),
                now=active_clock(),
            )
            return _success(actor.site_id, {"inbox_item": _workflow_inbox_wire(result)})

        @application.post("/internal/v1/bff/email-inbox/save-draft")
        def save_draft(
            payload: Annotated[Any, Body()],
            authorization: str | None = Header(default=None),
            request_auth_ref: str | None = Header(default=None, alias="X-GBOS-Local-Auth-Ref"),
            site_id: str | None = Header(default=None, alias="X-Site-ID"),
            purpose: str | None = Header(default=None, alias="X-Processing-Purpose"),
            request_id: str | None = Header(default=None, alias="X-Request-ID"),
            content_type: str | None = Header(default=None, alias="Content-Type"),
            header_idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        ) -> dict[str, object]:
            _authorize_bff_headers(
                authorization=authorization,
                auth_ref=request_auth_ref,
                site_id=site_id,
                purpose=purpose,
                request_id=request_id,
                content_type=content_type,
                bearer_token=bff_bearer_token,
                expected_purpose="email_inbox_command",
            )
            if not isinstance(payload, dict) or payload.get("phase") not in {
                "authorize",
                "commit",
            }:
                raise _BFFError("invalid_query", 400)
            phase = cast(str, payload["phase"])
            common = {
                "phase",
                "actor_ref",
                "actor_roles",
                "allowed_team_refs",
                "inbox_item_ref",
                "draft_ref",
                "expected_revision",
                "content_digest",
                "idempotency_key",
            }
            phase_fields = (
                set()
                if phase == "authorize"
                else {
                    "draft_authorization",
                    "evidence_ref",
                    "evidence_digest",
                    "evidence_revision",
                }
            )
            if set(payload) != common | phase_fields:
                raise _BFFError("invalid_query", 400)
            actor, values = _actor_payload(
                payload,
                site_id=cast(str, site_id),
                operation_fields=common - _ACTOR_FIELDS,
                optional_fields=phase_fields,
            )
            _require_command(actor)
            key = _bounded_text(values["idempotency_key"], "idempotency key", 256)
            _require_idempotency_header(header_idempotency_key, key)
            inbox_ref = _bounded_text(values["inbox_item_ref"], "inbox item ref", 140)
            scope, team_ref = active_authority.authorize_inbox(actor, inbox_ref)
            inbox = active_workflow.get_inbox(scope, inbox_ref)
            if inbox is None:
                raise _BFFError("not_found", 404)
            if "Sales User" in actor.roles and inbox.assignee_user_ref != actor.actor_ref:
                raise _BFFError("scope_mismatch", 403)
            draft_ref = _bounded_text(values["draft_ref"], "draft ref", 140)
            expected_revision = _nonnegative_integer(
                values["expected_revision"], "expected revision"
            )
            content_digest = _bounded_digest(values["content_digest"])
            if phase == "authorize":
                receipt = authorization_issuer.issue_draft(
                    site_id=actor.site_id,
                    actor_ref=actor.actor_ref,
                    team_ref=team_ref,
                    inbox_item_ref=inbox_ref,
                    draft_ref=draft_ref,
                    draft_revision=expected_revision + 1,
                    request_digest=content_digest,
                )
                return _success(actor.site_id, {"draft_authorization": receipt})
            receipt = _validate_draft_authorization(
                values["draft_authorization"],
                site_id=actor.site_id,
                actor_ref=actor.actor_ref,
                team_ref=team_ref,
                inbox_item_ref=inbox_ref,
                draft_ref=draft_ref,
                draft_revision=expected_revision + 1,
                content_digest=content_digest,
                now=active_clock(),
            )
            evidence_ref = _bounded_text(values["evidence_ref"], "evidence ref", 512)
            evidence_digest = _bounded_digest(values["evidence_digest"])
            evidence_revision = _nonnegative_integer(
                values["evidence_revision"], "evidence revision"
            )
            if evidence_digest != content_digest or evidence_revision != receipt["draft_revision"]:
                raise _BFFError("invalid_query", 400)
            worker = GatewayActorScope(
                site_id=actor.site_id,
                actor_ref="email-gateway-bff-draft",
                team_refs=(team_ref,),
                roles=("Email Gateway Worker",),
            )
            if expected_revision == 0:
                draft = active_drafts.create(
                    scope,
                    actor=worker,
                    inbox_item_ref=inbox_ref,
                    conversation_ref=inbox.conversation_ref,
                    content_evidence_ref=evidence_ref,
                    content_digest=evidence_digest,
                    request_id=cast(str, request_id),
                    idempotency_key=key,
                    now=active_clock(),
                )
            else:
                draft = active_drafts.update(
                    scope,
                    actor=worker,
                    draft_ref=draft_ref,
                    expected_revision=expected_revision,
                    content_evidence_ref=evidence_ref,
                    content_digest=evidence_digest,
                    request_id=cast(str, request_id),
                    idempotency_key=key,
                    now=active_clock(),
                )
            return _success(actor.site_id, {"draft": _draft_wire(draft)})

        @application.post("/internal/v1/bff/email-inbox/reveal")
        def reveal_evidence(
            payload: Annotated[Any, Body()],
            authorization: str | None = Header(default=None),
            request_auth_ref: str | None = Header(default=None, alias="X-GBOS-Local-Auth-Ref"),
            site_id: str | None = Header(default=None, alias="X-Site-ID"),
            purpose: str | None = Header(default=None, alias="X-Processing-Purpose"),
            request_id: str | None = Header(default=None, alias="X-Request-ID"),
            content_type: str | None = Header(default=None, alias="Content-Type"),
        ) -> dict[str, object]:
            _authorize_bff_headers(
                authorization=authorization,
                auth_ref=request_auth_ref,
                site_id=site_id,
                purpose=purpose,
                request_id=request_id,
                content_type=content_type,
                bearer_token=bff_bearer_token,
                expected_purpose="email_evidence_reveal",
            )
            actor, values = _actor_payload(
                payload,
                site_id=cast(str, site_id),
                operation_fields={"inbox_item_ref", "evidence_ref"},
            )
            _require_reveal(actor)
            if evidence_authority is None or evidence_client is None:
                raise _BFFError("runtime_unavailable", 503)
            inbox_ref = _bounded_text(values["inbox_item_ref"], "inbox item ref", 140)
            scope, _team = active_authority.authorize_inbox(actor, inbox_ref)
            evidence_ref = _bounded_text(values["evidence_ref"], "evidence ref", 512)
            team_ref = evidence_authority.authorize(
                scope,
                actor,
                inbox_item_ref=inbox_ref,
                evidence_ref=evidence_ref,
            )
            receipt = authorization_issuer.issue_evidence(
                site_id=actor.site_id,
                actor_ref=actor.actor_ref,
                team_ref=team_ref,
                inbox_item_ref=inbox_ref,
                evidence_ref=evidence_ref,
            )
            try:
                revealed = evidence_client.reveal(
                    site_id=actor.site_id,
                    request_id=cast(str, request_id),
                    authorization=receipt,
                )
            except (OSError, ValueError) as error:
                raise _BFFError("runtime_unavailable", 503) from error
            return _success(actor.site_id, {"revealed": revealed})

        @application.post("/internal/v1/bff/email-admin/rules/list")
        def list_rules(
            payload: Annotated[Any, Body()],
            authorization: str | None = Header(default=None),
            request_auth_ref: str | None = Header(default=None, alias="X-GBOS-Local-Auth-Ref"),
            site_id: str | None = Header(default=None, alias="X-Site-ID"),
            purpose: str | None = Header(default=None, alias="X-Processing-Purpose"),
            request_id: str | None = Header(default=None, alias="X-Request-ID"),
            content_type: str | None = Header(default=None, alias="Content-Type"),
        ) -> dict[str, object]:
            _authorize_bff_headers(
                authorization=authorization,
                auth_ref=request_auth_ref,
                site_id=site_id,
                purpose=purpose,
                request_id=request_id,
                content_type=content_type,
                bearer_token=bff_bearer_token,
                expected_purpose="email_mailbox_read",
            )
            actor, values = _actor_payload(
                payload,
                site_id=cast(str, site_id),
                operation_fields={"page_size"},
            )
            _require_admin(actor)
            page_size = _page_size(values["page_size"])
            rows = active_admin.list_rules(actor.site_id)
            if len(rows) > 1000:
                raise _BFFError("invalid_query", 400)
            return _success(
                actor.site_id,
                {"rules": [_rule_wire(item) for item in rows[:page_size]], "next_cursor": None},
            )

        @application.post("/internal/v1/bff/email-admin/rules/upsert")
        def upsert_rule(
            payload: Annotated[Any, Body()],
            authorization: str | None = Header(default=None),
            request_auth_ref: str | None = Header(default=None, alias="X-GBOS-Local-Auth-Ref"),
            site_id: str | None = Header(default=None, alias="X-Site-ID"),
            purpose: str | None = Header(default=None, alias="X-Processing-Purpose"),
            request_id: str | None = Header(default=None, alias="X-Request-ID"),
            content_type: str | None = Header(default=None, alias="Content-Type"),
            header_idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        ) -> dict[str, object]:
            _authorize_bff_headers(
                authorization=authorization,
                auth_ref=request_auth_ref,
                site_id=site_id,
                purpose=purpose,
                request_id=request_id,
                content_type=content_type,
                bearer_token=bff_bearer_token,
                expected_purpose="email_mailbox_admin",
            )
            actor, values = _actor_payload(
                payload,
                site_id=cast(str, site_id),
                operation_fields={
                    "team_ref",
                    "mailbox_ref",
                    "owner_user_ref",
                    "priority",
                    "enabled",
                    "expected_revision",
                    "idempotency_key",
                },
                optional_fields={"rule_ref"},
            )
            _require_admin(actor)
            idempotency_key = _bounded_text(values["idempotency_key"], "idempotency key", 256)
            _require_idempotency_header(header_idempotency_key, idempotency_key)
            expected_revision = _nonnegative_integer(
                values["expected_revision"], "expected revision"
            )
            reference = (
                stable_ref("RUL", actor.site_id, idempotency_key)
                if values.get("rule_ref") is None
                else _bounded_text(values["rule_ref"], "rule ref", 140)
            )
            rule = RoutingRule(
                rule_ref=reference,
                site_id=actor.site_id,
                team_ref=_bounded_text(values["team_ref"], "team ref", 140),
                mailbox_ref=_bounded_text(values["mailbox_ref"], "mailbox ref", 140),
                owner_user_ref=_bounded_text(values["owner_user_ref"], "owner user ref", 140),
                priority=_bounded_integer(values["priority"], "priority", 0, 1000),
                revision=expected_revision + 1,
                enabled=_boolean(values["enabled"], "enabled"),
            )
            result = active_admin.upsert_rule(
                TenantScope(actor.site_id, "business_operations"),
                rule,
                expected_revision=expected_revision,
                request_id=cast(str, request_id),
                idempotency_key=idempotency_key,
            )
            return _success(actor.site_id, {"rule": _rule_wire(result)})

        @application.post("/internal/v1/bff/email-admin/connector-health/get")
        def connector_health(
            payload: Annotated[Any, Body()],
            authorization: str | None = Header(default=None),
            request_auth_ref: str | None = Header(default=None, alias="X-GBOS-Local-Auth-Ref"),
            site_id: str | None = Header(default=None, alias="X-Site-ID"),
            purpose: str | None = Header(default=None, alias="X-Processing-Purpose"),
            request_id: str | None = Header(default=None, alias="X-Request-ID"),
            content_type: str | None = Header(default=None, alias="Content-Type"),
        ) -> dict[str, object]:
            _authorize_bff_headers(
                authorization=authorization,
                auth_ref=request_auth_ref,
                site_id=site_id,
                purpose=purpose,
                request_id=request_id,
                content_type=content_type,
                bearer_token=bff_bearer_token,
                expected_purpose="email_connector_health_read",
            )
            actor, _values = _actor_payload(
                payload,
                site_id=cast(str, site_id),
                operation_fields=set(),
            )
            _require_admin(actor)
            health_mailboxes = read_repository.mailboxes_for_health(actor.site_id)
            rows = connector_health_reader.read(actor.site_id, health_mailboxes)
            mailbox_refs = {item.mailbox_ref for item in health_mailboxes}
            health_refs = [item.mailbox_ref for item in rows]
            if len(health_refs) != len(set(health_refs)) or not set(health_refs).issubset(
                mailbox_refs
            ):
                raise _BFFError("invalid_query", 400)
            return _success(
                actor.site_id,
                {"connector_health": [item.to_wire() for item in rows]},
            )

    return application


def _authorize_bff_headers(
    *,
    authorization: str | None,
    auth_ref: str | None,
    site_id: str | None,
    purpose: str | None,
    request_id: str | None,
    content_type: str | None,
    bearer_token: str,
    expected_purpose: str,
) -> None:
    if authorization is None or not hmac.compare_digest(authorization, f"Bearer {bearer_token}"):
        raise _BFFError("scope_mismatch", 401)
    if auth_ref is None or not hmac.compare_digest(auth_ref, _BFF_AUTH_REF):
        raise _BFFError("scope_mismatch", 403)
    if purpose != expected_purpose:
        raise _BFFError("scope_mismatch", 403)
    _bounded_text(site_id, "site id", 140)
    _bounded_text(request_id, "request id", 256)
    if content_type != "application/json":
        raise _BFFError("invalid_query", 400)


def _actor_payload(
    payload: object,
    *,
    site_id: str,
    operation_fields: set[str],
    optional_fields: set[str] | None = None,
) -> tuple[GatewayActorScope, dict[str, object]]:
    if not isinstance(payload, dict):
        raise _BFFError("invalid_query", 400)
    optional = optional_fields or set()
    required = _ACTOR_FIELDS | operation_fields
    if not required.issubset(payload) or not set(payload).issubset(required | optional):
        raise _BFFError("invalid_query", 400)
    roles = payload.get("actor_roles")
    teams = payload.get("allowed_team_refs")
    if (
        not isinstance(roles, list)
        or not all(isinstance(value, str) for value in roles)
        or len(roles) > 20
        or len(roles) != len(set(roles))
        or not isinstance(teams, list)
        or not all(isinstance(value, str) for value in teams)
        or len(teams) > 100
        or len(teams) != len(set(teams))
    ):
        raise _BFFError("invalid_query", 400)
    try:
        actor = GatewayActorScope(
            site_id=site_id,
            actor_ref=_bounded_text(payload.get("actor_ref"), "actor ref", 256),
            team_refs=tuple(teams),
            roles=tuple(roles),
        )
    except ValidationError as error:
        raise _BFFError("invalid_query", 400) from error
    values = {key: value for key, value in payload.items() if key not in _ACTOR_FIELDS}
    return actor, values


def _require_admin(actor: GatewayActorScope) -> None:
    if not _ADMIN_ROLES.intersection(actor.roles):
        raise _BFFError("scope_mismatch", 403)
    if "*" in actor.team_refs and not _WILDCARD_ROLES.intersection(actor.roles):
        raise _BFFError("scope_mismatch", 403)


def _require_inbox(actor: GatewayActorScope) -> None:
    if not _INBOX_ROLES.intersection(actor.roles):
        raise _BFFError("scope_mismatch", 403)
    if actor.team_refs == ("*",):
        if not _WILDCARD_ROLES.intersection(actor.roles):
            raise _BFFError("scope_mismatch", 403)
        return
    if not actor.team_refs or "*" in actor.team_refs:
        raise _BFFError("scope_mismatch", 403)


def _require_command(actor: GatewayActorScope) -> None:
    if not _COMMAND_ROLES.intersection(actor.roles) or "CEO" in actor.roles:
        raise _BFFError("scope_mismatch", 403)
    if actor.team_refs == ("*",):
        if "GBOS Admin" not in actor.roles:
            raise _BFFError("scope_mismatch", 403)
        return
    if not actor.team_refs or "*" in actor.team_refs:
        raise _BFFError("scope_mismatch", 403)


def _require_reveal(actor: GatewayActorScope) -> None:
    if not _REVEAL_ROLES.intersection(actor.roles) or "CEO" in actor.roles:
        raise _BFFError("scope_mismatch", 403)
    if actor.team_refs == ("*",):
        if "GBOS Admin" not in actor.roles:
            raise _BFFError("scope_mismatch", 403)
        return
    if not actor.team_refs or "*" in actor.team_refs:
        raise _BFFError("scope_mismatch", 403)


def _success(site_id: str, data: dict[str, object]) -> dict[str, object]:
    return {"site_id": site_id, "data": data}


def _bounded_text(value: object, name: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise _BFFError("invalid_query", 400)
    return value


def _boolean(value: object, _name: str) -> bool:
    if not isinstance(value, bool):
        raise _BFFError("invalid_query", 400)
    return value


def _literal_false(value: object, _name: str) -> bool:
    if value is not False:
        raise _BFFError("invalid_query", 400)
    return False


def _bounded_integer(value: object, _name: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise _BFFError("invalid_query", 400)
    return value


def _nonnegative_integer(value: object, name: str) -> int:
    return _bounded_integer(value, name, 0, 2_147_483_647)


def _page_size(value: object) -> int:
    return _bounded_integer(value, "page size", 1, 50)


def _choice(value: object, name: str, choices: set[str]) -> str:
    text = _bounded_text(value, name, 80)
    if text not in choices:
        raise _BFFError("invalid_query", 400)
    return text


def _cursor(value: object) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, "cursor", _MAX_CURSOR_LENGTH)


def _require_idempotency_header(header: str | None, payload: str) -> None:
    if header is None or not hmac.compare_digest(header, payload):
        raise _BFFError("idempotency_conflict", 409)


def _bounded_text_list(
    value: object, name: str, maximum_items: int, maximum_text: int
) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > maximum_items
        or len(value) != len(set(value))
    ):
        raise _BFFError("invalid_query", 400)
    return tuple(_bounded_text(item, name, maximum_text) for item in value)


def _bounded_digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise _BFFError("invalid_query", 400)
    return value


def _workflow_inbox_wire(inbox: InboxItem) -> dict[str, object]:
    return {
        "inbox_item_ref": inbox.inbox_item_ref,
        "state": inbox.state,
        "team_ref": inbox.team_ref,
        "assignee_user_ref": inbox.assignee_user_ref,
        "conversation_ref": inbox.conversation_ref,
        "business_links": list(inbox.business_links),
        "revision": inbox.revision,
    }


def _conversation_wire(conversation: Conversation) -> dict[str, object]:
    return {
        "conversation_ref": conversation.conversation_ref,
        "team_ref": conversation.team_ref,
        "lifecycle_state": conversation.lifecycle_state,
        "inbox_item_refs": list(conversation.inbox_item_refs),
        "revision": conversation.revision,
    }


def _draft_wire(draft: Draft) -> dict[str, object]:
    return {"draft_ref": draft.draft_ref, "revision": draft.revision, "state": draft.state}


def _rule_wire(rule: RoutingRule) -> dict[str, object]:
    return {
        "rule_ref": rule.rule_ref,
        "team_ref": rule.team_ref,
        "mailbox_ref": rule.mailbox_ref,
        "owner_user_ref": rule.owner_user_ref,
        "priority": rule.priority,
        "revision": rule.revision,
        "enabled": rule.enabled,
    }


def _validate_draft_authorization(
    value: object,
    *,
    site_id: str,
    actor_ref: str,
    team_ref: str,
    inbox_item_ref: str,
    draft_ref: str,
    draft_revision: int,
    content_digest: str,
    now: datetime,
) -> dict[str, object]:
    fields = {
        "receipt_ref",
        "site_id",
        "purpose",
        "inbox_item_ref",
        "draft_ref",
        "draft_revision",
        "actor_ref",
        "team_ref",
        "request_digest",
        "issued_at",
        "expires_at",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise _BFFError("scope_mismatch", 403)
    expected = {
        "site_id": site_id,
        "purpose": "email_draft_material",
        "inbox_item_ref": inbox_item_ref,
        "draft_ref": draft_ref,
        "draft_revision": draft_revision,
        "actor_ref": actor_ref,
        "team_ref": team_ref,
        "request_digest": content_digest,
    }
    if any(value.get(field) != expected_value for field, expected_value in expected.items()):
        raise _BFFError("scope_mismatch", 403)
    issued_at = _parse_wire_time(value.get("issued_at"))
    expires_at = _parse_wire_time(value.get("expires_at"))
    if now.tzinfo is None:
        raise _BFFError("runtime_unavailable", 503)
    normalized = now.astimezone(UTC)
    if (
        expires_at <= issued_at
        or (expires_at - issued_at).total_seconds() > 300
        or not issued_at <= normalized <= expires_at
    ):
        raise _BFFError("scope_mismatch", 403)
    _bounded_text(value.get("receipt_ref"), "receipt ref", 140)
    return dict(value)


def _parse_wire_time(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise _BFFError("scope_mismatch", 403)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _BFFError("scope_mismatch", 403) from None
    if parsed.tzinfo is None:
        raise _BFFError("scope_mismatch", 403)
    return parsed.astimezone(UTC)


def _mailbox_wire(mailbox: Mailbox) -> dict[str, object]:
    return Phase1Mailbox(
        mailbox_ref=mailbox.mailbox_ref,
        observer_connector_instance_ref=mailbox.observer_connector_instance_ref,
        display_label=mailbox.address_display,
        provider_kind=mailbox.provider,
        business_mode=mailbox.entry_role,
        business_purpose=mailbox.business_purpose,
        default_team_ref=mailbox.default_team_ref,
        account_owner_user_ref=mailbox.account_owner_user_ref,
        inbound_enabled=mailbox.inbound_enabled,
        outbound_enabled=False,
        status=mailbox.status,
        config_revision=mailbox.config_revision,
        site_id=mailbox.site_id,
    ).to_wire()


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


__all__ = [
    "EmailPublicationIntake",
    "build_email_publication_api",
    "create_email_gateway_app",
]
