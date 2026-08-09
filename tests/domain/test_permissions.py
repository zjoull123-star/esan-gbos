from __future__ import annotations

import pytest
from esan_gbos.domain.permissions import (
    PermissionScopeError,
    can_access_crm_record,
    can_access_record,
    communication_scope,
    role_has_doctype_permission,
)


def test_team_member_can_read_team_record() -> None:
    assert can_access_record(
        roles={"Sales User"},
        doctype="GBOS Party Profile",
        permission_type="read",
        is_team_member=True,
    )


def test_non_member_cannot_read_team_record() -> None:
    assert not can_access_record(
        roles={"Sales User"},
        doctype="GBOS Party Profile",
        permission_type="read",
        is_team_member=False,
    )


def test_sales_user_cannot_approve() -> None:
    assert not can_access_record(
        roles={"Sales User"},
        doctype="GBOS Review Case",
        permission_type="approve",
        is_team_member=True,
    )


def test_assigned_reviewer_can_approve_review_case() -> None:
    assert can_access_record(
        roles={"Reviewer"},
        doctype="GBOS Review Case",
        permission_type="approve",
        is_team_member=False,
        is_assigned_reviewer=True,
    )


def test_reviewer_cannot_read_an_unassigned_team_case() -> None:
    assert not can_access_record(
        roles={"Reviewer"},
        doctype="GBOS Review Case",
        permission_type="read",
        is_team_member=True,
        is_assigned_reviewer=False,
    )


def test_reviewer_can_read_only_an_assigned_review_subject() -> None:
    assert can_access_record(
        roles={"Reviewer"},
        doctype="GBOS Work Item",
        permission_type="read",
        is_team_member=False,
        is_assigned_review_subject=True,
    )
    assert not can_access_record(
        roles={"Reviewer"},
        doctype="GBOS Work Item",
        permission_type="write",
        is_team_member=False,
        is_assigned_review_subject=True,
    )


def test_auditor_does_not_have_blanket_business_record_access() -> None:
    assert not can_access_record(
        roles={"Privacy/Audit"},
        doctype="GBOS Party Profile",
        permission_type="read",
        is_team_member=False,
    )
    assert not can_access_record(
        roles={"Privacy/Audit"},
        doctype="GBOS Work Item",
        permission_type="write",
        is_team_member=True,
    )


def test_administrator_role_is_not_a_daily_access_bypass() -> None:
    assert not can_access_record(
        roles={"Administrator"},
        doctype="GBOS Party Profile",
        permission_type="read",
        is_team_member=False,
    )


def test_internal_materializer_coarse_permissions_are_closed_and_non_exporting() -> None:
    role = "Agent TrustedMaterializer"
    allowed_subjects = {
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
    for doctype in allowed_subjects | {"GBOS Team"}:
        assert role_has_doctype_permission(role, doctype, "read")
    for doctype in {"GBOS Work Item", "GBOS Review Case", "GBOS Informal Observation"}:
        assert role_has_doctype_permission(role, doctype, "create")
    for doctype in {"GBOS Work Item", "GBOS Review Case"}:
        assert role_has_doctype_permission(role, doctype, "write")
    assert not role_has_doctype_permission(
        role,
        "GBOS Informal Observation",
        "write",
    )
    for doctype in allowed_subjects | {
        "GBOS Team",
        "GBOS Review Case",
        "GBOS Informal Observation",
    }:
        for forbidden in ("delete", "export", "email", "share"):
            assert not role_has_doctype_permission(role, doctype, forbidden)


def test_buyer_is_limited_to_procurement_and_authorized_demand_summary() -> None:
    assert can_access_record(
        roles={"Buyer"},
        doctype="GBOS Demand Signal",
        permission_type="read",
        is_team_member=True,
    )
    assert not can_access_record(
        roles={"Buyer"},
        doctype="GBOS Demand Signal",
        permission_type="write",
        is_team_member=True,
    )
    assert can_access_record(
        roles={"Buyer"},
        doctype="GBOS Sourcing Event",
        permission_type="write",
        is_team_member=True,
    )
    for doctype in (
        "GBOS Party Profile",
        "GBOS Product Brief",
        "GBOS Sample Project",
        "GBOS Sample Feedback",
    ):
        assert not can_access_record(
            roles={"Buyer"},
            doctype=doctype,
            permission_type="read",
            is_team_member=True,
        )


def test_sales_user_cannot_read_or_write_procurement_records() -> None:
    for permission_type in ("read", "write", "create"):
        assert not can_access_record(
            roles={"Sales User"},
            doctype="GBOS Sourcing Event",
            permission_type=permission_type,
            is_team_member=True,
        )


def test_product_role_can_work_on_product_and_sample_but_not_sourcing() -> None:
    for doctype in ("GBOS Product Brief", "GBOS Sample Project", "GBOS Sample Feedback"):
        assert can_access_record(
            roles={"Product/R&D"},
            doctype=doctype,
            permission_type="write",
            is_team_member=True,
        )
    assert not can_access_record(
        roles={"Product/R&D"},
        doctype="GBOS Sourcing Event",
        permission_type="read",
        is_team_member=True,
    )


def test_integration_admin_is_limited_to_external_mapping_records() -> None:
    for doctype in ("GBOS External Identity", "GBOS External Crosswalk"):
        assert can_access_record(
            roles={"Integration Admin"},
            doctype=doctype,
            permission_type="write",
            is_team_member=False,
        )
    assert not can_access_record(
        roles={"Integration Admin"},
        doctype="GBOS Party Profile",
        permission_type="read",
        is_team_member=False,
    )


def test_sales_and_product_crm_access_stays_team_scoped() -> None:
    assert can_access_crm_record(
        roles={"Sales User"},
        doctype="CRM Organization",
        permission_type="write",
        is_team_member=True,
    )
    assert not can_access_crm_record(
        roles={"Sales User"},
        doctype="CRM Organization",
        permission_type="read",
        is_team_member=False,
    )
    assert can_access_crm_record(
        roles={"Product/R&D"},
        doctype="CRM Deal",
        permission_type="read",
        is_team_member=True,
    )
    assert not can_access_crm_record(
        roles={"Product/R&D"},
        doctype="Contact",
        permission_type="read",
        is_team_member=True,
    )
    assert not can_access_crm_record(
        roles={"Product/R&D"},
        doctype="CRM Deal",
        permission_type="write",
        is_team_member=True,
    )


def test_ceo_can_read_but_not_write_crm_records() -> None:
    assert can_access_crm_record(
        roles={"CEO"},
        doctype="Contact",
        permission_type="read",
        is_team_member=False,
    )
    assert not can_access_crm_record(
        roles={"CEO"},
        doctype="Contact",
        permission_type="write",
        is_team_member=False,
    )


def test_informal_observation_is_global_for_ceo_but_reviewer_is_assignment_scoped() -> None:
    assert can_access_record(
        roles={"CEO"},
        doctype="GBOS Informal Observation",
        permission_type="read",
        is_team_member=False,
    )
    assert can_access_record(
        roles={"Reviewer"},
        doctype="GBOS Informal Observation",
        permission_type="read",
        is_team_member=False,
        is_assigned_review_subject=True,
    )
    assert not can_access_record(
        roles={"Reviewer"},
        doctype="GBOS Informal Observation",
        permission_type="read",
        is_team_member=True,
        is_assigned_review_subject=False,
    )


def test_sales_communication_scope_passes_only_resolved_teams_and_self() -> None:
    assert communication_scope(
        roles={"Sales User"},
        actor_ref="sales-a@example.invalid",
        team_refs={"TEM-B", "TEM-A"},
    ) == {
        "actor_ref": "sales-a@example.invalid",
        "allowed_team_refs": ["TEM-A", "TEM-B"],
        "scope": "team_and_self",
        "include_raw": False,
    }

    with pytest.raises(PermissionScopeError, match="team"):
        communication_scope(
            roles={"Sales User"},
            actor_ref="sales-a@example.invalid",
            team_refs=set(),
        )


def test_ceo_communication_scope_is_business_projection_but_raw_defaults_off() -> None:
    assert communication_scope(
        roles={"CEO"},
        actor_ref="ceo@example.invalid",
        team_refs=set(),
    ) == {
        "actor_ref": "ceo@example.invalid",
        "allowed_team_refs": ["*"],
        "scope": "all_business_projection",
        "include_raw": False,
    }
