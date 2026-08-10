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
    "/api/method/esan_gbos.api.v4.identity.list_states": "get",
    "/api/method/esan_gbos.api.v4.identity.get_state": "get",
    "/api/method/esan_gbos.api.v4.identity.list_candidates": "get",
    "/api/method/esan_gbos.api.v4.identity.list_pending_reviews": "get",
    "/api/method/esan_gbos.api.v4.identity.get_pending_review": "get",
    "/api/method/esan_gbos.api.v4.identity.submit_for_review": "post",
    "/api/method/esan_gbos.api.v4.identity.revoke": "post",
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
    assert "connector_account_user_ref" not in detail_extension["required"]
    assert "connector_account_user_ref" not in detail_extension["properties"]

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


def test_v4_identity_surface_has_exactly_two_commands_and_no_direct_decision_or_write_api() -> None:
    value = contract()
    identity_paths = {
        path: operation for path, operation in value["paths"].items() if ".identity." in path
    }
    post_paths = {path for path, operation in identity_paths.items() if "post" in operation}

    assert post_paths == {
        "/api/method/esan_gbos.api.v4.identity.submit_for_review",
        "/api/method/esan_gbos.api.v4.identity.revoke",
    }
    lowered = " ".join(identity_paths).lower()
    for forbidden in ("approve", "confirm", "merge", "send", "write"):
        assert forbidden not in lowered


def test_v4_identity_contract_freezes_closed_states_candidates_reviews_and_commands() -> None:
    schemas = contract()["components"]["schemas"]

    state = schemas["IdentityState"]
    assert state["additionalProperties"] is False
    assert state["properties"]["status"]["enum"] == [
        "unresolved",
        "proposed",
        "pending",
        "confirmed",
        "rejected",
        "revoked",
    ]
    assert "external_subject" not in state["properties"]
    assert "target_ref" not in state["properties"]
    assert state["properties"]["target_type"]["enum"] == ["User", "Party"]

    candidate = schemas["IdentityCandidate"]
    assert candidate["additionalProperties"] is False
    assert set(candidate["required"]) == {"candidate_type", "candidate_ref", "display_label"}
    assert candidate["properties"]["candidate_type"]["enum"] == ["User", "Party", "Contact"]
    reviewer = schemas["IdentityReviewer"]
    assert reviewer["additionalProperties"] is False
    assert set(reviewer["required"]) == {"reviewer_ref", "display_label"}
    candidate_payload = schemas["IdentityCandidateListPayload"]
    assert "eligible_reviewers" in candidate_payload["required"]
    assert candidate_payload["properties"]["eligible_reviewers"]["items"]["$ref"].endswith(
        "/IdentityReviewer"
    )

    review = schemas["IdentityPendingReview"]
    assert review["additionalProperties"] is False
    assert {"evidence_refs", "target", "mapping_revision", "policy_version"} <= set(
        review["required"]
    )
    assert "external_subject" not in review["properties"]

    submit = schemas["IdentitySubmitForReviewCommand"]
    assert submit["additionalProperties"] is False
    assert submit["properties"]["expected_state"]["enum"] == ["unresolved", "rejected"]
    assert submit["properties"]["expected_revision"] == {"type": "integer", "minimum": 0}
    assert submit["allOf"] == [
        {
            "if": {"properties": {"expected_state": {"const": "unresolved"}}},
            "then": {"properties": {"expected_revision": {"const": 0}}},
        },
        {
            "if": {"properties": {"expected_state": {"const": "rejected"}}},
            "then": {"properties": {"expected_revision": {"minimum": 1}}},
        },
    ]
    assert {"expected_revision", "idempotency_key", "assigned_reviewer"} <= set(submit["required"])

    revoke = schemas["IdentityRevokeCommand"]
    assert revoke["additionalProperties"] is False
    assert {"expected_revision", "idempotency_key", "mapping_ref", "identity_ref"} <= set(
        revoke["required"]
    )


def test_v4_identity_operations_freeze_roles_scope_csrf_no_store_and_safe_errors() -> None:
    value = contract()
    paths = value["paths"]
    submit = paths["/api/method/esan_gbos.api.v4.identity.submit_for_review"]["post"]
    revoke = paths["/api/method/esan_gbos.api.v4.identity.revoke"]["post"]

    assert submit["x-gbos-roles"] == [
        "Sales User",
        "Sales Manager",
        "Integration Admin",
        "GBOS Admin",
        "CEO",
    ]
    candidate_list = paths["/api/method/esan_gbos.api.v4.identity.list_candidates"]["get"]
    assert candidate_list["x-gbos-roles"] == submit["x-gbos-roles"]
    candidate_policy = {
        "sales_roles": ["Sales User", "Sales Manager"],
        "sales_candidate_types": ["Party", "Contact"],
        "administrative_roles": ["Integration Admin", "GBOS Admin", "CEO"],
        "administrative_candidate_types": ["User", "Party", "Contact"],
        "mixed_role_precedence": "administrative",
    }
    assert candidate_list["x-gbos-candidate-policy"] == candidate_policy
    assert submit["x-gbos-candidate-policy"] == candidate_policy
    assert submit["x-gbos-transition"] == [
        "unresolved -> AI Draft -> Pending",
        "Rejected Active -> AI Draft Active -> Pending Active",
    ]
    assert revoke["x-gbos-roles"] == ["Integration Admin", "GBOS Admin"]
    for operation in (submit, revoke):
        assert operation["x-gbos-cache"] == "no-store"
        assert operation["x-gbos-scope"] == "communication_and_same_team"
        assert operation["x-gbos-audit"] == "request_id_and_idempotency"
        assert {"FrappeSession", "FrappeCsrf"} <= set(operation["security"][0])

    errors = set(
        value["components"]["schemas"]["ErrorEnvelope"]["properties"]["error"]["properties"][
            "code"
        ]["enum"]
    )
    assert {
        "identity_mismatch",
        "suggestion_mismatch",
        "candidate_ineligible",
        "candidate_type_forbidden",
        "reviewer_ineligible",
        "revision_conflict",
        "idempotency_conflict",
    } <= errors
