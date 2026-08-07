from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parents[2]
CONTRACT = ROOT / "contracts" / "bff-v4.openapi.json"

OPERATIONS = {
    "/api/method/esan_gbos.api.v4.integration.list_status": "get",
    "/api/method/esan_gbos.api.v4.integration.pause": "post",
    "/api/method/esan_gbos.api.v4.integration.resume": "post",
    "/api/method/esan_gbos.api.v4.integration.replay": "post",
    "/api/method/esan_gbos.api.v4.communication.list": "get",
    "/api/method/esan_gbos.api.v4.communication.get": "get",
    "/api/method/esan_gbos.api.v4.model.get_usage": "get",
    "/api/method/esan_gbos.api.v4.ai_draft.list": "get",
    "/api/method/esan_gbos.api.v4.ai_draft.get": "get",
    "/api/method/esan_gbos.api.v4.ai_draft.submit_for_review": "post",
}


def contract() -> dict[str, object]:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def resolve_schema(value: dict[str, object], schema: dict[str, object]) -> dict[str, object]:
    reference = schema.get("$ref")
    if not isinstance(reference, str):
        return schema
    assert reference.startswith("#/components/schemas/")
    return value["components"]["schemas"][reference.rsplit("/", maxsplit=1)[-1]]


def test_v4_surface_is_exactly_the_frozen_channel_and_draft_api() -> None:
    value = contract()

    assert value["openapi"] == "3.1.0"
    assert value["info"]["version"] == "4.0.0"
    assert set(value["paths"]) == set(OPERATIONS)
    for path, method in OPERATIONS.items():
        operations = value["paths"][path]
        assert {key for key in operations if key in {"get", "post", "put", "patch", "delete"}} == {
            method
        }
        assert operations[method]["x-gbos-cache"] == "no-store"


def test_v4_reads_require_session_and_commands_require_csrf_revision_and_idempotency() -> None:
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
            body_ref = operation["requestBody"]["content"]["application/x-www-form-urlencoded"][
                "schema"
            ]
            body = resolve_schema(value, body_ref)
            assert body["additionalProperties"] is False
            assert {"expected_revision", "idempotency_key"} <= set(body["required"])


def test_v4_replay_is_bounded_to_failed_deliveries_inside_the_retention_window() -> None:
    operation = contract()["paths"]["/api/method/esan_gbos.api.v4.integration.replay"]["post"]

    assert operation["x-gbos-replay-scope"] == "eligible_failed_deliveries"
    assert operation["x-gbos-replay-limit"] == 100
    assert operation["x-gbos-replay-requires"] == [
        "within_connector_replay_window",
        "not_retention_expired",
        "same_site_and_instance",
    ]


def test_v4_freezes_success_and_stable_error_envelopes() -> None:
    schemas = contract()["components"]["schemas"]

    assert schemas["SuccessMeta"]["properties"]["schema_version"]["const"] == "4.0"
    assert set(schemas["SuccessMeta"]["required"]) >= {"request_id", "schema_version"}
    error = schemas["ErrorEnvelope"]["properties"]["error"]
    assert set(error["required"]) == {"code", "message", "request_id", "details"}
    assert {
        "authentication_required",
        "permission_denied",
        "csrf_failed",
        "idempotency_conflict",
        "revision_conflict",
        "request_in_progress",
    } <= set(error["properties"]["code"]["enum"])


def test_v4_freezes_channel_communication_usage_and_ai_draft_shapes() -> None:
    schemas = contract()["components"]["schemas"]

    connector = schemas["ConnectorStatus"]
    assert set(connector["required"]) >= {
        "instance_id",
        "channel",
        "status",
        "checkpoint_version",
        "backlog",
        "last_success_at",
        "safe_error_code",
        "freshness",
        "revision",
    }
    assert connector["properties"]["status"]["enum"] == [
        "enabled",
        "paused",
        "error",
        "disabled",
    ]

    summary = schemas["CommunicationSummary"]
    assert set(summary["required"]) >= {
        "observation_id",
        "channel",
        "occurred_at",
        "summary_zh",
        "original_language",
        "classification",
        "review_status",
        "team_ref",
        "party_ref",
        "evidence_count",
    }
    detail = schemas["CommunicationDetail"]
    detail_extension = detail["allOf"][1]
    assert {
        "evidence",
        "fact_proposals",
        "association_suggestions",
        "model",
        "raw_access_allowed",
    } <= set(detail_extension["required"])

    usage = schemas["ModelUsage"]
    assert usage["properties"]["model"]["const"] == "deepseek-v4-flash"
    assert set(usage["required"]) >= {
        "period",
        "tokens",
        "token_state",
        "cost",
        "soft_limit_usd",
        "hard_limit_usd",
        "state",
    }
    assert usage["properties"]["tokens"]["type"] == ["integer", "null"]
    assert usage["properties"]["token_state"]["enum"] == ["known", "partial", "unknown"]
    assert usage["properties"]["cost"]["$ref"].endswith("/UsageCost")
    assert usage["properties"]["soft_limit_usd"]["minimum"] == 0
    assert usage["properties"]["hard_limit_usd"]["exclusiveMinimum"] == 0
    assert schemas["UsageCost"]["properties"]["state"]["enum"] == [
        "known",
        "partial",
        "unknown",
    ]

    draft = schemas["AiDraft"]
    assert draft["properties"]["kind"]["enum"] == [
        "Work Item",
        "Review Case",
        "CEO Informal Observation",
    ]
    assert draft["properties"]["status"]["enum"] == ["AI Draft", "Pending"]
    assert draft["properties"]["origin"]["const"] == "AI"


def test_v4_never_exposes_secret_raw_model_or_automatic_formal_command_fields() -> None:
    text = CONTRACT.read_text(encoding="utf-8").lower()

    for forbidden in (
        "api_key",
        "access_token",
        "refresh_token",
        "secret",
        "raw_prompt",
        "raw_response",
        "identity_mapping",
        "auto_approve",
        "auto_execute",
        "approvedcommand",
    ):
        assert forbidden not in text
