from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

ROOT = Path(__file__).parents[2]
SCHEMA_PATH = ROOT / "contracts" / "gate6" / "gate6-decision.schema.json"
EXAMPLE_PATH = ROOT / "contracts" / "examples" / "gate6" / "local-production-no-go.json"


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _errors(schema: dict[str, Any], value: dict[str, Any]) -> list[str]:
    return [error.message for error in Draft202012Validator(schema).iter_errors(value)]


def test_gate6_decision_schema_and_local_no_go_example_are_valid() -> None:
    schema = _json(SCHEMA_PATH)
    example = _json(EXAMPLE_PATH)

    Draft202012Validator.check_schema(schema)
    assert _errors(schema, example) == []
    assert example["decision"]["technical_local"] == "go"
    assert example["decision"]["production"] == "no_go"
    assert example["decision"]["reason_code"] == "blocked_external_input"
    assert example["production_mutation_authorized"] is False


def test_gate6_production_go_requires_every_area_go_and_two_approvers() -> None:
    schema = _json(SCHEMA_PATH)
    example = _json(EXAMPLE_PATH)
    unsafe = copy.deepcopy(example)
    unsafe["decision"] = {
        "technical_local": "go",
        "production": "go",
        "reason_code": "all_mandatory_evidence_passed",
    }
    unsafe["production_mutation_authorized"] = True
    unsafe["production_approvers"] = [
        {
            "actor_ref": "release-owner-a",
            "role": "Release Owner",
            "approved_at": "2026-08-07T00:00:00Z",
            "approval_ref": "approval-a",
        }
    ]

    errors = _errors(schema, unsafe)
    assert errors
    assert any("go" in error or "too short" in error for error in errors)


def test_gate6_no_go_cannot_authorize_a_production_mutation() -> None:
    schema = _json(SCHEMA_PATH)
    example = _json(EXAMPLE_PATH)
    unsafe = copy.deepcopy(example)
    unsafe["production_mutation_authorized"] = True

    assert _errors(schema, unsafe)


def test_gate6_decision_has_exact_independent_areas() -> None:
    schema = _json(SCHEMA_PATH)
    example = _json(EXAMPLE_PATH)
    expected = {
        "code",
        "local_runtime",
        "preproduction",
        "kingdee_canary",
        "security",
        "privacy_cross_border",
        "uat",
        "backup_dr",
        "operations",
        "production_authorization",
    }

    assert set(example["areas"]) == expected
    extra = copy.deepcopy(example)
    extra["areas"]["informal_override"] = copy.deepcopy(extra["areas"]["code"])
    assert _errors(schema, extra)


def test_gate6_local_fixture_records_zero_external_activity() -> None:
    example = _json(EXAMPLE_PATH)

    assert all(value == 0 for value in example["external_activity"].values())
    assert {item["code"] for item in example["missing_entry_inputs"]} == {
        "live_kingdee_canary",
        "singapore_preproduction",
        "security_owner_approval",
        "privacy_cross_border_approval",
        "business_owner_uat",
        "production_authorization",
    }
