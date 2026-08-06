from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

CONTRACTS_DIR = Path(__file__).parents[2] / "contracts"
GATE3_DIR = CONTRACTS_DIR / "gate3"
EXAMPLES_DIR = CONTRACTS_DIR / "examples" / "gate3"

SCHEMA_FILES = (
    "manual-import-manifest.schema.json",
    "fact-proposal-record.schema.json",
    "entity-resolution-proposal.schema.json",
)

EXAMPLES = {
    "manual-import-manifest.json": "manual-import-manifest.schema.json",
    "fact-proposal-record.json": "fact-proposal-record.schema.json",
    "entity-resolution-proposal.json": "entity-resolution-proposal.schema.json",
}

EXPECTED_PATH_METHODS = {
    "/internal/v1/context/evidence-records": {"post"},
    "/internal/v1/context/fact-proposals": {"post"},
    "/internal/v1/context/entity-resolution-proposals": {"post"},
    "/v1/context/evidence-records/{evidence_record_id}": {"get"},
    "/v1/context/fact-proposals/{fact_proposal_record_id}": {"get"},
    "/v1/context/entity-resolution-proposals/{entity_resolution_proposal_id}": {"get"},
}

EXPECTED_SCOPES = {
    "context:evidence:write",
    "context:fact-proposal:write",
    "context:entity-resolution-proposal:write",
    "context:evidence:read",
    "context:fact-proposal:read",
    "context:entity-resolution-proposal:read",
}

EXPECTED_HEADERS = {
    "Authorization",
    "X-Site-ID",
    "X-Request-ID",
    "X-Processing-Purpose",
    "Idempotency-Key",
}


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _registry() -> Registry[Any]:
    registry: Registry[Any] = Registry()
    for path in sorted(CONTRACTS_DIR.glob("*.schema.json")):
        schema = _load(path)
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
    for filename in SCHEMA_FILES:
        schema = _load(GATE3_DIR / filename)
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
    return registry


def _validator(filename: str) -> Draft202012Validator:
    schema = _load(GATE3_DIR / filename)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(
        schema,
        registry=_registry(),
        format_checker=FormatChecker(),
    )


def _example(filename: str) -> dict[str, Any]:
    return _load(EXAMPLES_DIR / filename)


def _operations(contract: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        operation
        for path_item in contract["paths"].values()
        for method, operation in path_item.items()
        if method in {"get", "post", "put", "patch", "delete"}
    ]


def _operation_parameters(contract: dict[str, Any], path: str, method: str) -> list[dict[str, Any]]:
    path_item = contract["paths"][path]
    parameters = [*path_item.get("parameters", []), *path_item[method].get("parameters", [])]
    resolved: list[dict[str, Any]] = []
    for parameter in parameters:
        if "$ref" not in parameter:
            resolved.append(parameter)
            continue
        prefix = "#/components/parameters/"
        assert parameter["$ref"].startswith(prefix)
        resolved.append(contract["components"]["parameters"][parameter["$ref"][len(prefix) :]])
    return resolved


@pytest.mark.parametrize("filename", SCHEMA_FILES)
def test_gate3_contract_is_json_schema_2020_12(filename: str) -> None:
    schema = _load(GATE3_DIR / filename)

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize(("example_file", "schema_file"), EXAMPLES.items())
def test_gate3_examples_validate(example_file: str, schema_file: str) -> None:
    _validator(schema_file).validate(_example(example_file))


def test_manual_import_manifest_freezes_retention_classes_r0_through_r3() -> None:
    schema = _load(GATE3_DIR / "manual-import-manifest.schema.json")

    assert schema["properties"]["retention_class"]["enum"] == [
        "R0-ephemeral",
        "R1-operational",
        "R2-record",
        "R3-legal-hold",
    ]


def test_manual_import_manifest_carries_event_identity_and_is_synthetic_only() -> None:
    schema = _load(GATE3_DIR / "manual-import-manifest.schema.json")
    example = _example("manual-import-manifest.json")

    assert {
        "synthetic",
        "occurred_at",
        "original_language",
        "participants",
    } <= set(schema["required"])
    assert schema["properties"]["synthetic"]["const"] is True
    assert example["synthetic"] is True
    assert example["participants"]
    assert example["source"]["connector"] == "manual_import"


def test_manual_import_manifest_rejects_real_data_marker() -> None:
    manifest = _example("manual-import-manifest.json")
    manifest["synthetic"] = False

    with pytest.raises(ValidationError):
        _validator("manual-import-manifest.schema.json").validate(manifest)


@pytest.mark.parametrize("invalid_retention", ["ephemeral", "R4-forever", "R2"])
def test_manual_import_manifest_rejects_unknown_retention_class(
    invalid_retention: str,
) -> None:
    manifest = _example("manual-import-manifest.json")
    manifest["retention_class"] = invalid_retention

    with pytest.raises(ValidationError):
        _validator("manual-import-manifest.schema.json").validate(manifest)


@pytest.mark.parametrize("status", ["confirmed", "rejected"])
def test_fact_proposal_wrapper_rejects_non_proposed_nested_fact(status: str) -> None:
    proposal = _example("fact-proposal-record.json")
    proposal["fact"]["status"] = status

    with pytest.raises(ValidationError):
        _validator("fact-proposal-record.schema.json").validate(proposal)


@pytest.mark.parametrize("forbidden_field", ["review_case", "decision", "action"])
def test_fact_proposal_wrapper_rejects_gate4_fields(forbidden_field: str) -> None:
    proposal = _example("fact-proposal-record.json")
    proposal[forbidden_field] = "forbidden-gate4-value"

    with pytest.raises(ValidationError):
        _validator("fact-proposal-record.schema.json").validate(proposal)


def test_fact_proposal_records_required_processing_lineage() -> None:
    proposal = _example("fact-proposal-record.json")

    assert {
        "site_id",
        "processing_purpose",
        "data_classification",
        "source_lineage",
        "valid_time",
        "recorded_time",
        "processor",
        "processor_version",
        "rule_version",
        "output_version",
        "budget",
        "correlation_id",
    } <= set(proposal)
    assert proposal["processor"]["kind"] == "deterministic_test_processor"
    assert proposal["budget"]["used_units"] <= proposal["budget"]["limit_units"]


@pytest.mark.parametrize(
    "forbidden_field",
    ["merge", "merge_entities", "action", "review_case", "decision"],
)
def test_entity_resolution_proposal_cannot_merge_or_create_gate4_records(
    forbidden_field: str,
) -> None:
    proposal = _example("entity-resolution-proposal.json")
    proposal[forbidden_field] = "forbidden-gate4-value"

    with pytest.raises(ValidationError):
        _validator("entity-resolution-proposal.schema.json").validate(proposal)


def test_entity_resolution_proposal_is_proposal_only() -> None:
    proposal = _example("entity-resolution-proposal.json")
    proposal["status"] = "merged"

    with pytest.raises(ValidationError):
        _validator("entity-resolution-proposal.schema.json").validate(proposal)


def test_context_runtime_exposes_exactly_the_six_gate3_routes() -> None:
    contract = _load(GATE3_DIR / "context-runtime-v1.openapi.json")

    assert contract["openapi"] == "3.1.0"
    assert contract["x-gbos-gate"] == 3
    assert {
        path: {
            method for method in path_item if method in {"get", "post", "put", "patch", "delete"}
        }
        for path, path_item in contract["paths"].items()
    } == EXPECTED_PATH_METHODS


@pytest.mark.parametrize(
    "forbidden_fragment",
    [
        "/agent",
        "/decision",
        "/conflict",
        "/action",
        "/review-case",
        "/draft-mutation",
        "/kingdee",
        "/metrics",
    ],
)
def test_context_runtime_has_no_gate4_or_later_route(forbidden_fragment: str) -> None:
    contract = _load(GATE3_DIR / "context-runtime-v1.openapi.json")

    assert forbidden_fragment not in " ".join(contract["paths"]).lower()


def test_context_runtime_requires_auth_site_request_purpose_and_idempotency() -> None:
    contract = _load(GATE3_DIR / "context-runtime-v1.openapi.json")

    for path, methods in EXPECTED_PATH_METHODS.items():
        for method in methods:
            parameters = _operation_parameters(contract, path, method)
            header_parameters = {
                parameter["name"]: parameter
                for parameter in parameters
                if parameter["in"] == "header"
            }
            assert set(header_parameters) == EXPECTED_HEADERS
            assert all(parameter["required"] is True for parameter in header_parameters.values())


def test_context_runtime_uses_only_narrow_per_operation_scopes() -> None:
    contract = _load(GATE3_DIR / "context-runtime-v1.openapi.json")
    declared_scopes = set(
        contract["components"]["securitySchemes"]["OAuth2"]["flows"]["clientCredentials"]["scopes"]
    )

    assert declared_scopes == EXPECTED_SCOPES
    used_scopes: list[str] = []
    for operation in _operations(contract):
        assert len(operation["security"]) == 1
        assert set(operation["security"][0]) == {"OAuth2"}
        operation_scopes = operation["security"][0]["OAuth2"]
        assert len(operation_scopes) == 1
        used_scopes.extend(operation_scopes)
    assert set(used_scopes) == EXPECTED_SCOPES
    assert all("kingdee" not in scope and "admin" not in scope for scope in used_scopes)


def test_context_runtime_uses_only_gate3_response_contracts() -> None:
    contract = _load(GATE3_DIR / "context-runtime-v1.openapi.json")
    serialized = json.dumps(contract, sort_keys=True)

    for allowed_contract in (
        "evidence-record.schema.json",
        "fact-proposal-record.schema.json",
        "entity-resolution-proposal.schema.json",
    ):
        assert allowed_contract in serialized
    for forbidden_contract in (
        "verified-business-fact.schema.json",
        "conflict-record.schema.json",
        "decision-record.schema.json",
        "action-proposal.schema.json",
        "draft-mutation.schema.json",
        "approved-command.schema.json",
    ):
        assert forbidden_contract not in serialized


def test_gate3_capability_ledger_allows_only_four_output_types() -> None:
    ledger = _load(GATE3_DIR / "capabilities-v1.json")
    allowed = {entry["type"]: entry for entry in ledger["allowed_outputs"]}

    assert set(allowed) == {
        "observation",
        "evidence",
        "fact_proposal",
        "entity_resolution_proposal",
    }
    assert allowed["fact_proposal"]["required_status"] == "proposed"
    assert allowed["entity_resolution_proposal"]["required_status"] == "proposed"


def test_gate3_capability_ledger_explicitly_disables_gate4_and_external_effects() -> None:
    ledger = _load(GATE3_DIR / "capabilities-v1.json")
    capabilities = ledger["capabilities"]
    disabled = {
        "frappe_access",
        "review_case_creation",
        "agent_runtime",
        "real_model_calls",
        "kingdee_access",
        "external_side_effects",
    }

    assert disabled <= set(capabilities)
    assert all(capabilities[name]["enabled"] is False for name in disabled)
    assert capabilities["frappe_access"]["network_allowed"] is False
    assert capabilities["real_model_calls"]["network_allowed"] is False
    assert capabilities["kingdee_access"]["network_allowed"] is False
    assert capabilities["external_side_effects"]["network_allowed"] is False


@pytest.mark.parametrize(
    "forbidden_type",
    [
        "verified_fact",
        "conflict",
        "decision",
        "action_proposal",
        "review_case",
        "approved_command",
        "metric",
        "kingdee_projection",
    ],
)
def test_gate3_capability_ledger_has_no_gate4_or_later_output(forbidden_type: str) -> None:
    ledger = _load(GATE3_DIR / "capabilities-v1.json")

    assert forbidden_type not in {entry["type"] for entry in ledger["allowed_outputs"]}
