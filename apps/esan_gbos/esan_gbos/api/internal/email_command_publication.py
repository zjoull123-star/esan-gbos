from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import Any

import frappe

from esan_gbos.domain.approved_command import validate_email_send_approved_command
from esan_gbos.domain.naming import make_gbos_name
from esan_gbos.domain.permissions import EMAIL_COMMAND_PUBLICATION_ROLE

_ROLE = EMAIL_COMMAND_PUBLICATION_ROLE
_PURPOSE = "email_command_publication"
_ALLOWED_RUNTIME_ROLES = frozenset({_ROLE, "All", "Guest", "Website User"})
_CLAIM_FIELDS = frozenset(
    {"site_id", "processing_purpose", "worker_id", "lease_seconds", "request_id"}
)
_IDENTITY_FIELDS = frozenset(
    {
        "site_id",
        "processing_purpose",
        "worker_id",
        "publication_ref",
        "attempt",
        "generation",
        "fence_token",
        "request_id",
    }
)
_HEARTBEAT_FIELDS = _IDENTITY_FIELDS | {"lease_seconds"}
_ACK_FIELDS = _IDENTITY_FIELDS | {
    "command_receipt_ref",
    "send_outbox_ref",
    "payload_digest",
}
_RELEASE_FIELDS = _IDENTITY_FIELDS | {"safe_code"}
_SAFE_RELEASE_CODES = frozenset(
    {
        "gateway_unavailable",
        "gateway_rate_limited",
        "gateway_rejected_command",
        "authority_recheck_failed",
        "worker_shutdown",
    }
)
_TEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@~-]{0,255}$")
_SITE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,139}$")
_PREFIXED = re.compile(r"^(?P<prefix>[A-Z]{3})-[0-9A-HJKMNP-TV-Z]{26}$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")


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
def claim(payload: dict[str, Any]) -> dict[str, Any]:
    request = _request(payload, _CLAIM_FIELDS)
    _authenticate(request)
    now = _now()
    for row in frappe.get_all(
        "GBOS Command Publication",
        filters={"publication_status": ["in", ["Pending", "Retry", "Claimed"]]},
        fields=["name"],
        order_by="creation asc, name asc",
        page_length=50,
    ):
        publication = frappe.get_doc("GBOS Command Publication", str(row["name"]), for_update=True)
        replay = _claim_replay(publication, request)
        if replay is not None:
            return {"publication": replay}
        if not _claimable(publication, now):
            continue
        if int(publication.attempt or 0) >= int(publication.max_attempts or 0):
            _mutate(publication)
            publication.publication_status = "Dead Letter"
            publication.safe_error_code = "attempts_exhausted"
            publication.save(ignore_permissions=True)
            continue
        _mutate(publication)
        publication.publication_status = "Claimed"
        publication.attempt = int(publication.attempt or 0) + 1
        publication.generation = int(publication.generation or 0) + 1
        publication.worker_id = request["worker_id"]
        publication.fence_token = _new_fence_token()
        publication.lease_expires_at = now + timedelta(seconds=request["lease_seconds"])
        publication.heartbeat_at = now
        publication.claim_request_id = request["request_id"]
        publication.save(ignore_permissions=True)
        return {"publication": _claim_receipt(publication)}
    return {"publication": None}


@frappe.whitelist(methods=["POST"])  # type: ignore[untyped-decorator]
@_endpoint
def heartbeat(payload: dict[str, Any]) -> dict[str, Any]:
    request = _request(payload, _HEARTBEAT_FIELDS)
    _authenticate(request)
    publication = _locked_claim(request)
    now = _now()
    if publication.heartbeat_request_id == request["request_id"]:
        return {"lease": _lease_receipt(publication)}
    _mutate(publication)
    publication.heartbeat_at = now
    publication.lease_expires_at = now + timedelta(seconds=request["lease_seconds"])
    publication.heartbeat_request_id = request["request_id"]
    publication.save(ignore_permissions=True)
    return {"lease": _lease_receipt(publication)}


@frappe.whitelist(methods=["POST"])  # type: ignore[untyped-decorator]
@_endpoint
def acknowledge(payload: dict[str, Any]) -> dict[str, Any]:
    request = _request(payload, _ACK_FIELDS)
    _authenticate(request)
    publication = _locked_claim(request, allow_acknowledged=True)
    if publication.publication_status == "Acknowledged":
        if not _same_ack(publication, request):
            raise _APIError("acknowledgement_conflict", 409)
        return {"acknowledgement": _ack_receipt(publication)}
    if request["payload_digest"] != publication.payload_digest:
        raise _APIError("payload_digest_mismatch", 409)
    _mutate(publication)
    publication.publication_status = "Acknowledged"
    publication.acknowledge_request_id = request["request_id"]
    publication.gateway_command_receipt_ref = request["command_receipt_ref"]
    publication.gateway_send_outbox_ref = request["send_outbox_ref"]
    publication.gateway_payload_digest = request["payload_digest"]
    publication.save(ignore_permissions=True)
    return {"acknowledgement": _ack_receipt(publication)}


@frappe.whitelist(methods=["POST"])  # type: ignore[untyped-decorator]
@_endpoint
def release(payload: dict[str, Any]) -> dict[str, Any]:
    request = _request(payload, _RELEASE_FIELDS)
    _authenticate(request)
    if request["safe_code"] not in _SAFE_RELEASE_CODES:
        raise _APIError("invalid_publication_request", 422)
    publication = _locked_claim(request, allow_released=True)
    if publication.release_request_id == request["request_id"]:
        return {"release": _release_receipt(publication)}
    _mutate(publication)
    publication.safe_error_code = request["safe_code"]
    publication.release_request_id = request["request_id"]
    if int(publication.attempt or 0) >= int(publication.max_attempts or 0):
        publication.publication_status = "Dead Letter"
    else:
        publication.publication_status = "Retry"
    publication.lease_expires_at = None
    publication.save(ignore_permissions=True)
    return {"release": _release_receipt(publication)}


def _request(payload: dict[str, Any], fields: frozenset[str]) -> dict[str, Any]:
    if set(payload) != fields:
        raise _APIError("invalid_publication_request", 422)
    result: dict[str, Any] = {
        "site_id": _site(payload.get("site_id")),
        "processing_purpose": _text(payload.get("processing_purpose")),
        "worker_id": _text(payload.get("worker_id")),
        "request_id": _text(payload.get("request_id")),
    }
    if "lease_seconds" in fields:
        lease = _positive_integer(payload.get("lease_seconds"))
        if not 10 <= lease <= 300:
            raise _APIError("invalid_publication_request", 422)
        result["lease_seconds"] = lease
    if "publication_ref" in fields:
        result.update(
            publication_ref=_prefixed(payload.get("publication_ref"), "PUB"),
            attempt=_positive_integer(payload.get("attempt")),
            generation=_positive_integer(payload.get("generation")),
            fence_token=_prefixed(payload.get("fence_token"), "FNC"),
        )
    if "command_receipt_ref" in fields:
        result.update(
            command_receipt_ref=_prefixed(payload.get("command_receipt_ref"), "ECR"),
            send_outbox_ref=_prefixed(payload.get("send_outbox_ref"), "SND"),
            payload_digest=_digest(payload.get("payload_digest")),
        )
    if "safe_code" in fields:
        result["safe_code"] = _text(payload.get("safe_code"))
    return result


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
    identities = frappe.conf.get("gbos_email_command_publication_identities")
    auth_ref = _text(headers.get("X-GBOS-Frappe-Auth-Ref"))
    identity = identities.get(auth_ref) if isinstance(identities, Mapping) else None
    if (
        not isinstance(identity, Mapping)
        or set(identity) != {"user", "site_id", "processing_purposes"}
        or identity.get("user") != actor
        or identity.get("site_id") != payload["site_id"]
        or payload["site_id"] != str(getattr(frappe.local, "site", ""))
        or payload["processing_purpose"] != _PURPOSE
        or identity.get("processing_purposes") != [_PURPOSE]
        or headers.get("X-Site-ID") != payload["site_id"]
        or headers.get("X-Processing-Purpose") != payload["processing_purpose"]
        or headers.get("X-Request-ID") != payload["request_id"]
    ):
        raise _APIError("identity_scope_mismatch", 403)


def _locked_claim(
    request: Mapping[str, Any],
    *,
    allow_acknowledged: bool = False,
    allow_released: bool = False,
) -> Any:
    try:
        publication = frappe.get_doc(
            "GBOS Command Publication", request["publication_ref"], for_update=True
        )
    except Exception:
        raise _APIError("claim_fence_mismatch", 409) from None
    status_allowed = (
        publication.publication_status == "Claimed"
        or (allow_acknowledged and publication.publication_status == "Acknowledged")
        or (allow_released and publication.publication_status in {"Retry", "Dead Letter"})
    )
    if not status_allowed or any(
        (
            publication.worker_id != request["worker_id"],
            int(publication.attempt or 0) != request["attempt"],
            int(publication.generation or 0) != request["generation"],
            publication.fence_token != request["fence_token"],
        )
    ):
        raise _APIError("claim_fence_mismatch", 409)
    return publication


def _claim_replay(publication: Any, request: Mapping[str, Any]) -> dict[str, Any] | None:
    if (
        publication.publication_status == "Claimed"
        and publication.worker_id == request["worker_id"]
        and publication.claim_request_id == request["request_id"]
    ):
        return _claim_receipt(publication)
    return None


def _claimable(publication: Any, now: datetime) -> bool:
    if publication.publication_status in {"Pending", "Retry"}:
        return True
    return (
        publication.publication_status == "Claimed"
        and _document_time(publication.lease_expires_at) <= now
    )


def _claim_receipt(publication: Any) -> dict[str, Any]:
    command = frappe.parse_json(publication.command_payload)
    validate_email_send_approved_command(command)
    return {
        "publication_ref": publication.name,
        "attempt": int(publication.attempt),
        "generation": int(publication.generation),
        "fence_token": publication.fence_token,
        "lease_expires_at": _iso(_document_time(publication.lease_expires_at)),
        "command": command,
        "payload_digest": publication.payload_digest,
    }


def _lease_receipt(publication: Any) -> dict[str, Any]:
    return {
        "publication_ref": publication.name,
        "attempt": int(publication.attempt),
        "generation": int(publication.generation),
        "fence_token": publication.fence_token,
        "lease_expires_at": _iso(_document_time(publication.lease_expires_at)),
    }


def _same_ack(publication: Any, request: Mapping[str, Any]) -> bool:
    return bool(
        publication.gateway_command_receipt_ref == request["command_receipt_ref"]
        and publication.gateway_send_outbox_ref == request["send_outbox_ref"]
        and publication.gateway_payload_digest == request["payload_digest"]
    )


def _ack_receipt(publication: Any) -> dict[str, Any]:
    return {
        "publication_ref": publication.name,
        "command_receipt_ref": publication.gateway_command_receipt_ref,
        "send_outbox_ref": publication.gateway_send_outbox_ref,
        "payload_digest": publication.gateway_payload_digest,
        "status": "acknowledged",
    }


def _release_receipt(publication: Any) -> dict[str, Any]:
    return {
        "publication_ref": publication.name,
        "status": "dead_letter" if publication.publication_status == "Dead Letter" else "retry",
        "safe_code": publication.safe_error_code,
    }


def _mutate(publication: Any) -> None:
    publication.flags.gbos_publication_worker = True


def _new_fence_token() -> str:
    return str(make_gbos_name("FNC"))


def _now() -> datetime:
    return datetime.now(UTC)


def _document_time(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        return datetime.min.replace(tzinfo=UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _object(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = frappe.parse_json(value)
        except Exception:
            raise _APIError("invalid_publication_request", 422) from None
    if not isinstance(value, Mapping):
        raise _APIError("invalid_publication_request", 422)
    return dict(value)


def _text(value: object) -> str:
    if not isinstance(value, str) or _TEXT.fullmatch(value) is None:
        raise _APIError("invalid_publication_request", 422)
    return value


def _site(value: object) -> str:
    if not isinstance(value, str) or _SITE.fullmatch(value) is None:
        raise _APIError("invalid_publication_request", 422)
    return value


def _positive_integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise _APIError("invalid_publication_request", 422)
    return value


def _prefixed(value: object, prefix: str) -> str:
    if not isinstance(value, str):
        raise _APIError("invalid_publication_request", 422)
    match = _PREFIXED.fullmatch(value)
    if match is None or match.group("prefix") != prefix:
        raise _APIError("invalid_publication_request", 422)
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise _APIError("invalid_publication_request", 422)
    return value


__all__ = ["acknowledge", "claim", "heartbeat", "release"]
