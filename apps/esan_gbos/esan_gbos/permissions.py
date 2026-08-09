from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal

import frappe

from esan_gbos.domain.permissions import (
    INTERNAL_MATERIALIZATION_DRAFT_DOCTYPES,
    INTERNAL_MATERIALIZATION_SUBJECT_DOCTYPES,
    INTERNAL_MATERIALIZER_ROLE,
    can_access_crm_record,
    can_access_record,
    role_has_crm_doctype_permission,
)

GLOBAL_LIST_ROLES = frozenset({"GBOS Admin", "CEO"})
TEAM_LIST_ROLES = frozenset(
    {
        "Sales Manager",
        "Sales User",
        "Purchase Manager",
        "Buyer",
        "Product/R&D",
    }
)
INTEGRATION_DOCTYPES = frozenset({"GBOS External Identity", "GBOS External Crosswalk"})
_MATERIALIZATION_SCOPE_ATTRIBUTE = "_gbos_internal_materialization_permission_scope"
_MATERIALIZATION_SCOPE_TOKEN = object()


@dataclass(frozen=True, slots=True)
class _MaterializationPermissionScope:
    token: object
    actor: str
    mode: Literal["resolve", "apply"]


def _roles(user: str) -> set[str]:
    return set(frappe.get_roles(user))


def _is_global_reader(user: str) -> bool:
    return bool(_roles(user) & GLOBAL_LIST_ROLES)


def _is_internal_materializer(user: str) -> bool:
    return INTERNAL_MATERIALIZER_ROLE in _roles(user)


def _internal_materialization_mode(user: str) -> Literal["resolve", "apply"] | None:
    if not _is_internal_materializer(user):
        return None
    scope = getattr(frappe.local, _MATERIALIZATION_SCOPE_ATTRIBUTE, None)
    if (
        not isinstance(scope, _MaterializationPermissionScope)
        or scope.token is not _MATERIALIZATION_SCOPE_TOKEN
        or scope.actor != user
    ):
        return None
    return scope.mode


@contextmanager
def internal_materialization_permission_scope(
    mode: Literal["resolve", "apply"],
) -> Iterator[None]:
    """Open the least Frappe permission set for one authenticated internal call."""
    if mode not in {"resolve", "apply"}:
        raise frappe.PermissionError
    actor = str(getattr(frappe.session, "user", ""))
    if not actor or actor == "Guest" or not _is_internal_materializer(actor):
        raise frappe.PermissionError
    if getattr(frappe.local, _MATERIALIZATION_SCOPE_ATTRIBUTE, None) is not None:
        raise frappe.PermissionError
    scope = _MaterializationPermissionScope(
        token=_MATERIALIZATION_SCOPE_TOKEN,
        actor=actor,
        mode=mode,
    )
    setattr(frappe.local, _MATERIALIZATION_SCOPE_ATTRIBUTE, scope)
    try:
        yield
    finally:
        if hasattr(frappe.local, _MATERIALIZATION_SCOPE_ATTRIBUTE):
            delattr(frappe.local, _MATERIALIZATION_SCOPE_ATTRIBUTE)


def _internal_gbos_permission(
    *,
    user: str,
    doctype: str,
    permission_type: str,
) -> bool | None:
    if not _is_internal_materializer(user):
        return None
    mode = _internal_materialization_mode(user)
    if mode is None:
        return False
    if permission_type == "read":
        return doctype in INTERNAL_MATERIALIZATION_SUBJECT_DOCTYPES | {"GBOS Team"}
    if mode != "apply":
        return False
    if permission_type == "create":
        return doctype in INTERNAL_MATERIALIZATION_DRAFT_DOCTYPES
    if permission_type == "write":
        return doctype in {"GBOS Work Item", "GBOS Review Case"}
    return False


def _member_subquery(user: str) -> str:
    escaped = frappe.db.escape(user)
    return f"select `parent` from `tabGBOS Team Member` where `user` = {escaped} and `enabled` = 1"


def team_permission_query(user: str | None = None) -> str:
    actor = user or frappe.session.user
    if _is_internal_materializer(actor):
        return "1=0"
    if _is_global_reader(actor):
        return ""
    return f"`tabGBOS Team`.`name` in ({_member_subquery(actor)})"


def team_scoped_permission_query(user: str | None = None) -> str:
    actor = user or frappe.session.user
    if _is_internal_materializer(actor):
        return "1=0"
    if _is_global_reader(actor):
        return ""
    return f"`team` in ({_member_subquery(actor)})"


def integration_permission_query(user: str | None = None) -> str:
    actor = user or frappe.session.user
    if _is_internal_materializer(actor):
        return "1=0"
    roles = _roles(actor)
    if roles & {"GBOS Admin", "Integration Admin"}:
        return ""
    return "1=0"


def integration_request_permission_query(user: str | None = None) -> str:
    actor = user or frappe.session.user
    return "1=0" if _is_internal_materializer(actor) else ""


def review_case_permission_query(user: str | None = None) -> str:
    actor = user or frappe.session.user
    if _is_internal_materializer(actor):
        return "1=0"
    if _is_global_reader(actor):
        return ""
    roles = _roles(actor)
    conditions: list[str] = []
    if roles & TEAM_LIST_ROLES:
        conditions.append(f"`team` in ({_member_subquery(actor)})")
    if "Reviewer" in roles:
        conditions.append(f"`assigned_reviewer` = {frappe.db.escape(actor)}")
    return f"({' or '.join(conditions)})" if conditions else "1=0"


def work_item_permission_query(user: str | None = None) -> str:
    actor = user or frappe.session.user
    if _is_internal_materializer(actor):
        return "1=0"
    if _is_global_reader(actor):
        return ""
    roles = _roles(actor)
    conditions: list[str] = []
    if roles & TEAM_LIST_ROLES:
        conditions.append(f"`team` in ({_member_subquery(actor)})")
    if "Reviewer" in roles:
        escaped = frappe.db.escape(actor)
        conditions.append(
            "`tabGBOS Work Item`.`name` in ("
            "select `subject_name` from `tabGBOS Review Case` "
            "where `assigned_reviewer` = "
            f"{escaped} and `subject_doctype` = 'GBOS Work Item')"
        )
    return f"({' or '.join(conditions)})" if conditions else "1=0"


def informal_observation_permission_query(user: str | None = None) -> str:
    actor = user or frappe.session.user
    if _is_internal_materializer(actor):
        return "1=0"
    if _is_global_reader(actor):
        return ""
    if "Reviewer" not in _roles(actor):
        return "1=0"
    escaped = frappe.db.escape(actor)
    return (
        "(`tabGBOS Informal Observation`.`name` in ("
        "select `subject_name` from `tabGBOS Review Case` "
        "where `assigned_reviewer` = "
        f"{escaped} and `subject_doctype` = 'GBOS Informal Observation') "
        "or `tabGBOS Informal Observation`.`name` in ("
        "select `reference_name` from `tabGBOS Work Item` "
        "where `assigned_to` = "
        f"{escaped} and `reference_doctype` = 'GBOS Informal Observation'))"
    )


def _crm_permission_query(doctype: str, user: str | None = None) -> str:
    actor = user or frappe.session.user
    if _is_internal_materializer(actor):
        return "1=0"
    if _is_global_reader(actor):
        return ""
    if not any(role_has_crm_doctype_permission(role, doctype, "read") for role in _roles(actor)):
        return "1=0"
    return f"`tab{doctype}`.`custom_esan_team` in ({_member_subquery(actor)})"


def crm_organization_permission_query(user: str | None = None) -> str:
    return _crm_permission_query("CRM Organization", user)


def crm_lead_permission_query(user: str | None = None) -> str:
    return _crm_permission_query("CRM Lead", user)


def crm_deal_permission_query(user: str | None = None) -> str:
    return _crm_permission_query("CRM Deal", user)


def contact_permission_query(user: str | None = None) -> str:
    return _crm_permission_query("Contact", user)


def _is_team_member(user: str, team: str | None) -> bool:
    if not team:
        return False
    return bool(
        frappe.db.exists(
            "GBOS Team Member",
            {"parent": team, "user": user, "enabled": 1},
        )
    )


def _is_assigned_review_subject(
    user: str,
    doctype: str,
    name: str | None,
) -> bool:
    if not name:
        return False
    if bool(
        frappe.db.exists(
            "GBOS Review Case",
            {
                "assigned_reviewer": user,
                "subject_doctype": doctype,
                "subject_name": name,
                "business_status": "Pending",
            },
        )
    ):
        return True
    return doctype == "GBOS Informal Observation" and bool(
        frappe.db.exists(
            "GBOS Work Item",
            {
                "assigned_to": user,
                "reference_doctype": doctype,
                "reference_name": name,
            },
        )
    )


def has_gbos_permission(
    doc: object,
    user: str | None = None,
    permission_type: str | None = None,
    ptype: str | None = None,
    **kwargs: object,
) -> bool:
    del kwargs
    actor = user or frappe.session.user
    action = permission_type or ptype or "read"
    roles = _roles(actor)
    doctype = str(getattr(doc, "doctype", ""))
    internal_permission = _internal_gbos_permission(
        user=actor,
        doctype=doctype,
        permission_type=action,
    )
    if internal_permission is not None:
        return internal_permission

    if doctype == "GBOS Team" and action != "read":
        return "GBOS Admin" in roles
    if "Integration Admin" in roles and doctype in INTEGRATION_DOCTYPES:
        return action in {"read", "write", "create"}
    if doctype in INTEGRATION_DOCTYPES and action != "read":
        return "GBOS Admin" in roles

    team = getattr(doc, "name", None) if doctype == "GBOS Team" else getattr(doc, "team", None)
    assigned_reviewer = (
        doctype == "GBOS Review Case" and getattr(doc, "assigned_reviewer", None) == actor
    )
    if doctype == "GBOS Review Case" and action != "read":
        return "GBOS Admin" in roles
    return can_access_record(
        roles=roles,
        doctype=doctype,
        permission_type=action,
        is_team_member=_is_team_member(actor, team),
        is_assigned_reviewer=assigned_reviewer,
        is_assigned_review_subject=_is_assigned_review_subject(
            actor,
            doctype,
            getattr(doc, "name", None),
        ),
    )


def protect_ai_draft_command(doc: object, method: str | None = None) -> None:
    """Block generic DocType saves from bypassing the v4 draft command."""
    del method
    before = doc.get_doc_before_save()  # type: ignore[attr-defined]
    if not before or getattr(before, "review_status", None) != "AI Draft":
        return
    if getattr(doc, "review_status", None) == getattr(before, "review_status", None):
        return
    if not getattr(doc, "flags", {}).get("gbos_ai_draft_command"):
        raise frappe.PermissionError
    if (
        getattr(before, "origin", None) != "AI"
        or getattr(doc, "review_status", None) != "Pending"
        or getattr(doc, "business_status", None) != getattr(before, "business_status", None)
    ):
        raise frappe.PermissionError


def has_internal_materialization_permission(
    doc: object,
    user: str | None = None,
    permission_type: str | None = None,
    ptype: str | None = None,
    **kwargs: object,
) -> bool:
    """Keep Integration Request access closed outside the authenticated endpoint scope."""
    del kwargs
    actor = user or frappe.session.user
    if not _is_internal_materializer(actor):
        return True
    if str(getattr(doc, "doctype", "")) != "Integration Request":
        return False
    return _internal_materialization_mode(actor) == "apply" and (
        permission_type or ptype or "read"
    ) in {"read", "write", "create"}


def has_crm_permission(
    doc: object,
    user: str | None = None,
    permission_type: str | None = None,
    ptype: str | None = None,
    **kwargs: object,
) -> bool:
    del kwargs
    actor = user or frappe.session.user
    if _is_internal_materializer(actor):
        return False
    return can_access_crm_record(
        roles=_roles(actor),
        doctype=str(getattr(doc, "doctype", "")),
        permission_type=permission_type or ptype or "read",
        is_team_member=_is_team_member(
            actor,
            getattr(doc, "custom_esan_team", None),
        ),
    )
