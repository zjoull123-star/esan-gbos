from __future__ import annotations

import json
from pathlib import Path

CONTRACT = Path(__file__).parents[2] / "contracts" / "bff-v1.openapi.json"

OPERATIONS = {
    "/api/method/esan_gbos.api.v1.party.get_360": "get",
    "/api/method/esan_gbos.api.v1.work_item.list": "get",
    "/api/method/esan_gbos.api.v1.sample.get_status": "get",
    "/api/method/esan_gbos.api.v1.sourcing.get_board": "get",
    "/api/method/esan_gbos.api.v1.sample.create_project": "post",
    "/api/method/esan_gbos.api.v1.sample.record_feedback": "post",
    "/api/method/esan_gbos.api.v1.sourcing.create_from_demand": "post",
    "/api/method/esan_gbos.api.v1.work_item.transition": "post",
}


def _contract() -> dict[str, object]:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def test_bff_contract_has_only_the_frozen_versioned_surface() -> None:
    contract = _contract()

    assert contract["openapi"] == "3.1.0"
    assert set(contract["paths"]) == set(OPERATIONS)
    for path, method in OPERATIONS.items():
        operations = contract["paths"][path]
        assert method in operations
        assert {key for key in operations if key in {"get", "post", "put", "patch", "delete"}} == {
            method
        }


def test_bff_contract_requires_session_and_csrf_for_commands() -> None:
    contract = _contract()
    schemes = contract["components"]["securitySchemes"]

    assert schemes["FrappeSession"] == {
        "type": "apiKey",
        "in": "cookie",
        "name": "sid",
    }
    assert schemes["FrappeCsrf"]["in"] == "header"
    assert schemes["FrappeCsrf"]["name"] == "X-Frappe-CSRF-Token"
    for path, method in OPERATIONS.items():
        operation = contract["paths"][path][method]
        required = operation["security"]
        assert any("FrappeSession" in item for item in required)
        if method == "post":
            assert any({"FrappeSession", "FrappeCsrf"} <= set(item) for item in required)


def test_bff_error_envelope_contains_request_id_and_conflict_codes() -> None:
    contract = _contract()
    error = contract["components"]["schemas"]["ErrorEnvelope"]
    error_body = error["properties"]["error"]

    assert "request_id" in error_body["required"]
    assert {"idempotency_conflict", "revision_conflict", "invalid_transition"} <= set(
        error_body["properties"]["code"]["enum"]
    )
    for path, method in OPERATIONS.items():
        responses = contract["paths"][path][method]["responses"]
        assert "200" in responses
        assert "default" in responses


def test_bff_envelopes_freeze_schema_version_and_error_shape() -> None:
    contract = _contract()
    schemas = contract["components"]["schemas"]
    success_meta = schemas["SuccessEnvelope"]["properties"]["meta"]
    error = schemas["ErrorEnvelope"]

    assert {"request_id", "schema_version"} <= set(success_meta["required"])
    assert success_meta["properties"]["schema_version"]["const"] == "1.0"
    assert set(error["required"]) == {"error"}
    assert set(error["properties"]) == {"error"}


def test_bff_contract_never_exposes_generic_doctype_writers() -> None:
    text = CONTRACT.read_text(encoding="utf-8")

    assert "frappe.client.insert" not in text
    assert "frappe.client.set_value" not in text
    assert "kingdee" not in text.lower()
