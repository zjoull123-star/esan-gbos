from __future__ import annotations

import pytest
from esan_gbos.domain import access_policy


def _policy_contracts():
    assert hasattr(access_policy, "review_case_scope_filters")
    assert hasattr(access_policy, "can_access_review_case")
    return access_policy.review_case_scope_filters, access_policy.can_access_review_case


def test_reviewer_scope_remains_bound_to_the_assigned_user() -> None:
    review_case_scope_filters, can_access_review_case = _policy_contracts()
    assert review_case_scope_filters(
        roles={"Reviewer"},
        actor_ref="reviewer@example.invalid",
    ) == {"assigned_reviewer": "reviewer@example.invalid"}
    assert can_access_review_case(
        roles={"Reviewer"},
        actor_ref="reviewer@example.invalid",
        assigned_reviewer="reviewer@example.invalid",
    )
    assert not can_access_review_case(
        roles={"Reviewer"},
        actor_ref="reviewer@example.invalid",
        assigned_reviewer="other@example.invalid",
    )


def test_gbos_admin_can_read_and_decide_every_review_case() -> None:
    review_case_scope_filters, can_access_review_case = _policy_contracts()
    assert (
        review_case_scope_filters(
            roles={"GBOS Admin"},
            actor_ref="ceo@example.invalid",
        )
        == {}
    )
    assert can_access_review_case(
        roles={"GBOS Admin"},
        actor_ref="ceo@example.invalid",
        assigned_reviewer="other@example.invalid",
    )


def test_review_scope_rejects_unprivileged_or_missing_actors() -> None:
    review_case_scope_filters, _ = _policy_contracts()
    with pytest.raises(access_policy.PermissionScopeError, match="actor"):
        review_case_scope_filters(roles={"GBOS Admin"}, actor_ref="")
    with pytest.raises(access_policy.PermissionScopeError, match="role"):
        review_case_scope_filters(
            roles={"Sales User"},
            actor_ref="sales@example.invalid",
        )
