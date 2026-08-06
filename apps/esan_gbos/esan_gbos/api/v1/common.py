from __future__ import annotations

import re
from collections.abc import Callable, Collection
from functools import wraps
from typing import Any, TypeVar

import frappe

from esan_gbos.domain.errors import build_error_envelope, build_success_envelope
from esan_gbos.domain.naming import make_gbos_name

Endpoint = TypeVar("Endpoint", bound=Callable[..., dict[str, Any]])
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{8,100}$")


class BFFError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}


def request_id() -> str:
    existing = getattr(frappe.local, "gbos_request_id", None)
    if existing:
        return str(existing)
    supplied = ""
    request = getattr(frappe.local, "request", None)
    if request:
        supplied = request.headers.get("X-Request-ID", "")
    value = supplied if _REQUEST_ID.fullmatch(supplied) else make_gbos_name("REQ")
    frappe.local.gbos_request_id = value
    return value


def success(data: Any, **meta: Any) -> dict[str, Any]:
    return build_success_envelope(
        data=data,
        request_id=request_id(),
        meta=meta,
    )


def failure(error: BFFError) -> dict[str, Any]:
    frappe.local.response["http_status_code"] = error.status
    return build_error_envelope(
        code=error.code,
        request_id=request_id(),
        details=error.details,
    )


def _request_method() -> str:
    request = getattr(frappe.local, "request", None)
    return str(request.method).upper() if request else ""


def _require_request(method: str) -> None:
    actual = _request_method()
    if actual and actual != method:
        raise BFFError(
            "method_not_allowed",
            f"{method} required",
            status=405,
        )
    if frappe.session.user == "Guest":
        raise BFFError("authentication_required", "Login required", status=401)
    # Frappe's Auth.validate_csrf_token runs before whitelisted POST methods.
    # Restricting mutation endpoints to POST preserves that CSRF boundary.


def bff_endpoint(method: str) -> Callable[[Endpoint], Endpoint]:
    """Apply the authoritative BFF authentication and HTTP-method boundary.

    BFF functions are registered with Frappe for guest, GET, and POST dispatch
    so pre-dispatch checks cannot replace this API's stable error envelope.
    This guard still rejects every guest and every unexpected method before the
    endpoint body runs; authenticated unsafe requests remain subject to
    Frappe's earlier CSRF validation.
    """

    expected = method.upper()

    def decorate(function: Endpoint) -> Endpoint:
        @wraps(function)
        def wrapped(*args: Any, **kwargs: Any) -> dict[str, Any]:
            request_id()
            try:
                _require_request(expected)
                return function(*args, **kwargs)
            except BFFError as error:
                if expected == "POST":
                    frappe.db.rollback()
                return failure(error)
            except frappe.PermissionError:
                if expected == "POST":
                    frappe.db.rollback()
                return failure(BFFError("permission_denied", "Permission denied", status=403))
            except frappe.DoesNotExistError:
                if expected == "POST":
                    frappe.db.rollback()
                return failure(BFFError("not_found", "Record not found", status=404))
            except frappe.ValidationError as error:
                if expected == "POST":
                    frappe.db.rollback()
                return failure(BFFError("validation_error", str(error), status=422))
            except Exception as error:
                if expected == "POST":
                    frappe.db.rollback()
                current_request_id = request_id()
                frappe.logger("esan_gbos").error(
                    "BFF internal error request_id=%s exception=%s",
                    current_request_id,
                    type(error).__name__,
                )
                return failure(
                    BFFError(
                        "internal_error",
                        "Internal server error",
                        status=500,
                    )
                )

        return wrapped  # type: ignore[return-value]

    return decorate


def require_roles(allowed: Collection[str]) -> None:
    if not set(frappe.get_roles()) & set(allowed):
        raise BFFError("permission_denied", "Role is not permitted", status=403)


def require_doc_permission(doc: object, permission_type: str) -> None:
    try:
        doc.check_permission(permission_type)  # type: ignore[attr-defined]
    except frappe.PermissionError as error:
        raise BFFError(
            "permission_denied",
            "Record is outside your scope",
            status=403,
        ) from error


def parse_json_object(value: str | dict[str, Any] | None) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    parsed = frappe.parse_json(value) if isinstance(value, str) else value
    if not isinstance(parsed, dict):
        raise BFFError("invalid_dto", "Expected a JSON object")
    return parsed
