from __future__ import annotations

from collections.abc import Collection

from esan_gbos.domain.permissions import PermissionScopeError

PARTY_360_ROLES = frozenset(
    {
        "GBOS Admin",
        "CEO",
        "Sales Manager",
        "Sales User",
    }
)

WORK_READ_ROLES = frozenset(
    {
        "GBOS Admin",
        "CEO",
        "Sales Manager",
        "Sales User",
        "Purchase Manager",
        "Buyer",
        "Product/R&D",
        "Reviewer",
    }
)

SAMPLE_READ_ROLES = frozenset(
    {
        "GBOS Admin",
        "CEO",
        "Sales Manager",
        "Sales User",
        "Product/R&D",
        "Reviewer",
    }
)

SOURCING_READ_ROLES = frozenset(
    {
        "GBOS Admin",
        "CEO",
        "Purchase Manager",
        "Buyer",
    }
)

REVIEW_CASE_ROLES = frozenset({"Reviewer", "GBOS Admin"})


def review_case_scope_filters(
    *,
    roles: Collection[str],
    actor_ref: str,
) -> dict[str, str]:
    actor = actor_ref.strip()
    if not actor:
        raise PermissionScopeError("actor reference is required")
    assigned_roles = frozenset(roles)
    if "GBOS Admin" in assigned_roles:
        return {}
    if "Reviewer" in assigned_roles:
        return {"assigned_reviewer": actor}
    raise PermissionScopeError("a review management role is required")


def can_access_review_case(
    *,
    roles: Collection[str],
    actor_ref: str,
    assigned_reviewer: str,
) -> bool:
    actor = actor_ref.strip()
    if not actor:
        return False
    assigned_roles = frozenset(roles)
    return "GBOS Admin" in assigned_roles or (
        "Reviewer" in assigned_roles and assigned_reviewer == actor
    )
