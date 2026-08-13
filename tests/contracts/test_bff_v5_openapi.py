from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parents[2]
CONTRACT = ROOT / "contracts" / "bff-v5.openapi.json"

OPERATIONS = {
    "/api/method/esan_gbos.api.v5.email_admin.list_mailboxes": "get",
    "/api/method/esan_gbos.api.v5.email_admin.get_mailbox": "get",
    "/api/method/esan_gbos.api.v5.email_admin.list_rules": "get",
    "/api/method/esan_gbos.api.v5.email_admin.connector_health": "get",
    "/api/method/esan_gbos.api.v5.email_admin.upsert_mailbox": "post",
    "/api/method/esan_gbos.api.v5.email_admin.set_mailbox_status": "post",
    "/api/method/esan_gbos.api.v5.email_admin.upsert_rule": "post",
    "/api/method/esan_gbos.api.v5.email_inbox.list": "get",
    "/api/method/esan_gbos.api.v5.email_inbox.get": "get",
    "/api/method/esan_gbos.api.v5.email_inbox.claim": "post",
    "/api/method/esan_gbos.api.v5.email_inbox.reassign": "post",
    "/api/method/esan_gbos.api.v5.email_inbox.transition": "post",
    "/api/method/esan_gbos.api.v5.email_inbox.merge": "post",
    "/api/method/esan_gbos.api.v5.email_inbox.split": "post",
    "/api/method/esan_gbos.api.v5.email_inbox.link_business": "post",
    "/api/method/esan_gbos.api.v5.email_inbox.save_draft": "post",
    "/api/method/esan_gbos.api.v5.email_inbox.reveal": "post",
    "/api/method/esan_gbos.api.v5.email_send.submit_for_review": "post",
    "/api/method/esan_gbos.api.v5.email_send.approve": "post",
}


def contract() -> dict[str, object]:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def test_v5_commands_do_not_accept_caller_authority_booleans() -> None:
    serialized = json.dumps(contract(), sort_keys=True)
    for forbidden in ("assignee_enabled", "authority_valid", "authority_team_ref"):
        assert forbidden not in serialized


def resolve_schema(value: dict[str, object], schema: dict[str, object]) -> dict[str, object]:
    reference = schema.get("$ref")
    if not isinstance(reference, str):
        return schema
    assert reference.startswith("#/components/schemas/")
    return value["components"]["schemas"][reference.rsplit("/", maxsplit=1)[-1]]


def test_v5_surface_is_exactly_the_nineteen_email_operations_and_never_cached() -> None:
    value = contract()

    assert value["openapi"] == "3.1.0"
    assert value["info"]["version"] == "5.0.0"
    assert len(value["paths"]) == 19
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
        elif method == "post":
            assert roles == [
                "Sales Manager",
                "Sales User",
                "Reviewer",
                "GBOS Admin",
            ]
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
        if method == "post" and not path.endswith(".reveal"):
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
        "canonical_mailbox_address",
        "provider_account_ref",
        "observer_connector_instance_ref",
        "default_team_ref",
        "account_owner_user_ref",
        "priority",
        "credential_ref",
    } <= set(command["required"])
    assert command["properties"]["canonical_mailbox_address"] == {
        "type": "string",
        "format": "email",
        "maxLength": 254,
        "writeOnly": True,
    }
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
    assert "canonical_mailbox_address" not in mailbox["properties"]
    assert "mailbox_address_identity_ref" not in mailbox["properties"]
    assert "mailbox_address_identity_ref" not in command["properties"]

    inbox = schemas["InboxItem"]
    assert inbox["additionalProperties"] is False
    assert inbox["properties"]["state"]["enum"] == [
        "identity_pending",
        "unassigned",
        "assigned",
        "draft",
        "waiting_internal",
        "waiting_customer",
        "converted",
        "closed",
        "quarantined",
        "send_queued",
        "send_uncertain",
    ]
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


def test_v5_has_no_send_operation_or_sensitive_fields() -> None:
    text = CONTRACT.read_text(encoding="utf-8").lower()

    for forbidden in (
        "sendoutbox",
        "raw_body",
        "body_html",
        "participant_address",
        "provider_message_id",
        "password",
        "api_secret",
        "credential_value",
        "access_token",
        "participant_address",
    ):
        assert forbidden not in text
    send_paths = {path for path in contract()["paths"] if ".email_send." in path}
    assert send_paths == {
        "/api/method/esan_gbos.api.v5.email_send.submit_for_review",
        "/api/method/esan_gbos.api.v5.email_send.approve",
    }
    assert not any(
        path not in send_paths
        and any(marker in path for marker in (".send", "provider", "direct", "outbox"))
        for path in contract()["paths"]
    )


def test_v5_email_send_governance_requests_are_closed_and_server_authoritative() -> None:
    value = contract()

    submit = value["paths"]["/api/method/esan_gbos.api.v5.email_send.submit_for_review"]["post"]
    approve = value["paths"]["/api/method/esan_gbos.api.v5.email_send.approve"]["post"]

    assert submit["x-gbos-provider-send"] is False
    assert approve["x-gbos-provider-send"] is False
    submit_body = resolve_schema(
        value,
        submit["requestBody"]["content"]["application/x-www-form-urlencoded"]["schema"],
    )
    approve_body = resolve_schema(
        value,
        approve["requestBody"]["content"]["application/x-www-form-urlencoded"]["schema"],
    )

    assert submit_body["additionalProperties"] is False
    assert set(submit_body["required"]) == {
        "inbox_item_ref",
        "draft_ref",
        "expected_revision",
        "expected_draft_revision",
        "idempotency_key",
    }
    assert set(submit_body["properties"]) == set(submit_body["required"])
    assert submit_body["properties"]["expected_revision"]["minimum"] == 1
    assert submit_body["properties"]["expected_draft_revision"]["minimum"] == 1
    assert approve_body["additionalProperties"] is False
    assert set(approve_body["required"]) == {
        "review_case_name",
        "expected_revision",
        "decision_note",
        "idempotency_key",
    }
    assert set(approve_body["properties"]) == set(approve_body["required"])
    assert approve_body["properties"]["expected_revision"]["minimum"] == 1
    assert approve_body["properties"]["decision_note"] == {
        "type": "string",
        "minLength": 1,
        "maxLength": 2000,
    }
    for body in (submit_body, approve_body):
        assert "live_authority_snapshot" not in body["properties"]
        assert "raw_address" not in body["properties"]
        assert "raw_content" not in body["properties"]


def test_v5_reveal_and_save_draft_are_closed_no_store_non_send_operations() -> None:
    value = contract()
    reveal = value["paths"]["/api/method/esan_gbos.api.v5.email_inbox.reveal"]["post"]
    save = value["paths"]["/api/method/esan_gbos.api.v5.email_inbox.save_draft"]["post"]

    assert reveal["x-gbos-cache"] == save["x-gbos-cache"] == "no-store"
    assert reveal["x-gbos-restricted-reveal"] is True
    assert save["x-gbos-provider-send"] is False
    assert "SendOutbox" not in value["components"]["schemas"]
