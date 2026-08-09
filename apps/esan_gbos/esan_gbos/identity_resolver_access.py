"""Request-scoped capability for the Observer identity-resolution endpoint."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import frappe

from esan_gbos.domain.permissions import IDENTITY_RESOLVER_ROLE

_SCOPE_ATTRIBUTE = "_gbos_identity_resolution_scope"
_SCOPE_TOKEN = object()


@dataclass(frozen=True, slots=True)
class _IdentityResolutionScope:
    token: object
    actor: str
    request_id: str
    auth_ref: str


def identity_resolution_scope_active() -> bool:
    actor = str(getattr(frappe.session, "user", ""))
    scope = getattr(frappe.local, _SCOPE_ATTRIBUTE, None)
    return bool(
        isinstance(scope, _IdentityResolutionScope)
        and scope.token is _SCOPE_TOKEN
        and scope.actor == actor
        and IDENTITY_RESOLVER_ROLE in frappe.get_roles(actor)
    )


def require_identity_resolution_scope() -> None:
    if not identity_resolution_scope_active():
        raise frappe.PermissionError


@contextmanager
def identity_resolution_permission_scope(
    *,
    request_id: str,
    auth_ref: str,
) -> Iterator[None]:
    actor = str(getattr(frappe.session, "user", ""))
    if (
        not actor
        or actor == "Guest"
        or IDENTITY_RESOLVER_ROLE not in frappe.get_roles(actor)
        or getattr(frappe.local, _SCOPE_ATTRIBUTE, None) is not None
    ):
        raise frappe.PermissionError
    setattr(
        frappe.local,
        _SCOPE_ATTRIBUTE,
        _IdentityResolutionScope(
            token=_SCOPE_TOKEN,
            actor=actor,
            request_id=request_id,
            auth_ref=auth_ref,
        ),
    )
    try:
        yield
    finally:
        if hasattr(frappe.local, _SCOPE_ATTRIBUTE):
            delattr(frappe.local, _SCOPE_ATTRIBUTE)


__all__ = [
    "identity_resolution_permission_scope",
    "identity_resolution_scope_active",
    "require_identity_resolution_scope",
]
