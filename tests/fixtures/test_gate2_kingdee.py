from __future__ import annotations

import ast
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

REPO_ROOT = Path(__file__).parents[2]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))
CONTRACTS_DIR = REPO_ROOT / "contracts"
GATE2_CONTRACTS_DIR = CONTRACTS_DIR / "gate2"
GATE2_EXAMPLES_DIR = CONTRACTS_DIR / "examples" / "gate2"
GATE2_FIXTURES_DIR = REPO_ROOT / "fixtures" / "kingdee" / "gate2"

LOGICAL_OBJECTS = {
    "material",
    "customer",
    "supplier",
    "sales_order",
    "purchase_order",
    "inventory",
    "receivable",
}
READ_TOOLS = {f"kingdee.{logical_object}.get" for logical_object in LOGICAL_OBJECTS}
FORBIDDEN_REQUEST_KEYS = {
    "form",
    "form_id",
    "field",
    "fields",
    "field_keys",
    "filter",
    "filters",
    "order",
    "order_by",
    "sql",
    "doctype",
    "url",
    "host",
    "method",
}
WRITER_OPERATIONS = (
    "kingdee.material.save",
    "kingdee.sales_order.create",
    "kingdee.sales_order.update",
    "kingdee.sales_order.submit",
    "kingdee.sales_order.audit",
    "kingdee.sales_order.unaudit",
    "kingdee.sales_order.delete",
    "kingdee.receivable.payment",
    "arbitrary_sql",
    "arbitrary_doctype",
)


def read_json(path: Path) -> Any:
    assert path.is_file(), f"missing Gate 2 Kingdee asset: {path.relative_to(REPO_ROOT)}"
    return json.loads(path.read_text(encoding="utf-8"))


def projection_validator() -> Draft202012Validator:
    schema = read_json(CONTRACTS_DIR / "kingdee-read-projection.schema.json")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def test_gate2_field_dictionary_covers_exact_objects_and_marks_candidates_unverified() -> None:
    dictionary = read_json(GATE2_CONTRACTS_DIR / "kingdee-field-dictionary-v0.json")

    assert dictionary["dictionary_version"] == "0.1.0"
    assert dictionary["gate"] == 2
    assert dictionary["mode"] == "design_only"
    assert dictionary["synthetic"] is True
    assert {item["logical_object"] for item in dictionary["objects"]} == LOGICAL_OBJECTS

    for logical_object in dictionary["objects"]:
        assert logical_object["candidate_form"]["verification_status"] == (
            "gate5_metadata_required"
        )
        assert logical_object["candidate_form"]["value"]
        assert logical_object["fields"]
        for field in logical_object["fields"]:
            assert field["logical_name"]
            assert field["candidate_field"]["value"]
            assert field["candidate_field"]["verification_status"] == ("gate5_metadata_required")


def test_gate2_allowlist_freezes_exact_mock_only_zero_transport_surface() -> None:
    allowlist = read_json(GATE2_CONTRACTS_DIR / "kingdee-query-allowlist-v0.json")

    assert allowlist["allowlist_version"] == "0.1.0"
    assert allowlist["gate"] == 2
    assert allowlist["network_allowed"] is False
    assert allowlist["credentials_allowed"] is False
    assert allowlist["max_rows"] == 50
    assert set(allowlist["forbidden_request_keys"]) >= FORBIDDEN_REQUEST_KEYS
    assert set(allowlist["forbidden_operation_tokens"]) >= {
        "create",
        "update",
        "save",
        "submit",
        "audit",
        "unaudit",
        "delete",
        "payment",
        "write",
        "execute",
    }
    assert {tool["name"] for tool in allowlist["tools"]} == READ_TOOLS
    assert {tool["logical_object"] for tool in allowlist["tools"]} == LOGICAL_OBJECTS
    assert all(tool["mode"] == "mock_only" for tool in allowlist["tools"])
    assert all(tool["runtime_enabled"] is False for tool in allowlist["tools"])


def test_gate2_mcp_manifest_keeps_kingdee_read_disabled_until_gate5() -> None:
    manifest = read_json(GATE2_CONTRACTS_DIR / "mcp-tool-manifest-v0.json")

    assert manifest["manifest_version"] == "0.1.0"
    assert manifest["gate"] == 2
    assert manifest["protocol_target"] == "2026-07-28"
    assert len(manifest["scopes"]) == 1
    scope = manifest["scopes"][0]
    assert scope["name"] == "kingdee-read"
    assert scope["earliest_live_gate"] == 5
    assert scope["gate2_status"] == "disabled"
    assert scope["enabled"] is False
    assert scope["runtime_enabled"] is False
    assert {tool["name"] for tool in manifest["tools"]} == READ_TOOLS
    assert all(tool["scope"] == "kingdee-read" for tool in manifest["tools"])
    assert all(tool["required_scope"] == "kingdee-read" for tool in manifest["tools"])
    assert all(tool["mutates"] is False for tool in manifest["tools"])
    assert all(tool["runtime_enabled"] is False for tool in manifest["tools"])
    assert all(tool["gate2_enabled"] is False for tool in manifest["tools"])
    assert all(tool["mock_only"] is True for tool in manifest["tools"])
    assert all(tool["earliest_live_gate"] == 5 for tool in manifest["tools"])
    assert all(tool["earliest_runtime_gate"] == 5 for tool in manifest["tools"])


def test_gate2_projection_examples_validate_and_disclose_synthetic_controls() -> None:
    examples = sorted(GATE2_EXAMPLES_DIR.glob("kingdee-*-projection.json"))

    assert len(examples) == len(LOGICAL_OBJECTS)
    assert {read_json(path)["logical_object"] for path in examples} == LOGICAL_OBJECTS
    validator = projection_validator()
    for path in examples:
        projection = read_json(path)
        validator.validate(projection)
        assert projection["site_id"] == "gbos.localhost"
        assert projection["account_set_ref"] == "account-set-synthetic-gate2"
        assert projection["dictionary_version"] == "0.1.0"
        assert projection["allowlist_version"] == "0.1.0"
        assert projection["crosswalk_status"] == "synthetic_only"
        assert projection["evidence_status"] == "synthetic_fixture_only"
        assert projection["controls"] == {
            "network_allowed": False,
            "network_calls": 0,
            "credentials_allowed": False,
            "credentials_accessed": False,
            "subprocess_calls": 0,
        }


def test_gate2_adapter_returns_deterministic_schema_valid_projections() -> None:
    from fixtures.kingdee.gate2.adapter import Gate2KingdeeMock

    adapter = Gate2KingdeeMock()
    validator = projection_validator()
    observed_objects: set[str] = set()

    for tool_name in sorted(READ_TOOLS):
        request = adapter.default_request(tool_name=tool_name, limit=2)
        first = adapter.invoke(tool_name, request)
        second = adapter.invoke(tool_name, request)

        assert first == second
        validator.validate(first)
        assert first["tool_name"] == tool_name
        assert first["site_id"] == "gbos.localhost"
        assert first["account_set_ref"] == "account-set-synthetic-gate2"
        assert first["query_time"] == "2026-08-06T00:00:00Z"
        assert first["page"]["returned_rows"] <= 2
        assert all(row["synthetic"] is True for row in first["rows"])
        observed_objects.add(first["logical_object"])

    assert observed_objects == LOGICAL_OBJECTS


@pytest.mark.parametrize(
    ("tool_name", "request_update"),
    [
        ("kingdee.unknown.get", {}),
        ("kingdee.material.get", {"logical_object": "unknown"}),
        ("kingdee.material.get", {"form_id": "BD_MATERIAL"}),
        ("kingdee.material.get", {"field_keys": ["FNumber"]}),
        ("kingdee.material.get", {"fields": ["material_number"]}),
        ("kingdee.material.get", {"filter": "FNumber='unsafe'"}),
        ("kingdee.material.get", {"filters": {"material_number": "unsafe"}}),
        ("kingdee.material.get", {"order_by": "FNumber asc"}),
        ("kingdee.material.get", {"sql": "select 1"}),
        ("kingdee.material.get", {"doctype": "GBOS External Crosswalk"}),
        ("kingdee.material.get", {"url": "https://example.invalid"}),
        ("kingdee.material.get", {"host": "example.invalid"}),
        ("kingdee.material.get", {"method": "POST"}),
        ("kingdee.material.get", {"limit": 51}),
        ("kingdee.material.get", {"limit": 0}),
        ("kingdee.material.get", {"offset": -1}),
        ("kingdee.material.get", {"request_id": "REQUEST-SYNTHETIC-UPPERCASE"}),
        ("kingdee.material.get", {"request_id": "request/synthetic/path"}),
    ],
)
def test_gate2_adapter_fails_closed_for_unknown_raw_or_over_budget_requests(
    tool_name: str,
    request_update: dict[str, Any],
) -> None:
    from fixtures.kingdee.gate2.adapter import Gate2KingdeeMock

    adapter = Gate2KingdeeMock()
    request = adapter.default_request(tool_name="kingdee.material.get")
    request.update(request_update)

    with pytest.raises(ValueError):
        adapter.invoke(tool_name, request)


@pytest.mark.parametrize("operation", WRITER_OPERATIONS)
def test_gate2_adapter_rejects_every_writer_shaped_operation(operation: str) -> None:
    from fixtures.kingdee.gate2.adapter import Gate2KingdeeMock

    adapter = Gate2KingdeeMock()
    request = adapter.default_request(tool_name="kingdee.material.get")

    with pytest.raises(ValueError):
        adapter.invoke(operation, request)


def _qualified_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _qualified_name(node.value)
        return f"{owner}.{node.attr}" if owner else node.attr
    return ""


def test_gate2_adapter_ast_has_no_transport_credentials_or_process_escape_hatches() -> None:
    adapter_path = GATE2_FIXTURES_DIR / "adapter.py"
    source = adapter_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_roots = {
        "aiohttp",
        "boto3",
        "ftplib",
        "http",
        "httpx",
        "keyring",
        "os",
        "requests",
        "socket",
        "ssl",
        "subprocess",
        "urllib",
    }
    forbidden_calls = {
        "open",
        "eval",
        "exec",
        "compile",
        "__import__",
    }

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(
                alias.name.split(".", maxsplit=1)[0] not in forbidden_roots for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".", maxsplit=1)[0] not in forbidden_roots
        elif isinstance(node, ast.Call):
            assert _qualified_name(node.func).split(".", maxsplit=1)[0] not in forbidden_roots
            assert _qualified_name(node.func) not in forbidden_calls


def test_gate2_adapter_runtime_tripwire_observes_zero_network_credentials_and_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fixtures.kingdee.gate2.adapter import Gate2KingdeeMock

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Gate 2 adapter attempted a forbidden external capability")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "getenv", forbidden)

    adapter = Gate2KingdeeMock()
    request = adapter.default_request(tool_name="kingdee.inventory.get")
    result = adapter.invoke("kingdee.inventory.get", request)

    assert result["controls"]["network_calls"] == 0
    assert result["controls"]["credentials_accessed"] is False
    assert result["controls"]["subprocess_calls"] == 0


def test_projection_schema_rejects_unmeasured_external_capability_claims() -> None:
    projection = read_json(GATE2_EXAMPLES_DIR / "kingdee-material-projection.json")
    projection["controls"]["network_calls"] = 1

    with pytest.raises(ValidationError):
        projection_validator().validate(projection)


def test_projection_schema_rejects_tool_and_logical_object_mismatch() -> None:
    projection = read_json(GATE2_EXAMPLES_DIR / "kingdee-material-projection.json")
    projection["logical_object"] = "customer"

    with pytest.raises(ValidationError):
        projection_validator().validate(projection)
