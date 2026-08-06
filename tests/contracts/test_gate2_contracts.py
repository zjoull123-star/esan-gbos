from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

CONTRACTS_DIR = Path(__file__).parents[2] / "contracts"
EXAMPLES_DIR = CONTRACTS_DIR / "examples" / "gate2"


def load_validator(filename: str) -> Draft202012Validator:
    schemas = [
        json.loads(path.read_text(encoding="utf-8")) for path in CONTRACTS_DIR.glob("*.schema.json")
    ]
    registry: Registry[Any] = Registry()
    for schema in schemas:
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
    schema = next(schema for schema in schemas if filename in schema["$id"])
    return Draft202012Validator(
        schema,
        registry=registry,
        format_checker=FormatChecker(),
    )


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _example(filename: str) -> dict[str, Any]:
    return json.loads((EXAMPLES_DIR / filename).read_text(encoding="utf-8"))


EXAMPLE_SCHEMAS = {
    "evidence-record.json": "evidence-record.schema.json",
    "verified-business-fact.json": "verified-business-fact.schema.json",
    "conflict-record.json": "conflict-record.schema.json",
    "decision-record.json": "decision-record.schema.json",
    "action-proposal.json": "action-proposal.schema.json",
    "action-approval.json": "action-approval.schema.json",
    "action-execution.json": "action-execution.schema.json",
    "action-verification.json": "action-verification.schema.json",
    "agent-task.json": "agent-task.schema.json",
    "agent-timeline-event.json": "agent-timeline-event.schema.json",
    "metric-definition.json": "metric-definition.schema.json",
    "metric-response-available.json": "metric-response.schema.json",
    "metric-response-unavailable.json": "metric-response.schema.json",
}


@pytest.mark.parametrize(("example_name", "schema_name"), EXAMPLE_SCHEMAS.items())
def test_gate2_canonical_example_validates(example_name: str, schema_name: str) -> None:
    load_validator(schema_name).validate(_example(example_name))


def test_gate2_examples_are_explicitly_mapped_to_a_schema() -> None:
    non_kingdee_examples = {
        path.name for path in EXAMPLES_DIR.glob("*.json") if "kingdee" not in path.name
    }
    assert non_kingdee_examples == set(EXAMPLE_SCHEMAS)


@pytest.mark.parametrize("missing", ("evidence_refs", "confirmation_decision_ref"))
def test_verified_fact_requires_evidence_and_confirmation_decision(missing: str) -> None:
    instance = _example("verified-business-fact.json")
    instance.pop(missing)

    with pytest.raises(ValidationError):
        load_validator("verified-business-fact.schema.json").validate(instance)


def test_verified_fact_reuses_frozen_fact_proposal_subject_predicate_and_value_shapes() -> None:
    schema = json.loads(
        (CONTRACTS_DIR / "verified-business-fact.schema.json").read_text(encoding="utf-8")
    )

    assert {"subject_ref", "predicate", "value"} <= set(schema["required"])
    for field in ("subject_ref", "predicate", "value"):
        assert schema["properties"][field]["$ref"] == (
            f"https://contracts.esan.example/gbos/v1/extracted-fact.schema.json#/properties/{field}"
        )


@pytest.mark.parametrize("missing", ("subject_ref", "predicate", "value"))
def test_verified_fact_rejects_missing_business_statement_field(missing: str) -> None:
    instance = _example("verified-business-fact.json")
    instance.update(
        {
            "subject_ref": "opportunity-SYNTH-001",
            "predicate": "requested_quantity",
            "value": {
                "type": "number",
                "number": 1000,
                "unit": "pcs",
            },
        }
    )
    instance.pop(missing)

    with pytest.raises(ValidationError):
        load_validator("verified-business-fact.schema.json").validate(instance)


@pytest.mark.parametrize(
    ("example_name", "schema_name"),
    (
        ("evidence-record.json", "evidence-record.schema.json"),
        ("agent-task.json", "agent-task.schema.json"),
        ("action-proposal.json", "action-proposal.schema.json"),
    ),
)
def test_cross_boundary_records_require_restricted_processing_purpose(
    example_name: str,
    schema_name: str,
) -> None:
    instance = _example(example_name)
    instance["processing_purpose"] = "business_operations"
    load_validator(schema_name).validate(instance)

    instance.pop("processing_purpose")
    with pytest.raises(ValidationError):
        load_validator(schema_name).validate(instance)


@pytest.mark.parametrize(
    ("example_name", "schema_name"),
    (
        ("evidence-record.json", "evidence-record.schema.json"),
        ("agent-task.json", "agent-task.schema.json"),
        ("action-proposal.json", "action-proposal.schema.json"),
    ),
)
def test_cross_boundary_records_reject_unknown_processing_purpose(
    example_name: str,
    schema_name: str,
) -> None:
    instance = _example(example_name)
    instance["processing_purpose"] = "arbitrary_model_use"

    with pytest.raises(ValidationError):
        load_validator(schema_name).validate(instance)


def test_resolved_conflict_requires_resolver_basis_and_decision() -> None:
    validator = load_validator("conflict-record.schema.json")
    valid = _example("conflict-record.json")

    for missing in ("resolved_by", "resolution_basis", "resolution_decision_ref"):
        instance = deepcopy(valid)
        instance.pop(missing)
        with pytest.raises(ValidationError):
            validator.validate(instance)


def test_decision_requires_input_fact_versions() -> None:
    instance = _example("decision-record.json")
    instance["input_fact_versions"] = []

    with pytest.raises(ValidationError):
        load_validator("decision-record.schema.json").validate(instance)


def test_action_approval_requires_human_reviewer() -> None:
    instance = _example("action-approval.json")
    instance.pop("human_reviewer")

    with pytest.raises(ValidationError):
        load_validator("action-approval.schema.json").validate(instance)


def test_action_execution_requires_approved_stage_references() -> None:
    validator = load_validator("action-execution.schema.json")
    valid = _example("action-execution.json")

    for missing in ("approval_ref", "approved_command_ref", "approval_status_snapshot"):
        instance = deepcopy(valid)
        instance.pop(missing)
        with pytest.raises(ValidationError):
            validator.validate(instance)


@pytest.mark.parametrize(
    "schema_name, example_name, forbidden_action",
    (
        ("action-proposal.schema.json", "action-proposal.json", "kingdee.sales_order.write"),
        ("action-proposal.schema.json", "action-proposal.json", "external.email.send"),
        ("action-approval.schema.json", "action-approval.json", "kingdee.bill.audit"),
        ("action-execution.schema.json", "action-execution.json", "database.direct_write"),
    ),
)
def test_action_contracts_reject_external_and_kingdee_writes(
    schema_name: str,
    example_name: str,
    forbidden_action: str,
) -> None:
    instance = _example(example_name)
    instance["action_type"] = forbidden_action

    with pytest.raises(ValidationError):
        load_validator(schema_name).validate(instance)


def test_action_verification_requires_execution_reference() -> None:
    instance = _example("action-verification.json")
    instance.pop("execution_ref")

    with pytest.raises(ValidationError):
        load_validator("action-verification.schema.json").validate(instance)


def test_action_records_are_ordered_by_python_cross_record_invariant() -> None:
    proposal = _example("action-proposal.json")
    approval = _example("action-approval.json")
    execution = _example("action-execution.json")
    verification = _example("action-verification.json")

    assert approval["proposal_ref"] == proposal["action_proposal_id"]
    assert execution["approval_ref"] == approval["action_approval_id"]
    assert verification["execution_ref"] == execution["action_execution_id"]
    for field in ("site_id", "action_type", "correlation_id", "payload_digest"):
        assert proposal[field] == approval[field] == execution[field]
    assert (
        _timestamp(proposal["created_at"])
        <= _timestamp(approval["reviewed_at"])
        <= _timestamp(execution["started_at"])
        <= _timestamp(verification["verified_at"])
    )


def test_action_cross_record_invariant_detects_execution_before_approval() -> None:
    approval = _example("action-approval.json")
    execution = _example("action-execution.json")
    execution["started_at"] = "2026-08-06T01:00:00Z"

    with pytest.raises(AssertionError):
        assert _timestamp(approval["reviewed_at"]) <= _timestamp(execution["started_at"])


def test_action_execution_requires_payload_digest() -> None:
    instance = _example("action-execution.json")
    instance["payload_digest"] = "c" * 64
    load_validator("action-execution.schema.json").validate(instance)

    instance.pop("payload_digest")
    with pytest.raises(ValidationError):
        load_validator("action-execution.schema.json").validate(instance)


@pytest.mark.parametrize(
    ("field", "mismatch"),
    (
        ("site_id", "other.localhost"),
        ("action_type", "internal.review_case.create"),
        ("correlation_id", "corr-SYNTH-mismatch"),
        ("payload_digest", "e" * 64),
    ),
)
def test_action_cross_record_integrity_invariant_detects_mismatch(
    field: str,
    mismatch: str,
) -> None:
    proposal = _example("action-proposal.json")
    approval = _example("action-approval.json")
    execution = _example("action-execution.json")
    execution["payload_digest"] = proposal["payload_digest"]
    execution[field] = mismatch

    with pytest.raises(AssertionError):
        assert proposal[field] == approval[field] == execution[field]


def test_agent_task_requires_lease_pair_for_active_status() -> None:
    validator = load_validator("agent-task.schema.json")
    valid = _example("agent-task.json")

    for missing in ("lease_owner", "lease_expires_at"):
        instance = deepcopy(valid)
        instance.pop(missing)
        with pytest.raises(ValidationError):
            validator.validate(instance)


@pytest.mark.parametrize(
    "path, value",
    (
        (("budget", "token_limit"), 0),
        (("budget", "cost_limit_usd"), -0.01),
        (("budget", "time_limit_seconds"), 0),
        (("attempt",), -1),
        (("max_attempts",), 0),
        (("priority",), 101),
    ),
)
def test_agent_task_rejects_invalid_budget_attempt_and_priority_bounds(
    path: tuple[str, ...],
    value: int | float,
) -> None:
    instance = _example("agent-task.json")
    target: dict[str, Any] = instance
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(ValidationError):
        load_validator("agent-task.schema.json").validate(instance)


@pytest.mark.parametrize("status", ("succeeded", "failed", "dead_letter", "cancelled"))
def test_agent_task_terminal_states_reject_a_live_lease(status: str) -> None:
    instance = _example("agent-task.json")
    instance["status"] = status

    with pytest.raises(ValidationError):
        load_validator("agent-task.schema.json").validate(instance)


def test_agent_task_requires_causation_and_correlation() -> None:
    validator = load_validator("agent-task.schema.json")
    valid = _example("agent-task.json")

    for missing in ("causation_id", "correlation_id"):
        instance = deepcopy(valid)
        instance.pop(missing)
        with pytest.raises(ValidationError):
            validator.validate(instance)


def test_agent_attempt_count_cross_field_invariant_is_explicit_python_check() -> None:
    instance = _example("agent-task.json")
    assert instance["attempt"] <= instance["max_attempts"]

    instance["attempt"] = instance["max_attempts"] + 1
    with pytest.raises(AssertionError):
        assert instance["attempt"] <= instance["max_attempts"]


def test_agent_timeline_is_monotonic_and_unique_as_cross_record_invariant() -> None:
    first = _example("agent-timeline-event.json")
    second = deepcopy(first)
    second.update(
        {
            "timeline_event_id": "timeline-event-SYNTH-002",
            "sequence": first["sequence"] + 1,
            "event_type": "started",
            "occurred_at": "2026-08-06T02:00:05Z",
        }
    )
    events = [first, second]

    assert len({event["timeline_event_id"] for event in events}) == len(events)
    assert [event["sequence"] for event in events] == sorted(event["sequence"] for event in events)
    assert [_timestamp(event["occurred_at"]) for event in events] == sorted(
        _timestamp(event["occurred_at"]) for event in events
    )


@pytest.mark.parametrize(
    "missing",
    ("owner", "window", "unit", "source_lineage", "exclusions", "reconciliation"),
)
def test_metric_definition_requires_governance_fields(missing: str) -> None:
    instance = _example("metric-definition.json")
    instance.pop(missing)

    with pytest.raises(ValidationError):
        load_validator("metric-definition.schema.json").validate(instance)


def test_metric_response_available_requires_value_and_lineage() -> None:
    validator = load_validator("metric-response.schema.json")
    valid = _example("metric-response-available.json")

    for missing in (
        "value",
        "definition_version",
        "freshness",
        "coverage",
        "reconciliation",
        "source_lineage",
    ):
        instance = deepcopy(valid)
        instance.pop(missing)
        with pytest.raises(ValidationError):
            validator.validate(instance)


@pytest.mark.parametrize(
    ("section", "status"),
    (
        ("freshness", "stale"),
        ("freshness", "unknown"),
        ("coverage", "insufficient"),
        ("coverage", "unknown"),
        ("reconciliation", "failed"),
        ("reconciliation", "not_run"),
    ),
)
def test_metric_response_cannot_be_available_without_all_quality_gates(
    section: str,
    status: str,
) -> None:
    instance = _example("metric-response-available.json")
    instance[section]["status"] = status

    with pytest.raises(ValidationError):
        load_validator("metric-response.schema.json").validate(instance)


def test_metric_response_coverage_requires_explicit_status() -> None:
    instance = _example("metric-response-unavailable.json")
    instance["coverage"].pop("status", None)

    with pytest.raises(ValidationError):
        load_validator("metric-response.schema.json").validate(instance)


def test_metric_response_unavailable_requires_reason_and_forbids_value() -> None:
    validator = load_validator("metric-response.schema.json")
    valid = _example("metric-response-unavailable.json")
    validator.validate(valid)

    without_reason = deepcopy(valid)
    without_reason.pop("unavailable_reason")
    with pytest.raises(ValidationError):
        validator.validate(without_reason)

    with_value = deepcopy(valid)
    with_value["value"] = 42
    with pytest.raises(ValidationError):
        validator.validate(with_value)
