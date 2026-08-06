from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

CONTRACTS_DIR = Path(__file__).parents[2] / "contracts"
GATE4_DIR = CONTRACTS_DIR / "gate4"
EXAMPLES_DIR = CONTRACTS_DIR / "examples" / "gate4"

SCHEMA_FILES = (
    "conflict-record.schema.json",
    "verified-business-fact.schema.json",
    "decision-record.schema.json",
    "action-proposal.schema.json",
    "action-guard-decision.schema.json",
    "action-approval.schema.json",
    "decision-trace-response.schema.json",
)

EXAMPLES = {
    "conflict-record.json": "conflict-record.schema.json",
    "verified-business-fact.json": "verified-business-fact.schema.json",
    "decision-record.json": "decision-record.schema.json",
    "action-proposal.json": "action-proposal.schema.json",
    "action-guard-decision.json": "action-guard-decision.schema.json",
    "action-approval.json": "action-approval.schema.json",
    "decision-trace-response.json": "decision-trace-response.schema.json",
}


def _load(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _registry() -> Registry[Any]:
    registry: Registry[Any] = Registry()
    for path in (
        *sorted(CONTRACTS_DIR.glob("*.schema.json")),
        *sorted((CONTRACTS_DIR / "gate3").glob("*.schema.json")),
        *sorted(GATE4_DIR.glob("*.schema.json")),
    ):
        schema = _load(path)
        registry = registry.with_resource(str(schema["$id"]), Resource.from_contents(schema))
    return registry


def _validator(filename: str) -> Draft202012Validator:
    schema = _load(GATE4_DIR / filename)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(
        schema,
        registry=_registry(),
        format_checker=FormatChecker(),
    )


def _example(filename: str) -> dict[str, Any]:
    return _load(EXAMPLES_DIR / filename)


@pytest.mark.parametrize("filename", SCHEMA_FILES)
def test_gate4_contract_is_json_schema_2020_12(filename: str) -> None:
    schema = _load(GATE4_DIR / filename)

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"].startswith("https://contracts.esan.example/gbos/gate4/")
    Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize(("example_file", "schema_file"), EXAMPLES.items())
def test_gate4_examples_validate(example_file: str, schema_file: str) -> None:
    _validator(schema_file).validate(_example(example_file))


def test_verified_fact_requires_exact_proposal_revision_and_evidence() -> None:
    fact = _example("verified-business-fact.json")

    for field in ("proposal_version", "proposal_revision", "evidence_refs"):
        invalid = deepcopy(fact)
        invalid.pop(field)
        with pytest.raises(ValidationError):
            _validator("verified-business-fact.schema.json").validate(invalid)


def test_verified_fact_version_after_first_requires_explicit_supersession() -> None:
    fact = _example("verified-business-fact.json")
    fact["fact_version"] = 2

    with pytest.raises(ValidationError):
        _validator("verified-business-fact.schema.json").validate(fact)

    fact["supersedes_fact_ref"] = "verified-fact-SYNTH-000"
    fact["supersedes_fact_version"] = 1
    _validator("verified-business-fact.schema.json").validate(fact)


def test_rule_decision_requires_rule_version() -> None:
    decision = _example("decision-record.json")
    decision["decision_type"] = "rule"
    decision["review_status"] = "rule_reviewed"

    with pytest.raises(ValidationError):
        _validator("decision-record.schema.json").validate(decision)

    decision["rule_version"] = "quantity-confirm-v1"
    _validator("decision-record.schema.json").validate(decision)


def test_action_proposal_is_immutable_proposal_only() -> None:
    proposal = _example("action-proposal.json")
    proposal["status"] = "approved"

    with pytest.raises(ValidationError):
        _validator("action-proposal.schema.json").validate(proposal)


@pytest.mark.parametrize(
    "forbidden_field",
    ["draft_mutation", "approved_command", "execution", "frappe_write", "kingdee_mutation"],
)
def test_gate4_action_contracts_reject_execution_or_external_write_shapes(
    forbidden_field: str,
) -> None:
    for schema_file, example_file in (
        ("action-proposal.schema.json", "action-proposal.json"),
        ("action-guard-decision.schema.json", "action-guard-decision.json"),
        ("action-approval.schema.json", "action-approval.json"),
    ):
        value = _example(example_file)
        value[forbidden_field] = {"forbidden": True}
        with pytest.raises(ValidationError):
            _validator(schema_file).validate(value)


def test_action_guard_binds_exact_proposal_and_target_revisions() -> None:
    guard = _example("action-guard-decision.json")

    for field in ("proposal_version", "proposal_revision", "target_revision"):
        invalid = deepcopy(guard)
        invalid.pop(field)
        with pytest.raises(ValidationError):
            _validator("action-guard-decision.schema.json").validate(invalid)


def test_decision_trace_contains_exact_fact_and_evidence_versions() -> None:
    trace = _example("decision-trace-response.json")

    assert trace["decision"]["output_fact_refs"] == [
        {
            "fact_id": trace["facts"][0]["fact_id"],
            "fact_version": trace["facts"][0]["fact_version"],
        }
    ]
    assert trace["decision"]["evidence_refs"] == [trace["evidence"][0]["evidence_record_id"]]
    assert trace["proposal"]["proposal_ref"] == trace["decision"]["proposal_ref"]
    assert trace["proposal"]["proposal_revision"] == trace["decision"]["proposal_revision"]
