"""Authenticated minimum-data resolver for approved external identities."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import datetime
from functools import wraps
from typing import Any

import frappe

from esan_gbos.domain.permissions import IDENTITY_RESOLVER_ROLE
from esan_gbos.identity_resolver_access import (
    identity_resolution_permission_scope,
    require_identity_resolution_scope,
)

_ROLE = IDENTITY_RESOLVER_ROLE
_PURPOSE = "identity_resolution"
_ALLOWED_RUNTIME_ROLES = frozenset({_ROLE, "All", "Guest", "Website User"})
_PROVIDERS = frozenset({"email", "wecom", "whatsapp", "phone", "manual_import"})
_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,127}")
_RAW_EMAIL = re.compile(r"[^@\s]+@[^@\s]+")
_RAW_PHONE = re.compile(r"\+?[0-9][0-9 ()-]{7,}[0-9]")
_TEXT = re.compile(r"[^\x00-\x1f\x7f]+")
_SITE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,139}")
_TEAM = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
_MAPPING_REF = re.compile(r"EID-[0-9A-HJKMNP-TV-Z]{26}")
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})"
)
_REQUEST_FIELDS = frozenset({"site_id", "processing_purpose", "request_id", "auth_ref", "lookups"})
_LOOKUP_REQUIRED = frozenset({"identity_provider", "external_subject_ref", "expected_team_ref"})
_LOOKUP_OPTIONAL = frozenset({"expected_mapping_revision"})
_MAX_BATCH = 100


class _APIError(Exception):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def _endpoint[Endpoint: Callable[[dict[str, Any]], dict[str, Any]]](
    function: Endpoint,
) -> Endpoint:
    @wraps(function)
    def wrapped(payload: str | dict[str, Any]) -> dict[str, Any]:
        try:
            _set_no_store()
            frappe.local.response.pop("http_status_code", None)
            return function(_object(payload))
        except _APIError as error:
            frappe.db.rollback()
            frappe.local.response["http_status_code"] = error.status
            return {"error": {"code": error.code}}
        except frappe.PermissionError:
            frappe.db.rollback()
            frappe.local.response["http_status_code"] = 403
            return {"error": {"code": "permission_denied"}}
        except Exception:
            frappe.db.rollback()
            frappe.local.response["http_status_code"] = 500
            return {"error": {"code": "internal_error"}}

    return wrapped  # type: ignore[return-value]


def _set_no_store() -> None:
    response = frappe.local.response
    headers = response.get("headers")
    if not isinstance(headers, dict):
        headers = {}
        response["headers"] = headers
    headers["Cache-Control"] = "no-store"


@frappe.whitelist(methods=["POST"])  # type: ignore[untyped-decorator]
@_endpoint
def resolve(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != set(_REQUEST_FIELDS):
        raise _APIError("invalid_resolution_request", 422)
    identity = _authenticate(payload)
    del identity
    lookups = _validate_lookups(payload.get("lookups"))
    request_id = _text(payload.get("request_id"), maximum=256)
    auth_ref = _text(payload.get("auth_ref"), maximum=140)
    site_id = _site(payload.get("site_id"))
    with identity_resolution_permission_scope(request_id=request_id, auth_ref=auth_ref):
        resolutions = [_resolve_lookup(site_id, lookup) for lookup in lookups]
    return {"resolutions": resolutions}


def _authenticate(payload: dict[str, Any]) -> Mapping[str, Any]:
    request = getattr(frappe.local, "request", None)
    if request is None or str(getattr(request, "method", "")).upper() != "POST":
        raise _APIError("method_not_allowed", 405)
    actor = str(getattr(frappe.session, "user", ""))
    headers = request.headers
    authorization = str(headers.get("Authorization") or "")
    roles = set(frappe.get_roles(actor))
    if (
        not authorization.startswith("token ")
        or ":" not in authorization[6:]
        or not all(authorization[6:].split(":", 1))
        or actor in {"", "Guest"}
        or _ROLE not in roles
        or bool(roles - _ALLOWED_RUNTIME_ROLES)
    ):
        raise _APIError("authentication_required", 401)

    identities = frappe.conf.get("gbos_identity_resolver_identities")
    if not isinstance(identities, Mapping) or not identities:
        raise _APIError("service_unconfigured", 503)
    auth_ref = _text(payload.get("auth_ref"), maximum=140)
    identity = identities.get(auth_ref)
    if not isinstance(identity, Mapping) or set(identity) != {
        "user",
        "site_id",
        "processing_purposes",
    }:
        raise _APIError("identity_scope_mismatch", 403)
    site_id = _site(payload.get("site_id"))
    purpose = _text(payload.get("processing_purpose"), maximum=80)
    request_id = _text(payload.get("request_id"), maximum=256)
    configured_purposes = identity.get("processing_purposes")
    if (
        identity.get("user") != actor
        or identity.get("site_id") != site_id
        or site_id != str(getattr(frappe.local, "site", ""))
        or purpose != _PURPOSE
        or not isinstance(configured_purposes, list)
        or configured_purposes != [_PURPOSE]
        or headers.get("X-Site-ID") != site_id
        or headers.get("X-Processing-Purpose") != purpose
        or headers.get("X-Request-ID") != request_id
        or headers.get("X-GBOS-Frappe-Auth-Ref") != auth_ref
    ):
        raise _APIError("identity_scope_mismatch", 403)
    return identity


def _validate_lookups(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= _MAX_BATCH:
        raise _APIError("invalid_resolution_request", 422)
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise _APIError("invalid_resolution_request", 422)
        supplied = frozenset(item)
        if not supplied >= _LOOKUP_REQUIRED or supplied - _LOOKUP_REQUIRED - _LOOKUP_OPTIONAL:
            raise _APIError("invalid_resolution_request", 422)
        provider = item.get("identity_provider")
        subject = item.get("external_subject_ref")
        if not isinstance(provider, str) or provider not in _PROVIDERS:
            raise _APIError("invalid_resolution_request", 422)
        _external_subject(provider, subject)
        team = item.get("expected_team_ref")
        if not isinstance(team, str) or _TEAM.fullmatch(team) is None:
            raise _APIError("invalid_resolution_request", 422)
        key = (provider, str(subject))
        if key in seen:
            raise _APIError("invalid_resolution_request", 422)
        seen.add(key)
        lookup: dict[str, Any] = {
            "identity_provider": provider,
            "external_subject_ref": subject,
            "expected_team_ref": team,
        }
        if "expected_mapping_revision" in item:
            lookup["expected_mapping_revision"] = _positive_integer(
                item.get("expected_mapping_revision")
            )
        normalized.append(lookup)
    return normalized


def _resolve_lookup(site_id: str, lookup: dict[str, Any]) -> dict[str, Any]:
    rows = _mapping_rows(
        provider=str(lookup["identity_provider"]),
        external_subject=str(lookup["external_subject_ref"]),
    )
    authoritative = [
        row
        for row in rows
        if row.get("review_status") == "Approved"
        and row.get("business_status") in {"Active", "Revoked"}
    ]
    if not authoritative:
        raise _APIError("mapping_not_resolved", 404)
    if len(authoritative) != 1:
        raise _APIError("mapping_conflict", 409)
    row = authoritative[0]
    team_ref = _bounded_row_text(row.get("team_ref"), maximum=256)
    if team_ref != lookup["expected_team_ref"]:
        raise _APIError("team_scope_mismatch", 403)
    revision = _positive_integer(row.get("mapping_revision"))
    expected_revision = lookup.get("expected_mapping_revision")
    if expected_revision is not None and revision != expected_revision:
        raise _APIError("mapping_revision_conflict", 409)

    target_type = row.get("target_type")
    user_ref = row.get("user_ref")
    party_ref = row.get("party_ref")
    if target_type == "User" and user_ref and not party_ref:
        target_ref = _bounded_row_text(user_ref, maximum=256)
    elif target_type == "Party" and party_ref and not user_ref:
        target_ref = _bounded_row_text(party_ref, maximum=256)
    else:
        raise _APIError("mapping_conflict", 409)
    mapping_ref = _bounded_row_text(row.get("mapping_ref"), maximum=30)
    if _MAPPING_REF.fullmatch(mapping_ref) is None:
        raise _APIError("mapping_conflict", 409)
    return {
        "schema_version": "1.0",
        "site_id": site_id,
        "identity_provider": lookup["identity_provider"],
        "external_subject_ref": lookup["external_subject_ref"],
        "mapping_ref": mapping_ref,
        "mapping_revision": revision,
        "team_ref": team_ref,
        "target_type": target_type,
        "target_ref": target_ref,
        "status": "confirmed" if row.get("business_status") == "Active" else "revoked",
        "resolved_at": _timestamp(row.get("resolved_at")),
    }


def _mapping_rows(*, provider: str, external_subject: str) -> list[dict[str, Any]]:
    require_identity_resolution_scope()
    rows = frappe.db.sql(
        """
        select
            `name` as `mapping_ref`,
            `revision` as `mapping_revision`,
            `team` as `team_ref`,
            `identity_type` as `target_type`,
            `user` as `user_ref`,
            `party_profile` as `party_ref`,
            `review_status`,
            `business_status`,
            `modified` as `resolved_at`
        from `tabGBOS External Identity`
        where `identity_provider` = %(identity_provider)s
          and `external_subject` = %(external_subject)s
        limit 3
        """,
        {
            "identity_provider": provider,
            "external_subject": external_subject,
        },
        as_dict=True,
    )
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise _APIError("mapping_conflict", 409)
    return [dict(row) for row in rows]


def _object(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = frappe.parse_json(value)
        except Exception:
            raise _APIError("invalid_resolution_request", 422) from None
    if not isinstance(value, Mapping):
        raise _APIError("invalid_resolution_request", 422)
    return dict(value)


def _text(value: object, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or _TEXT.fullmatch(value) is None
    ):
        raise _APIError("invalid_resolution_request", 422)
    return value


def _site(value: object) -> str:
    if not isinstance(value, str) or _SITE.fullmatch(value) is None:
        raise _APIError("invalid_resolution_request", 422)
    return value


def _external_subject(provider: str, value: object) -> str:
    if not isinstance(value, str) or len(value) > 160:
        raise _APIError("invalid_resolution_request", 422)
    prefix = f"extid:v1:{provider}:"
    if not value.startswith(prefix):
        raise _APIError("invalid_resolution_request", 422)
    opaque = value[len(prefix) :]
    if (
        _OPAQUE.fullmatch(opaque) is None
        or _RAW_EMAIL.fullmatch(opaque) is not None
        or _RAW_PHONE.fullmatch(opaque) is not None
    ):
        raise _APIError("invalid_resolution_request", 422)
    return value


def _positive_integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > 2147483647:
        raise _APIError("invalid_resolution_request", 422)
    return value


def _bounded_row_text(value: object, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or _TEXT.fullmatch(value) is None
    ):
        raise _APIError("mapping_conflict", 409)
    return value


def _timestamp(value: object) -> str:
    if isinstance(value, datetime):
        serialized = value.isoformat()
        return f"{serialized}Z" if value.tzinfo is None else serialized
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise _APIError("mapping_conflict", 409)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _APIError("mapping_conflict", 409) from None
    if parsed.tzinfo is None:
        raise _APIError("mapping_conflict", 409)
    return value


__all__ = ["resolve"]
