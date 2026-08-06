from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parents[2]
CONTRACT = ROOT / "contracts" / "bff-v2.openapi.json"

OPERATIONS = {
    "/api/method/esan_gbos.api.v2.review_case.list": "get",
    "/api/method/esan_gbos.api.v2.review_case.get": "get",
    "/api/method/esan_gbos.api.v2.review_case.decide": "post",
}

DECIDE_REQUIRED = {
    "name",
    "decision",
    "decision_note",
    "expected_revision",
    "expected_subject_revision",
    "idempotency_key",
    "subject_payload_sha256",
    "evidence_refs",
    "policy_version",
}


def contract() -> dict[str, object]:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def test_v2_surface_contains_only_review_list_get_and_decide() -> None:
    value = contract()

    assert value["openapi"] == "3.1.0"
    assert value["info"]["version"] == "2.0.0"
    assert set(value["paths"]) == set(OPERATIONS)
    for path, method in OPERATIONS.items():
        operations = value["paths"][path]
        assert {key for key in operations if key in {"get", "post", "put", "patch", "delete"}} == {
            method
        }


def test_v2_uses_session_for_reads_and_session_plus_csrf_for_decide() -> None:
    value = contract()
    schemes = value["components"]["securitySchemes"]

    assert schemes["FrappeSession"] == {"type": "apiKey", "in": "cookie", "name": "sid"}
    assert schemes["FrappeCsrf"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-Frappe-CSRF-Token",
    }
    for path, method in OPERATIONS.items():
        security = value["paths"][path][method]["security"]
        assert any("FrappeSession" in entry for entry in security)
        if method == "post":
            assert any({"FrappeSession", "FrappeCsrf"} <= set(entry) for entry in security)


def test_decide_wire_requires_double_revision_hash_evidence_policy_and_idempotency() -> None:
    value = contract()
    body = value["paths"]["/api/method/esan_gbos.api.v2.review_case.decide"]["post"]["requestBody"][
        "content"
    ]["application/x-www-form-urlencoded"]["schema"]

    assert body["additionalProperties"] is False
    assert set(body["required"]) == DECIDE_REQUIRED
    assert body["properties"]["decision"]["enum"] == ["Approved", "Rejected"]
    assert body["properties"]["decision_note"]["minLength"] >= 4
    assert body["properties"]["expected_revision"]["minimum"] >= 0
    assert body["properties"]["expected_subject_revision"]["minimum"] >= 0
    assert body["properties"]["subject_payload_sha256"]["pattern"] == "^[a-f0-9]{64}$"
    assert body["properties"]["evidence_refs"]["description"].startswith("JSON array")
    assert "expected_case_payload_hash" in body["properties"]


def test_responses_use_frappe_wire_envelopes_and_frozen_snapshot_shape() -> None:
    value = contract()
    schemas = value["components"]["schemas"]

    for path, method in OPERATIONS.items():
        responses = value["paths"][path][method]["responses"]
        assert "200" in responses
        assert "default" in responses
    assert schemas["SuccessMeta"]["properties"]["schema_version"]["const"] == "1.0"
    assert set(schemas["ReviewSubject"]["required"]) == {
        "doctype",
        "name",
        "revision",
        "payload_hash",
        "snapshot",
    }
    assert schemas["ReviewCase"]["properties"]["subject"]["$ref"].endswith("/ReviewSubject")
    assert schemas["ReviewCase"]["properties"]["evidence"]["items"]["$ref"].endswith(
        "/ReviewEvidence"
    )
    assert schemas["ReviewDecision"]["properties"]["decision"]["enum"] == [
        "Approved",
        "Rejected",
    ]


def test_v2_exposes_no_subject_writer_generic_writer_or_kingdee_surface() -> None:
    text = CONTRACT.read_text(encoding="utf-8").lower()

    for forbidden in (
        "frappe.client.insert",
        "frappe.client.set_value",
        "subject.update",
        "subject.write",
        "kingdee",
        "approvedcommand",
    ):
        assert forbidden not in text
