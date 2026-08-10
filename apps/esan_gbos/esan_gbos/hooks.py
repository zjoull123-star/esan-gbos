app_name = "esan_gbos"
app_title = "ESAN GBOS"
app_publisher = "ESAN"
app_description = "Governed front-office workflows for ESAN GBOS"
app_email = "engineering@example.invalid"
app_license = "AGPL-3.0"

required_apps = ["erpnext", "crm"]
after_install = "esan_gbos.install.after_install"
after_migrate = "esan_gbos.install.after_migrate"
after_request = [
    "esan_gbos.api.v1.http.normalize_bff_pre_dispatch_error",
    "esan_gbos.security.add_gbos_pwa_security_headers",
]

website_route_rules = [
    {"from_route": "/gbos/<path:app_path>", "to_route": "gbos"},
]

GBOS_ROLES = [
    "GBOS Admin",
    "Integration Admin",
    "Privacy/Audit",
    "CEO",
    "Sales Manager",
    "Sales User",
    "Purchase Manager",
    "Buyer",
    "Product/R&D",
    "Reviewer",
    "Finance Readonly",
    "Agent TrustedMaterializer",
    "Observer Identity Resolver",
]

fixtures = [
    {"dt": "Role", "filters": [["name", "in", GBOS_ROLES]]},
    {"dt": "Custom Field", "filters": [["fieldname", "like", "custom_esan_%"]]},
]

_PARENT_DOCTYPES = [
    "GBOS Party Profile",
    "GBOS External Identity",
    "GBOS External Crosswalk",
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
]

permission_query_conditions = {
    "GBOS Team": "esan_gbos.permissions.team_permission_query",
    **{
        doctype: "esan_gbos.permissions.team_scoped_permission_query"
        for doctype in _PARENT_DOCTYPES
        if doctype
        not in {
            "GBOS External Identity",
            "GBOS External Crosswalk",
            "GBOS Review Case",
            "GBOS Work Item",
        }
    },
    "GBOS External Identity": "esan_gbos.permissions.integration_permission_query",
    "GBOS External Crosswalk": "esan_gbos.permissions.integration_permission_query",
    "GBOS Review Case": "esan_gbos.permissions.review_case_permission_query",
    "GBOS Work Item": "esan_gbos.permissions.work_item_permission_query",
    "GBOS Informal Observation": ("esan_gbos.permissions.informal_observation_permission_query"),
    "CRM Organization": "esan_gbos.permissions.crm_organization_permission_query",
    "CRM Lead": "esan_gbos.permissions.crm_lead_permission_query",
    "CRM Deal": "esan_gbos.permissions.crm_deal_permission_query",
    "Contact": "esan_gbos.permissions.contact_permission_query",
}
permission_query_conditions["Integration Request"] = (
    "esan_gbos.permissions.integration_request_permission_query"
)

has_permission = {
    doctype: "esan_gbos.permissions.has_gbos_permission"
    for doctype in ["GBOS Team", *_PARENT_DOCTYPES]
}
has_permission.update(
    {
        doctype: "esan_gbos.permissions.has_crm_permission"
        for doctype in ("CRM Organization", "CRM Lead", "CRM Deal", "Contact")
    }
)
has_permission["Integration Request"] = (
    "esan_gbos.permissions.has_internal_materialization_permission"
)

_BLOCKED_ERP_DOCTYPES = [
    "Sales Order",
    "Purchase Order",
    "Delivery Note",
    "Purchase Receipt",
    "Sales Invoice",
    "Purchase Invoice",
    "Payment Entry",
    "Material Request",
    "Pick List",
    "POS Invoice",
    "Stock Entry",
    "Stock Reconciliation",
    "Stock Ledger Entry",
    "Journal Entry",
    "GL Entry",
    "Work Order",
    "Job Card",
]

doc_events = {
    doctype: {
        "before_insert": "esan_gbos.erpnext_guard.reject_v1_transaction_creation",
    }
    for doctype in _BLOCKED_ERP_DOCTYPES
}

doc_events["User"] = {
    "before_validate": "esan_gbos.ceo_access.ensure_ceo_full_access",
}
doc_events["User"]["on_update"] = (
    "esan_gbos.gbos.doctype.gbos_external_identity.gbos_external_identity."
    "deny_ineligible_user_mappings"
)
doc_events["User"]["on_trash"] = (
    "esan_gbos.gbos.doctype.gbos_external_identity.gbos_external_identity."
    "deny_ineligible_user_mappings"
)
doc_events["GBOS Team Member"] = {
    "on_update": (
        "esan_gbos.gbos.doctype.gbos_external_identity.gbos_external_identity."
        "deny_ineligible_team_member_mappings"
    ),
    "on_trash": (
        "esan_gbos.gbos.doctype.gbos_external_identity.gbos_external_identity."
        "deny_ineligible_team_member_mappings"
    ),
}
doc_events["GBOS Team"] = {
    "on_update": (
        "esan_gbos.gbos.doctype.gbos_external_identity.gbos_external_identity."
        "deny_removed_team_member_mappings"
    ),
    "on_trash": (
        "esan_gbos.gbos.doctype.gbos_external_identity.gbos_external_identity."
        "deny_removed_team_member_mappings"
    ),
}
doc_events["GBOS Party Profile"] = {
    "on_update": (
        "esan_gbos.gbos.doctype.gbos_external_identity.gbos_external_identity."
        "deny_ineligible_party_mappings"
    ),
    "on_trash": (
        "esan_gbos.gbos.doctype.gbos_external_identity.gbos_external_identity."
        "deny_ineligible_party_mappings"
    ),
}

for _draft_doctype in (
    "GBOS Work Item",
    "GBOS Review Case",
    "GBOS Informal Observation",
):
    doc_events.setdefault(_draft_doctype, {})["validate"] = (
        "esan_gbos.permissions.protect_ai_draft_command"
    )

has_permission.update(
    {
        doctype: "esan_gbos.erpnext_guard.has_v1_transaction_permission"
        for doctype in _BLOCKED_ERP_DOCTYPES
    }
)
