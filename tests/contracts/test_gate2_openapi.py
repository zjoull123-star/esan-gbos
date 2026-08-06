from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONTRACT = Path(__file__).parents[2] / "contracts" / "gate2" / "services-v1.openapi.json"

EXPECTED_PATHS = {
    "/v1/agent/tasks",
    "/v1/agent/tasks/{task_id}/timeline",
    "/v1/context/evidence/{evidence_record_id}",
    "/v1/context/facts/{fact_id}",
    "/v1/decisions/{decision_id}",
    "/v1/actions/proposals",
    "/v1/actions/{action_proposal_id}",
    "/v1/metrics/{metric_key}",
    "/v1/kingdee/read-projections/query",
}

SCHEMA_REFS = {
    "AgentTask": "../agent-task.schema.json",
    "AgentTimelineEvent": "../agent-timeline-event.schema.json",
    "EvidenceRecord": "../evidence-record.schema.json",
    "VerifiedBusinessFact": "../verified-business-fact.schema.json",
    "DecisionRecord": "../decision-record.schema.json",
    "ActionProposal": "../action-proposal.schema.json",
    "ActionChain": "#/components/schemas/ActionChain",
    "MetricResponse": "../metric-response.schema.json",
    "KingdeeReadProjection": "../kingdee-read-projection.schema.json",
}

OPERATION_SCOPES = {
    "createAgentTask": "gbos-propose",
    "listAgentTaskTimeline": "gbos-read",
    "getEvidenceRecord": "gbos-read",
    "getVerifiedBusinessFact": "gbos-read",
    "getDecisionRecord": "gbos-read",
    "createActionProposal": "gbos-propose",
    "getActionChain": "gbos-read",
    "getMetric": "metrics-read",
    "queryKingdeeReadProjection": "kingdee-read",
}

PROCESSING_PURPOSES = {
    "business_operations",
    "observation_processing",
    "entity_resolution",
    "customer_service",
    "sales_follow_up",
    "procurement_coordination",
    "product_sample_management",
    "risk_review",
    "metric_reporting",
    "audit_compliance",
}


def _contract() -> dict[str, Any]:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def _operations(contract: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        operation
        for path_item in contract["paths"].values()
        for method, operation in path_item.items()
        if method in {"get", "post", "put", "patch", "delete"}
    ]


def test_gate2_openapi_exposes_only_versioned_typed_service_endpoints() -> None:
    contract = _contract()

    assert contract["openapi"] == "3.1.0"
    assert set(contract["paths"]) == EXPECTED_PATHS
    assert all(path.startswith("/v1/") for path in contract["paths"])
    assert {operation["operationId"] for operation in _operations(contract)} == {
        "createAgentTask",
        "listAgentTaskTimeline",
        "getEvidenceRecord",
        "getVerifiedBusinessFact",
        "getDecisionRecord",
        "createActionProposal",
        "getActionChain",
        "getMetric",
        "queryKingdeeReadProjection",
    }
    for operation in _operations(contract):
        assert operation["tags"]
        assert operation["responses"]["200"]["content"]["application/json"]["schema"][
            "$ref"
        ].startswith("#/components/schemas/")


def test_gate2_openapi_components_point_to_frozen_contracts() -> None:
    schemas = _contract()["components"]["schemas"]

    for name, ref in SCHEMA_REFS.items():
        if name == "ActionChain":
            assert set(schemas[name]["properties"]) == {
                "proposal",
                "approval",
                "execution",
                "verification",
            }
            continue
        assert schemas[name] == {"$ref": ref}


def test_gate2_openapi_commands_have_typed_request_bodies() -> None:
    contract = _contract()
    for path, path_item in contract["paths"].items():
        if "post" not in path_item:
            continue
        request = path_item["post"]["requestBody"]
        assert request["required"] is True
        schema = request["content"]["application/json"]["schema"]
        assert "$ref" in schema
        assert schema["$ref"].startswith("#/components/schemas/")
        assert path in {
            "/v1/agent/tasks",
            "/v1/actions/proposals",
            "/v1/kingdee/read-projections/query",
        }


def test_gate2_openapi_has_no_generic_or_mutating_external_surface() -> None:
    contract = _contract()
    text = CONTRACT.read_text(encoding="utf-8").lower()

    assert all(
        method not in {"put", "patch", "delete"}
        for path_item in contract["paths"].values()
        for method in path_item
    )
    for forbidden in (
        "arbitrary_sql",
        "arbitrary-doctype",
        "frappe.client",
        "direct_database_write",
        "kingdee_write",
        "kingdee_delete",
        "kingdee_submit",
        "kingdee_audit",
        "kingdee_unaudit",
        "raw_form_id",
    ):
        assert forbidden not in text


def test_gate2_openapi_declares_design_only_zero_runtime_capability() -> None:
    contract = _contract()

    assert contract["x-gbos-gate"] == 2
    assert contract["x-runtime-enabled"] is False
    assert contract["x-network-allowed"] is False
    assert contract["x-real-credentials-allowed"] is False
    assert contract["x-production-capability"] is False


def test_gate2_openapi_declares_design_only_security_and_minimal_operation_scopes() -> None:
    contract = _contract()
    security_design = contract["x-gbos-security-design"]
    oauth2 = contract["components"]["securitySchemes"]["OAuth2"]

    assert security_design == {
        "status": "design_only",
        "runtime_enforcement_enabled": False,
        "per_operation_scope_required": True,
        "site_header_required": True,
        "request_id_header_required": True,
        "purpose_header_required": True,
        "required_audience": "gbos-services",
        "resource_binding_required": True,
        "token_passthrough_allowed": False,
        "explicit_user_authorization_required": True,
    }
    assert oauth2["type"] == "oauth2"
    scopes = oauth2["flows"]["authorizationCode"]["scopes"]
    assert set(scopes) == {"gbos-read", "gbos-propose", "metrics-read", "kingdee-read"}
    for operation in _operations(contract):
        assert operation["security"] == [{"OAuth2": [OPERATION_SCOPES[operation["operationId"]]]}]


def test_every_gate2_path_requires_site_request_and_purpose_headers() -> None:
    contract = _contract()
    required_refs = {
        "#/components/parameters/SiteIdHeader",
        "#/components/parameters/RequestIdHeader",
        "#/components/parameters/ProcessingPurposeHeader",
    }

    for path_item in contract["paths"].values():
        assert {parameter["$ref"] for parameter in path_item["parameters"]} == required_refs

    parameters = contract["components"]["parameters"]
    assert parameters["SiteIdHeader"]["name"] == "X-GBOS-Site-ID"
    assert parameters["RequestIdHeader"]["name"] == "X-Request-ID"
    assert parameters["ProcessingPurposeHeader"]["name"] == "X-GBOS-Purpose"
    for parameter in (
        parameters["SiteIdHeader"],
        parameters["RequestIdHeader"],
        parameters["ProcessingPurposeHeader"],
    ):
        assert parameter["in"] == "header"
        assert parameter["required"] is True
    assert set(parameters["ProcessingPurposeHeader"]["schema"]["enum"]) == PROCESSING_PURPOSES


def test_kingdee_read_query_matches_exact_adapter_dispatch_envelope() -> None:
    query = _contract()["components"]["schemas"]["KingdeeReadQuery"]
    expected_fields = {
        "tool_name",
        "request_id",
        "site_id",
        "account_set_ref",
        "limit",
        "offset",
    }

    assert query["additionalProperties"] is False
    assert set(query["required"]) == expected_fields
    assert set(query["properties"]) == expected_fields
    assert set(query["properties"]["tool_name"]["enum"]) == {
        "kingdee.material.get",
        "kingdee.customer.get",
        "kingdee.supplier.get",
        "kingdee.sales_order.get",
        "kingdee.purchase_order.get",
        "kingdee.inventory.get",
        "kingdee.receivable.get",
    }
    assert query["properties"]["limit"]["minimum"] == 1
    assert query["properties"]["limit"]["maximum"] == 50
    assert query["properties"]["offset"]["minimum"] == 0
    assert "fields" not in query["properties"]
    assert "object_type" not in query["properties"]
