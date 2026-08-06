from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

CONTRACTS_DIR = Path(__file__).parents[2] / "contracts"
GATE2_DIR = CONTRACTS_DIR / "gate2"


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


def _manifest(filename: str) -> dict[str, Any]:
    return json.loads((GATE2_DIR / filename).read_text(encoding="utf-8"))


def test_contract_evolution_matrix_has_one_non_duplicate_strategy_per_concept() -> None:
    matrix = _manifest("contract-evolution-matrix.json")
    entries = matrix["entries"]
    by_concept = {entry["concept"]: entry for entry in entries}

    assert len(by_concept) == len(entries)
    assert matrix["schema_version"] == "2.0"
    assert by_concept["Fact Proposal"]["strategy"] == "reuse"
    assert by_concept["Fact Proposal"]["contract"] == "ExtractedFact"
    assert by_concept["Evidence Pointer"]["strategy"] == "reuse"
    assert by_concept["Evidence Pointer"]["contract"] == "EvidenceRef"
    assert by_concept["Approved Internal Command"]["strategy"] == "reuse"
    assert by_concept["Approved Internal Command"]["contract"] == "ApprovedCommand"
    assert by_concept["Action Approval"]["contract"] == "ActionApproval"
    assert by_concept["Action Approval"]["semantically_distinct_from"] == ["ApprovedCommand"]
    assert {
        "Evidence Record",
        "Verified Business Fact",
        "Conflict Record",
        "Decision Record",
        "Action Proposal",
        "Action Approval",
        "Action Execution",
        "Action Verification",
        "Agent Task",
        "Agent Timeline Event",
        "Metric Definition",
        "Metric Response",
        "Kingdee Read Projection",
    } <= set(by_concept)
    assert all(entry["strategy"] in {"reuse", "extend", "new"} for entry in entries)
    assert all(entry["rationale"] for entry in entries)


def test_context_ontology_freezes_node_and_relation_allow_lists() -> None:
    ontology = _manifest("context-ontology-v0.json")

    assert ontology["ontology_version"] == "0.1.0"
    assert ontology["storage_projection"] == "postgresql"
    assert ontology["dedicated_graph_runtime_enabled"] is False
    assert set(ontology["node_types"]) == {
        "Customer",
        "Supplier",
        "Contact",
        "InternalUser",
        "CommunicationEvent",
        "Requirement",
        "Opportunity",
        "Product",
        "ProductSpecification",
        "SampleRequest",
        "Quotation",
        "SalesOrder",
        "PurchaseRequirement",
        "SupplierQuotation",
        "PurchaseOrder",
        "Shipment",
        "Receivable",
        "RiskSignal",
        "Evidence",
        "Decision",
        "ActionProposal",
        "ActionExecution",
    }
    assert set(ontology["relation_types"]) == {
        "WORKS_AT",
        "COMMUNICATED_WITH",
        "EXPRESSES_REQUIREMENT",
        "RELATES_TO",
        "REQUESTED_SAMPLE",
        "QUOTED_IN",
        "CONVERTED_TO",
        "SUPPLIED_BY",
        "DEPENDS_ON",
        "IMPACTS",
        "SUPPORTED_BY",
        "CONTRADICTS",
        "SUPERSEDES",
        "DERIVED_FROM",
        "CAUSED",
        "INFLUENCED",
        "APPROVED_BY",
        "EXECUTED_AS",
    }


def test_context_relations_require_temporal_provenance_and_review_controls() -> None:
    ontology = _manifest("context-ontology-v0.json")
    constraints = ontology["relation_constraints"]

    assert {
        "site_id",
        "valid_time",
        "recorded_at",
        "source_system",
        "evidence_refs",
        "confidence",
        "status",
    } <= set(constraints["required_properties"])
    assert constraints["vector_similarity_creates_formal_relation"] is False
    assert constraints["uncertain_entity_merge"] == "review_case_required"
    assert constraints["cross_site_relations_allowed"] is False
    assert constraints["provenance_alignment"] == "W3C PROV-O"


def test_context_ontology_defines_valid_endpoints_for_every_relation_type() -> None:
    ontology = _manifest("context-ontology-v0.json")
    node_types = set(ontology["node_types"])
    relation_types = set(ontology["relation_types"])
    definitions = ontology["relation_definitions"]
    by_type = {definition["relation_type"]: definition for definition in definitions}

    assert len(definitions) == len(by_type) == 18
    assert set(by_type) == relation_types
    assert by_type["EXECUTED_AS"]["source_types"] == ["ActionProposal"]
    assert by_type["EXECUTED_AS"]["target_types"] == ["ActionExecution"]
    for definition in definitions:
        assert definition["source_types"]
        assert definition["target_types"]
        assert set(definition["source_types"]) <= node_types
        assert set(definition["target_types"]) <= node_types


def test_metrics_registry_entries_validate_and_are_uniquely_versioned() -> None:
    registry = _manifest("metrics-registry-v0.json")
    validator = load_validator("metric-definition.schema.json")

    keys: set[tuple[str, str]] = set()
    for definition in registry["metrics"]:
        validator.validate(definition)
        key = (definition["metric_key"], definition["definition_version"])
        assert key not in keys
        keys.add(key)
    assert registry["registry_version"] == "0.1.0"
    assert registry["official_interface"] == "Metrics API"
    assert registry["llm_calculation_allowed"] is False
    assert registry["arbitrary_query_allowed"] is False
    assert registry["runtime_enabled"] is False
    assert registry["synthetic_only"] is True
    assert len(keys) >= 3
