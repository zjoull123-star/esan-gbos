from __future__ import annotations

from collections.abc import Collection


class PermissionScopeError(ValueError):
    """The current actor cannot be resolved to a safe downstream scope."""


def communication_scope(
    *,
    roles: Collection[str],
    actor_ref: str,
    team_refs: Collection[str],
) -> dict[str, object]:
    actor = actor_ref.strip()
    if not actor:
        raise PermissionScopeError("actor reference is required")
    assigned_roles = frozenset(roles)
    if assigned_roles & {"CEO", "GBOS Admin"}:
        allowed = ["*"]
        scope = "all_business_projection"
    else:
        allowed = sorted({team.strip() for team in team_refs if team.strip()})
        if not allowed:
            raise PermissionScopeError("a resolved team scope is required")
        scope = "team_and_self"
    return {
        "actor_ref": actor,
        "allowed_team_refs": allowed,
        "scope": scope,
        "include_raw": False,
    }


_BUSINESS_DOCTYPES = frozenset(
    {
        "GBOS Party Profile",
        "GBOS Product Brief",
        "GBOS Sample Project",
        "GBOS Sample Iteration",
        "GBOS Sample Shipment",
        "GBOS Sample Feedback",
        "GBOS Demand Signal",
        "GBOS Sourcing Event",
        "GBOS Work Item",
        "GBOS Review Case",
        "GBOS Informal Observation",
    }
)
_INTEGRATION_DOCTYPES = frozenset({"GBOS External Identity", "GBOS External Crosswalk"})
_ALL_PARENT_DOCTYPES = _BUSINESS_DOCTYPES | _INTEGRATION_DOCTYPES | {"GBOS Team"}
INTERNAL_MATERIALIZER_ROLE = "Agent TrustedMaterializer"
IDENTITY_RESOLVER_ROLE = "Observer Identity Resolver"
INTERNAL_MATERIALIZATION_SUBJECT_DOCTYPES = frozenset(
    {
        "GBOS Demand Signal",
        "GBOS Party Profile",
        "GBOS Product Brief",
        "GBOS Sample Feedback",
        "GBOS Sample Iteration",
        "GBOS Sample Project",
        "GBOS Sample Shipment",
        "GBOS Sourcing Event",
        "GBOS Work Item",
    }
)
INTERNAL_MATERIALIZATION_DRAFT_DOCTYPES = frozenset(
    {
        "GBOS Work Item",
        "GBOS Review Case",
        "GBOS Informal Observation",
    }
)

_SALES_DOCTYPES = frozenset(
    {
        "GBOS Party Profile",
        "GBOS Product Brief",
        "GBOS Sample Project",
        "GBOS Sample Iteration",
        "GBOS Sample Shipment",
        "GBOS Sample Feedback",
        "GBOS Demand Signal",
        "GBOS Work Item",
    }
)
_PRODUCT_DOCTYPES = frozenset(
    {
        "GBOS Party Profile",
        "GBOS Product Brief",
        "GBOS Sample Project",
        "GBOS Sample Iteration",
        "GBOS Sample Shipment",
        "GBOS Sample Feedback",
        "GBOS Demand Signal",
        "GBOS Work Item",
    }
)
_PURCHASE_READ_DOCTYPES = frozenset(
    {
        "GBOS Demand Signal",
        "GBOS Sourcing Event",
        "GBOS Work Item",
    }
)
_PURCHASE_WRITE_DOCTYPES = frozenset(
    {
        "GBOS Sourcing Event",
        "GBOS Work Item",
    }
)
_TEAM_READ = {
    "Sales Manager": _SALES_DOCTYPES | {"GBOS Review Case", "GBOS Team"},
    "Sales User": _SALES_DOCTYPES | {"GBOS Review Case", "GBOS Team"},
    "Purchase Manager": _PURCHASE_READ_DOCTYPES | {"GBOS Review Case", "GBOS Team"},
    "Buyer": _PURCHASE_READ_DOCTYPES | {"GBOS Review Case", "GBOS Team"},
    "Product/R&D": _PRODUCT_DOCTYPES | {"GBOS Review Case", "GBOS Team"},
}
_TEAM_WRITE = {
    "Sales Manager": _SALES_DOCTYPES,
    "Sales User": _SALES_DOCTYPES,
    "Purchase Manager": _PURCHASE_WRITE_DOCTYPES,
    "Buyer": _PURCHASE_WRITE_DOCTYPES,
    "Product/R&D": _PRODUCT_DOCTYPES,
}
_CRM_DOCTYPES = frozenset({"CRM Organization", "Contact", "CRM Lead", "CRM Deal"})
_CRM_TEAM_READ = frozenset({"Sales Manager", "Sales User"})
_CRM_TEAM_WRITE = frozenset({"Sales Manager", "Sales User"})
_PRODUCT_CRM_READ = frozenset({"CRM Organization", "CRM Deal"})


def role_has_doctype_permission(
    role: str,
    doctype: str,
    permission_type: str,
) -> bool:
    """Return the coarse DocPerm capability before record-level scope is applied."""
    if doctype not in _ALL_PARENT_DOCTYPES:
        return False
    if role == IDENTITY_RESOLVER_ROLE:
        return False
    if role == INTERNAL_MATERIALIZER_ROLE:
        if permission_type == "read":
            return doctype in INTERNAL_MATERIALIZATION_SUBJECT_DOCTYPES | {"GBOS Team"}
        if permission_type == "create":
            return doctype in INTERNAL_MATERIALIZATION_DRAFT_DOCTYPES
        if permission_type == "write":
            return doctype in {"GBOS Work Item", "GBOS Review Case"}
        return False
    if role == "GBOS Admin":
        return permission_type in {"read", "write", "create", "delete"}
    if role == "CEO":
        return permission_type == "read" and doctype in (_BUSINESS_DOCTYPES | {"GBOS Team"})
    if role == "Integration Admin":
        return permission_type in {"read", "write", "create"} and doctype in _INTEGRATION_DOCTYPES
    if role == "Reviewer":
        if permission_type == "read":
            return doctype in _BUSINESS_DOCTYPES
        return False
    if permission_type == "read":
        return doctype in _TEAM_READ.get(role, frozenset())
    if permission_type in {"write", "create"}:
        return doctype in _TEAM_WRITE.get(role, frozenset())
    return False


def can_access_record(
    *,
    roles: Collection[str],
    doctype: str,
    permission_type: str,
    is_team_member: bool,
    is_assigned_reviewer: bool = False,
    is_assigned_review_subject: bool = False,
) -> bool:
    assigned_roles = frozenset(roles)

    if doctype not in _ALL_PARENT_DOCTYPES:
        return False
    if (
        "Reviewer" in assigned_roles
        and is_assigned_review_subject
        and permission_type in {"write", "create", "delete"}
    ):
        return False
    if permission_type == "read":
        if "GBOS Admin" in assigned_roles:
            return True
        if "CEO" in assigned_roles and doctype in _BUSINESS_DOCTYPES | {"GBOS Team"}:
            return True
        if "Integration Admin" in assigned_roles and doctype in _INTEGRATION_DOCTYPES:
            return True
        if "Reviewer" in assigned_roles and (
            (doctype == "GBOS Review Case" and is_assigned_reviewer)
            or (doctype in _BUSINESS_DOCTYPES and is_assigned_review_subject)
        ):
            return True
        return is_team_member and any(
            doctype in _TEAM_READ.get(role, frozenset()) for role in assigned_roles
        )
    if permission_type == "approve":
        return (
            doctype == "GBOS Review Case" and is_assigned_reviewer and "Reviewer" in assigned_roles
        )
    if permission_type == "write" and doctype == "GBOS Review Case":
        return "GBOS Admin" in assigned_roles
    if permission_type in {"write", "create"}:
        if "GBOS Admin" in assigned_roles:
            return True
        if "Integration Admin" in assigned_roles and doctype in _INTEGRATION_DOCTYPES:
            return True
        return is_team_member and any(
            doctype in _TEAM_WRITE.get(role, frozenset()) for role in assigned_roles
        )
    if permission_type == "delete":
        return "GBOS Admin" in assigned_roles
    return False


def can_access_crm_record(
    *,
    roles: Collection[str],
    doctype: str,
    permission_type: str,
    is_team_member: bool,
) -> bool:
    if doctype not in _CRM_DOCTYPES:
        return False
    assigned_roles = frozenset(roles)
    if permission_type == "read":
        return bool(
            assigned_roles & {"GBOS Admin", "CEO"}
            or (is_team_member and assigned_roles & _CRM_TEAM_READ)
            or (is_team_member and "Product/R&D" in assigned_roles and doctype in _PRODUCT_CRM_READ)
        )
    if permission_type in {"write", "create"}:
        return bool(
            "GBOS Admin" in assigned_roles or (is_team_member and assigned_roles & _CRM_TEAM_WRITE)
        )
    if permission_type == "delete":
        return "GBOS Admin" in assigned_roles
    return False


def role_has_crm_doctype_permission(
    role: str,
    doctype: str,
    permission_type: str,
) -> bool:
    """Return the coarse CRM DocPerm capability before team scope is applied."""
    if doctype not in _CRM_DOCTYPES:
        return False
    if role == "GBOS Admin":
        return permission_type in {"read", "write", "create", "delete"}
    if role == "CEO":
        return permission_type == "read"
    if role in _CRM_TEAM_WRITE:
        return permission_type in {"read", "write", "create"}
    return role == "Product/R&D" and permission_type == "read" and doctype in _PRODUCT_CRM_READ
