"""Closed Frappe authority reads for the independent Email Gateway."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import datetime
from functools import wraps
from typing import Any

import frappe

from esan_gbos.domain.external_identity_projection import (
    ExternalIdentityProjectionError,
    build_external_identity_projection,
    owner_eligibility_revision,
)
from esan_gbos.domain.permissions import EMAIL_GATEWAY_AUTHORITY_ROLE
from esan_gbos.email_gateway_authority_access import (
    email_gateway_authority_permission_scope,
    require_email_gateway_authority_scope,
)

_ROLE = EMAIL_GATEWAY_AUTHORITY_ROLE
_PURPOSE = "email_gateway_authority"
_ALLOWED_RUNTIME_ROLES = frozenset({_ROLE, "All", "Guest", "Website User"})
_TEXT = re.compile(r"[^\x00-\x1f\x7f]+")
_SITE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,139}")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@~-]{0,255}")
_DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
_PROJECT_FIELDS = frozenset(
    {
        "site_id",
        "processing_purpose",
        "request_id",
        "auth_ref",
        "mapping_ref",
        "expected_mapping_revision",
        "expected_team_ref",
    }
)
_ROUTE_FIELDS = frozenset(
    {
        *_PROJECT_FIELDS,
        "expected_party_revision",
        "expected_team_revision",
        "expected_owner_eligibility_revision",
    }
)


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
    headers = frappe.local.response.get("headers")
    if not isinstance(headers, dict):
        headers = {}
        frappe.local.response["headers"] = headers
    headers["Cache-Control"] = "no-store"


@frappe.whitelist(methods=["POST"])  # type: ignore[untyped-decorator]
@_endpoint
def project(payload: dict[str, Any]) -> dict[str, Any]:
    request = _validated_request(payload, _PROJECT_FIELDS)
    with email_gateway_authority_permission_scope(
        request_id=request["request_id"], auth_ref=request["auth_ref"]
    ):
        rows = _mapping_rows(request["mapping_ref"])
        if len(rows) != 1:
            raise _APIError("mapping_not_resolved", 404)
        try:
            projection = build_external_identity_projection(rows[0])
        except ExternalIdentityProjectionError:
            raise _APIError("mapping_not_resolved", 404) from None
        if (
            projection["mapping_revision"] != request["expected_mapping_revision"]
            or projection["team_ref"] != request["expected_team_ref"]
        ):
            raise _APIError("mapping_not_resolved", 404)
        if rows[0].get("target_eligible") != 1 and projection["status"] == "confirmed":
            raise _APIError("mapping_not_resolved", 404)
    return {"identity_projection": projection}


@frappe.whitelist(methods=["POST"])  # type: ignore[untyped-decorator]
@_endpoint
def resolve_route(payload: dict[str, Any]) -> dict[str, Any]:
    request = _validated_request(payload, _ROUTE_FIELDS)
    with email_gateway_authority_permission_scope(
        request_id=request["request_id"], auth_ref=request["auth_ref"]
    ):
        rows = _route_rows(request["mapping_ref"])
        decision = _route_decision(rows, request)
    return {"route_authority": decision}


def _route_decision(
    rows: list[dict[str, Any]],
    request: Mapping[str, Any],
) -> dict[str, Any]:
    resolved_at = datetime.now().astimezone().isoformat(timespec="seconds")
    unavailable = {
        "route_status": "unassigned",
        "safe_reason_code": "owner_unavailable",
        "resolved_at": resolved_at,
    }
    if len(rows) != 1:
        return unavailable
    row = rows[0]
    try:
        resolved_at = _timestamp(row.get("resolved_at"))
        projection = build_external_identity_projection(row)
        party_revision = _row_positive_integer(row.get("party_revision"))
        team_revision = _row_positive_integer(row.get("team_revision"))
        owner = _row_ref(row.get("owner_user_ref"))
        party_ref = _row_ref(row.get("party_ref"))
        state = {
            "owner_enabled": row.get("owner_enabled"),
            "owner_user_type": row.get("owner_user_type"),
            "membership_ref": row.get("membership_ref"),
            "membership_parent": row.get("membership_parent"),
            "membership_user": row.get("membership_user"),
            "membership_enabled": row.get("membership_enabled"),
            "membership_modified": row.get("membership_modified"),
            "team_revision": team_revision,
        }
        owner_revision = owner_eligibility_revision(
            {
                "name": party_ref,
                "revision": party_revision,
                "team": projection["team_ref"],
                "owner_user": owner,
            },
            state,
        )
    except ExternalIdentityProjectionError, ValueError, TypeError:
        return unavailable
    if (
        projection["status"] != "confirmed"
        or projection["target_type"] != "Party"
        or row.get("target_eligible") != 1
        or row.get("party_status") != "Active"
        or row.get("party_review_status") != "Approved"
        or row.get("owner_enabled") != 1
        or row.get("owner_user_type") != "System User"
        or row.get("membership_enabled") != 1
        or row.get("membership_parent") != projection["team_ref"]
        or row.get("membership_user") != owner
        or projection["mapping_revision"] != request["expected_mapping_revision"]
        or projection["team_ref"] != request["expected_team_ref"]
        or party_revision != request["expected_party_revision"]
        or team_revision != request["expected_team_revision"]
        or owner_revision != request["expected_owner_eligibility_revision"]
    ):
        return unavailable
    return {
        "route_status": "assigned",
        "party_ref": party_ref,
        "party_revision": party_revision,
        "team_ref": projection["team_ref"],
        "team_revision": team_revision,
        "owner_user_ref": owner,
        "owner_eligibility_revision": owner_revision,
        "resolved_at": resolved_at,
    }


def _validated_request(payload: dict[str, Any], fields: frozenset[str]) -> dict[str, Any]:
    if set(payload) != fields:
        raise _APIError("invalid_authority_request", 422)
    _authenticate(payload)
    normalized = {
        "site_id": _site(payload.get("site_id")),
        "processing_purpose": _text(payload.get("processing_purpose"), 80),
        "request_id": _text(payload.get("request_id"), 256),
        "auth_ref": _text(payload.get("auth_ref"), 140),
        "mapping_ref": _ref(payload.get("mapping_ref")),
        "expected_mapping_revision": _positive_integer(payload.get("expected_mapping_revision")),
        "expected_team_ref": _ref(payload.get("expected_team_ref")),
    }
    if fields == _ROUTE_FIELDS:
        normalized.update(
            expected_party_revision=_positive_integer(payload.get("expected_party_revision")),
            expected_team_revision=_positive_integer(payload.get("expected_team_revision")),
            expected_owner_eligibility_revision=_digest(
                payload.get("expected_owner_eligibility_revision")
            ),
        )
    return normalized


def _authenticate(payload: Mapping[str, Any]) -> None:
    request = getattr(frappe.local, "request", None)
    actor = str(getattr(frappe.session, "user", ""))
    if request is None or str(getattr(request, "method", "")).upper() != "POST":
        raise _APIError("method_not_allowed", 405)
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
    identities = frappe.conf.get("gbos_email_gateway_authority_identities")
    auth_ref = _text(payload.get("auth_ref"), 140)
    identity = identities.get(auth_ref) if isinstance(identities, Mapping) else None
    site_id = _site(payload.get("site_id"))
    purpose = _text(payload.get("processing_purpose"), 80)
    request_id = _text(payload.get("request_id"), 256)
    if (
        not isinstance(identity, Mapping)
        or set(identity) != {"user", "site_id", "processing_purposes"}
        or identity.get("user") != actor
        or identity.get("site_id") != site_id
        or site_id != str(getattr(frappe.local, "site", ""))
        or purpose != _PURPOSE
        or identity.get("processing_purposes") != [_PURPOSE]
        or headers.get("X-Site-ID") != site_id
        or headers.get("X-Processing-Purpose") != purpose
        or headers.get("X-Request-ID") != request_id
        or headers.get("X-GBOS-Frappe-Auth-Ref") != auth_ref
    ):
        raise _APIError("identity_scope_mismatch", 403)


def _mapping_rows(mapping_ref: str) -> list[dict[str, Any]]:
    require_email_gateway_authority_scope()
    rows = frappe.db.sql(
        """
        select mapping.`name` as `mapping_ref`, mapping.`revision` as `mapping_revision`,
               mapping.`team` as `team_ref`, mapping.`identity_type` as `target_type`,
               mapping.`user` as `user_ref`, mapping.`party_profile` as `party_ref`,
               mapping.`review_status`, mapping.`business_status`,
               mapping.`modified` as `resolved_at`,
               case when mapping.`identity_type` = 'User' then exists (
                    select 1 from `tabUser` u where u.`name` = mapping.`user` and u.`enabled` = 1)
                    and exists (select 1 from `tabGBOS Team Member` m
                    where m.`parent` = mapping.`team`
                    and m.`user` = mapping.`user` and m.`enabled` = 1)
                    when mapping.`identity_type` = 'Party' then exists (
                    select 1 from `tabGBOS Party Profile` p where p.`name` = mapping.`party_profile`
                    and p.`team` = mapping.`team`) else 0 end as `target_eligible`
        from `tabGBOS External Identity` mapping where mapping.`name` = %(mapping_ref)s limit 3
        """,
        {"mapping_ref": mapping_ref},
        as_dict=True,
    )
    return _rows(rows)


def _route_rows(mapping_ref: str) -> list[dict[str, Any]]:
    require_email_gateway_authority_scope()
    rows = frappe.db.sql(
        """
        select mapping.`name` as `mapping_ref`, mapping.`revision` as `mapping_revision`,
               mapping.`team` as `team_ref`, mapping.`identity_type` as `target_type`,
               mapping.`user` as `user_ref`, mapping.`party_profile` as `party_ref`,
               mapping.`review_status`, mapping.`business_status`,
               mapping.`modified` as `resolved_at`,
               party.`revision` as `party_revision`, party.`business_status` as `party_status`,
               party.`review_status` as `party_review_status`, team.`revision` as `team_revision`,
               party.`owner_user` as `owner_user_ref`, owner_user.`enabled` as `owner_enabled`,
               owner_user.`user_type` as `owner_user_type`, member.`name` as `membership_ref`,
               member.`parent` as `membership_parent`, member.`user` as `membership_user`,
               member.`enabled` as `membership_enabled`, member.`modified` as `membership_modified`,
               case when party.`name` is not null
                    and party.`team` = mapping.`team` then 1 else 0 end
                    as `target_eligible`
        from `tabGBOS External Identity` mapping
        left join `tabGBOS Party Profile` party on party.`name` = mapping.`party_profile`
        left join `tabGBOS Team` team on team.`name` = party.`team`
        left join `tabUser` owner_user on owner_user.`name` = party.`owner_user`
        left join `tabGBOS Team Member` member on member.`parent` = party.`team`
             and member.`user` = party.`owner_user`
        where mapping.`name` = %(mapping_ref)s limit 3
        """,
        {"mapping_ref": mapping_ref},
        as_dict=True,
    )
    return _rows(rows)


def _rows(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, Mapping) for row in value):
        raise _APIError("authority_conflict", 409)
    return [dict(row) for row in value]


def _object(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = frappe.parse_json(value)
        except Exception:
            raise _APIError("invalid_authority_request", 422) from None
    if not isinstance(value, Mapping):
        raise _APIError("invalid_authority_request", 422)
    return dict(value)


def _text(value: object, maximum: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or not _TEXT.fullmatch(value):
        raise _APIError("invalid_authority_request", 422)
    return value


def _site(value: object) -> str:
    if not isinstance(value, str) or not _SITE.fullmatch(value):
        raise _APIError("invalid_authority_request", 422)
    return value


def _ref(value: object) -> str:
    if not isinstance(value, str) or not _REF.fullmatch(value):
        raise _APIError("invalid_authority_request", 422)
    return value


def _row_ref(value: object) -> str:
    if not isinstance(value, str) or not _REF.fullmatch(value):
        raise ValueError
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise _APIError("invalid_authority_request", 422)
    return value


def _positive_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2147483647:
        raise _APIError("invalid_authority_request", 422)
    return value


def _row_positive_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2147483647:
        raise ValueError
    return value


def _timestamp(value: object) -> str:
    if isinstance(value, datetime):
        result = value.isoformat()
        return f"{result}Z" if value.tzinfo is None else result
    if not isinstance(value, str):
        raise ValueError
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError
    return value


__all__ = ["project", "resolve_route"]
