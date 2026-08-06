from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource

ROOT = Path(__file__).parents[2]
CONTRACTS = ROOT / "contracts"
GATE5 = CONTRACTS / "gate5"
EXAMPLES = CONTRACTS / "examples" / "gate5"


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _validator(name: str) -> Draft202012Validator:
    schemas = [_json(path) for path in GATE5.glob("*.schema.json")]
    registry: Registry[Any] = Registry()
    for schema in schemas:
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
    schema = next(item for item in schemas if item["$id"].endswith(f"/{name}"))
    return Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())


def test_gate5_examples_validate() -> None:
    _validator("metric-response.schema.json").validate(_json(EXAMPLES / "metric-available.json"))
    _validator("metric-response.schema.json").validate(_json(EXAMPLES / "metric-unavailable.json"))
    _validator("metrics-dashboard.schema.json").validate(_json(EXAMPLES / "metrics-dashboard.json"))


@pytest.mark.parametrize(
    ("quality_path", "value"),
    (
        (("freshness", "status"), "stale"),
        (("coverage", "status"), "insufficient"),
        (("reconciliation", "status"), "failed"),
    ),
)
def test_available_metric_fails_closed_when_a_quality_gate_fails(
    quality_path: tuple[str, str],
    value: str,
) -> None:
    metric = _json(EXAMPLES / "metric-available.json")
    metric[quality_path[0]][quality_path[1]] = value

    with pytest.raises(ValidationError):
        _validator("metric-response.schema.json").validate(metric)


def test_unavailable_metric_never_exposes_value_or_unit() -> None:
    metric = _json(EXAMPLES / "metric-unavailable.json")
    for field, value in (("value", 100), ("unit", "CNY")):
        candidate = deepcopy(metric)
        candidate[field] = value
        with pytest.raises(ValidationError):
            _validator("metric-response.schema.json").validate(candidate)


def test_dashboard_cannot_mix_synthetic_and_live_metrics() -> None:
    dashboard = _json(EXAMPLES / "metrics-dashboard.json")
    dashboard["metrics"][0]["source_mode"] = "live"
    dashboard["metrics"][0]["synthetic"] = False

    with pytest.raises(ValidationError):
        _validator("metrics-dashboard.schema.json").validate(dashboard)


def test_gate5_registry_is_exact_and_has_no_arbitrary_query_surface() -> None:
    registry = _json(GATE5 / "metrics-registry-v1.json")

    assert registry["runtime_enabled"] is True
    assert registry["synthetic_mode_enabled"] is True
    assert registry["live_mode_enabled"] is False
    assert registry["llm_calculation_allowed"] is False
    assert registry["arbitrary_query_allowed"] is False
    assert registry["allowed_query_keys"] == ["metric_key", "window_start", "window_end"]
    assert len({item["metric_key"] for item in registry["metrics"]}) == len(registry["metrics"])


def test_kingdee_gate5_policy_has_only_read_tools_and_no_writer_scope() -> None:
    policy = _json(GATE5 / "kingdee-runtime-policy-v1.json")

    assert policy["live_enabled"] is False
    assert policy["required_scope"] == "kingdee-read"
    assert "kingdee-write" not in policy["available_scopes"]
    assert len(policy["tools"]) == 8
    assert all(tool["mutates"] is False for tool in policy["tools"])
    assert all(
        tool["name"] == "kingdee.metadata.get" or tool["name"].endswith(".get")
        for tool in policy["tools"]
    )
    assert policy["forbidden_operation_tokens"]


def test_bff_v3_exposes_only_the_governed_read_dashboard() -> None:
    spec = _json(CONTRACTS / "bff-v3.openapi.json")
    path = "/api/method/esan_gbos.api.v3.metrics.dashboard"

    assert spec["openapi"] == "3.1.0"
    assert set(spec["paths"]) == {path}
    assert set(spec["paths"][path]) == {"get"}
    assert spec["paths"][path]["get"]["x-gbos-mutates"] is False
    assert "post" not in spec["paths"][path]
