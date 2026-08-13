"""Authenticated HTTP boundaries for publication intake and the Phase 1 BFF."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import replace
from typing import Annotated, Any, Protocol, cast

from fastapi import Body, FastAPI, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .mailboxes import MailboxRegistry
from .models import (
    AuthorizationError,
    EmailMessagePublication,
    GatewayActorScope,
    IdempotencyConflict,
    IntakeResult,
    Mailbox,
    RevisionConflict,
    ScopeViolation,
    TenantScope,
    ValidationError,
    stable_ref,
)
from .phase1_read import Phase1Mailbox
from .repository import ConnectorHealthReader, Phase1ReadRepository

_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_AUTH_REF = "observer-email-publication-v1"
_PURPOSE = "observation_processing"
_BFF_AUTH_REF = "email-gateway-bff-v1"
_ADMIN_ROLES = frozenset({"Integration Admin", "GBOS Admin"})
_INBOX_ROLES = frozenset({"CEO", "Sales Manager", "Sales User", "Reviewer", "GBOS Admin"})
_WILDCARD_ROLES = frozenset({"CEO", "GBOS Admin"})
_ACTOR_FIELDS = frozenset({"actor_ref", "actor_roles", "allowed_team_refs"})
_MAX_REQUEST_BYTES = 262_144
_MAX_CURSOR_LENGTH = 512


class EmailPublicationIntake(Protocol):
    def accept(self, scope: TenantScope, publication: EmailMessagePublication) -> IntakeResult: ...


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

        @application.post("/internal/v1/bff/mailboxes/list")
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

        @application.post("/internal/v1/bff/mailboxes/get")
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

        @application.post("/internal/v1/bff/mailboxes/upsert")
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

        @application.post("/internal/v1/bff/mailboxes/status")
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

        @application.post("/internal/v1/bff/inbox/list")
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
                optional_fields={"state", "cursor"},
            )
            _require_inbox(actor)
            state = values.get("state")
            if state is not None:
                state = _choice(state, "state", {"identity_pending", "unassigned"})
            page = read_repository.list_inbox(
                actor,
                state=state,
                page_size=_page_size(values["page_size"]),
                cursor=_cursor(values.get("cursor")),
            )
            return _success(
                actor.site_id,
                {
                    "inbox_items": [item.to_wire() for item in page.items],
                    "next_cursor": page.next_cursor,
                },
            )

        @application.post("/internal/v1/bff/inbox/get")
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
            item = read_repository.get_inbox(
                actor,
                _bounded_text(values["inbox_item_ref"], "inbox item ref", 140),
            )
            if item is None:
                raise _BFFError("not_found", 404)
            return _success(actor.site_id, {"inbox_item": item.to_wire()})

        @application.post("/internal/v1/bff/email-connectors/health")
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
