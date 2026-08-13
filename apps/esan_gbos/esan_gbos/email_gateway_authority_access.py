"""Request-scoped capability for the Email Gateway authority endpoint."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import frappe

from esan_gbos.domain.permissions import EMAIL_GATEWAY_AUTHORITY_ROLE

_SCOPE_ATTRIBUTE = "_gbos_email_gateway_authority_scope"
_SCOPE_TOKEN = object()


@dataclass(frozen=True, slots=True)
class _Scope:
    token: object
    actor: str
    request_id: str
    auth_ref: str


def email_gateway_authority_scope_active() -> bool:
    actor = str(getattr(frappe.session, "user", ""))
    scope = getattr(frappe.local, _SCOPE_ATTRIBUTE, None)
    return bool(
        isinstance(scope, _Scope)
        and scope.token is _SCOPE_TOKEN
        and scope.actor == actor
        and EMAIL_GATEWAY_AUTHORITY_ROLE in frappe.get_roles(actor)
    )


def require_email_gateway_authority_scope() -> None:
    if not email_gateway_authority_scope_active():
        raise frappe.PermissionError


@contextmanager
def email_gateway_authority_permission_scope(
    *,
    request_id: str,
    auth_ref: str,
) -> Iterator[None]:
    actor = str(getattr(frappe.session, "user", ""))
    if (
        not actor
        or actor == "Guest"
        or EMAIL_GATEWAY_AUTHORITY_ROLE not in frappe.get_roles(actor)
        or getattr(frappe.local, _SCOPE_ATTRIBUTE, None) is not None
    ):
        raise frappe.PermissionError
    setattr(frappe.local, _SCOPE_ATTRIBUTE, _Scope(_SCOPE_TOKEN, actor, request_id, auth_ref))
    try:
        yield
    finally:
        if hasattr(frappe.local, _SCOPE_ATTRIBUTE):
            delattr(frappe.local, _SCOPE_ATTRIBUTE)


__all__ = [
    "email_gateway_authority_permission_scope",
    "email_gateway_authority_scope_active",
    "require_email_gateway_authority_scope",
]
