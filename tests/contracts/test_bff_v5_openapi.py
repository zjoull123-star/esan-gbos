from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parents[2]
CONTRACT = ROOT / "contracts" / "bff-v5.openapi.json"

OPERATIONS = {
    "/api/method/esan_gbos.api.v5.email_admin.list": "get",
    "/api/method/esan_gbos.api.v5.email_admin.get": "get",
    "/api/method/esan_gbos.api.v5.email_admin.upsert": "post",
    "/api/method/esan_gbos.api.v5.email_admin.set_status": "post",
    "/api/method/esan_gbos.api.v5.email_admin.get_connector_health": "get",
    "/api/method/esan_gbos.api.v5.email_inbox.list": "get",
    "/api/method/esan_gbos.api.v5.email_inbox.get": "get",
}


def contract() -> dict[str, object]:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def resolve_schema(value: dict[str, object], schema: dict[str, object]) -> dict[str, object]:
    reference = schema.get("$ref")
    if not isinstance(reference, str):
        return schema
    assert reference.startswith("#/components/schemas/")
    return value["components"]["schemas"][reference.rsplit("/", maxsplit=1)[-1]]


def test_v5_surface_is_exactly_the_phase_one_email_api_and_never_cached() -> None:
    value = contract()

    assert value["openapi"] == "3.1.0"
    assert value["info"]["version"] == "5.0.0"
    assert set(value["paths"]) == set(OPERATIONS)
    for path, method in OPERATIONS.items():
        operations = value["paths"][path]
        actual = {key for key in operations if key in {"get", "post", "put", "patch", "delete"}}
        assert actual == {method}
        assert operations[method]["x-gbos-cache"] == "no-store"


def test_v5_freezes_config_only_and_business_inbox_role_boundaries() -> None:
    value = contract()
    for path, method in OPERATIONS.items():
        roles = value["paths"][path][method]["x-gbos-roles"]
        if ".email_admin." in path:
            assert roles == ["Integration Admin", "GBOS Admin"]
        else:
            assert roles == [
                "CEO",
                "Sales Manager",
                "Sales User",
                "Reviewer",
                "GBOS Admin",
            ]


def test_v5_reads_require_session_and_writes_require_csrf_revision_and_idempotency() -> None:
    value = contract()
    schemes = value["components"]["securitySchemes"]

    assert schemes["FrappeSession"] == {"type": "apiKey", "in": "cookie", "name": "sid"}
    assert schemes["FrappeCsrf"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-Frappe-CSRF-Token",
    }
    for path, method in OPERATIONS.items():
        operation = value["paths"][path][method]
        assert any("FrappeSession" in entry for entry in operation["security"])
        if method == "post":
            assert any(
                {"FrappeSession", "FrappeCsrf"} <= set(entry) for entry in operation["security"]
            )
            request_schema = operation["requestBody"]["content"][
                "application/x-www-form-urlencoded"
            ]["schema"]
            body = resolve_schema(value, request_schema)
            assert body["additionalProperties"] is False
            assert {"expected_revision", "idempotency_key"} <= set(body["required"])


def test_v5_closed_shapes_expose_only_safe_mailbox_inbox_and_health_projections() -> None:
    schemas = contract()["components"]["schemas"]

    mailbox = schemas["Mailbox"]
    assert mailbox["additionalProperties"] is False
    assert mailbox["properties"]["provider_kind"]["enum"] == [
        "fake",
        "imap_smtp",
        "wecom_app_mail",
    ]
    assert mailbox["properties"]["business_mode"]["enum"] == [
        "primary",
        "selective_archive",
        "migration",
    ]
    assert mailbox["properties"]["status"]["enum"] == [
        "draft",
        "active",
        "paused",
        "revoked",
        "error",
    ]
    assert mailbox["properties"]["outbound_enabled"]["const"] is False
    command = schemas["MailboxUpsertCommand"]
    assert {
        "provider_account_ref",
        "observer_connector_instance_ref",
        "default_team_ref",
        "account_owner_user_ref",
        "priority",
        "credential_ref",
    } <= set(command["required"])
    assert command["properties"]["business_purpose"]["enum"] == [
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
    ]
    assert "credential_ref" not in mailbox["properties"]

    inbox = schemas["InboxItem"]
    assert inbox["additionalProperties"] is False
    assert inbox["properties"]["state"]["enum"] == ["identity_pending", "unassigned"]
    detail = schemas["InboxDetail"]
    assert detail["additionalProperties"] is False
    health = schemas["ConnectorHealth"]
    assert health["additionalProperties"] is False
    assert health["properties"]["status"]["enum"] == [
        "healthy",
        "degraded",
        "paused",
        "revoked",
        "unknown",
    ]


def test_v5_has_no_phase_two_operations_or_sensitive_fields() -> None:
    text = CONTRACT.read_text(encoding="utf-8").lower()

    for forbidden in (
        "claim",
        "merge",
        "draft_reply",
        "send",
        "raw_body",
        "body_html",
        "participant_address",
        "provider_message_id",
        "password",
        "api_secret",
        "credential_value",
        "access_token",
        "mapping_ref",
        "evidence_ref",
    ):
        assert forbidden not in text
