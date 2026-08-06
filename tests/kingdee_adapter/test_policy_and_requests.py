from __future__ import annotations

import ast
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from services.kingdee_adapter import (
    AuthContext,
    FrozenKingdeePolicy,
    KingdeeAdapter,
    RequestRejected,
)

READ_TOOL_TO_OBJECT = {
    "kingdee.material.get": "material",
    "kingdee.customer.get": "customer",
    "kingdee.supplier.get": "supplier",
    "kingdee.sales_order.get": "sales_order",
    "kingdee.purchase_order.get": "purchase_order",
    "kingdee.inventory.get": "inventory",
    "kingdee.receivable.get": "receivable",
}


def request_for(logical_object: str) -> dict[str, object]:
    return {
        "request_id": f"request-gate5-synthetic-{logical_object.replace('_', '-')}",
        "site_id": "gbos.localhost",
        "account_set_ref": "account-set-synthetic-gate5",
        "processing_purpose": f"governed_metric_{logical_object}_lookup",
        "logical_object": logical_object,
        "limit": 2,
        "offset": 0,
        "timeout_ms": 1_000,
    }


def test_policy_loads_exact_tools_and_contract_derived_fields(
    policy: FrozenKingdeePolicy,
) -> None:
    assert policy.read_tools == frozenset(READ_TOOL_TO_OBJECT)
    assert policy.tools == frozenset({"metadata.get", *READ_TOOL_TO_OBJECT})
    assert policy.max_rows == 50
    assert policy.allowlist_version == "0.1.0"
    assert policy.dictionary_version == "0.1.0"

    material = policy.plan_for("kingdee.material.get", "material")
    assert material.tool_name == "kingdee.material.get"
    assert material.logical_object == "material"
    assert material.fields == (
        "material_number",
        "material_name",
        "document_status",
    )
    assert material.filters == ()
    assert material.order == (("material_number", "asc"),)


def test_metadata_is_the_only_extra_tool(policy: FrozenKingdeePolicy) -> None:
    metadata = policy.plan_for("metadata.get", "inventory")

    assert metadata.tool_name == "metadata.get"
    assert metadata.logical_object == "inventory"
    assert metadata.fields == ("material_number", "warehouse_number", "base_quantity")


@pytest.mark.parametrize(("tool_name", "logical_object"), READ_TOOL_TO_OBJECT.items())
def test_exact_read_tool_object_pairs_are_accepted(
    adapter: KingdeeAdapter,
    auth: AuthContext,
    tool_name: str,
    logical_object: str,
) -> None:
    result = adapter.invoke(tool_name, request_for(logical_object), auth=auth)

    assert result.status == "available"
    assert result.tool_name == tool_name
    assert result.logical_object == logical_object


@pytest.mark.parametrize(
    ("tool_name", "logical_object"),
    [
        ("kingdee.material.get", "customer"),
        ("kingdee.customer.get", "material"),
        ("kingdee.unknown.get", "material"),
        ("kingdee.material.list", "material"),
        ("metadata.get", "unknown"),
    ],
)
def test_unknown_or_mismatched_tool_object_pairs_fail_closed(
    adapter: KingdeeAdapter,
    auth: AuthContext,
    tool_name: str,
    logical_object: str,
) -> None:
    with pytest.raises(RequestRejected):
        adapter.invoke(tool_name, request_for(logical_object), auth=auth)


@pytest.mark.parametrize(
    "unknown_key",
    [
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
        "token",
        "access_token",
        "destination",
        "headers",
        "body",
    ],
)
def test_raw_query_transport_and_secret_passthrough_keys_are_rejected(
    adapter: KingdeeAdapter,
    auth: AuthContext,
    material_request: Mapping[str, Any],
    unknown_key: str,
) -> None:
    request = dict(material_request)
    request[unknown_key] = "unsafe"

    with pytest.raises(RequestRejected, match="request keys"):
        adapter.invoke("kingdee.material.get", request, auth=auth)


def test_missing_required_request_key_is_rejected(
    adapter: KingdeeAdapter,
    auth: AuthContext,
    material_request: Mapping[str, Any],
) -> None:
    for key in material_request:
        request = dict(material_request)
        del request[key]

        with pytest.raises(RequestRejected, match="request keys"):
            adapter.invoke("kingdee.material.get", request, auth=auth)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("request_id", ""),
        ("request_id", "request/escape"),
        ("site_id", ""),
        ("site_id", "https://internal.invalid"),
        ("account_set_ref", ""),
        ("account_set_ref", "https://169.254.169.254/latest"),
        ("processing_purpose", ""),
        ("processing_purpose", "select * from users"),
        ("limit", 0),
        ("limit", 51),
        ("limit", True),
        ("offset", -1),
        ("offset", True),
        ("timeout_ms", 0),
        ("timeout_ms", 30_001),
        ("timeout_ms", True),
    ],
)
def test_identifier_and_budget_validation_is_bounded(
    adapter: KingdeeAdapter,
    auth: AuthContext,
    material_request: Mapping[str, Any],
    key: str,
    value: object,
) -> None:
    request = dict(material_request)
    request[key] = value

    with pytest.raises(RequestRejected):
        adapter.invoke("kingdee.material.get", request, auth=auth)


@pytest.mark.parametrize(
    "tool_name",
    [
        "kingdee.material.create",
        "kingdee.material.update",
        "kingdee.material.save",
        "kingdee.material.submit",
        "kingdee.material.audit",
        "kingdee.material.unaudit",
        "kingdee.material.delete",
        "kingdee.receivable.payment",
        "kingdee.execute",
        "kingdee.push",
    ],
)
def test_mutation_tokens_and_writer_tools_are_rejected_before_transport(
    adapter: KingdeeAdapter,
    auth: AuthContext,
    material_request: Mapping[str, Any],
    tool_name: str,
) -> None:
    with pytest.raises(RequestRejected, match="forbidden|unsupported"):
        adapter.invoke(tool_name, material_request, auth=auth)


def test_request_authentication_and_exact_scope_are_required(
    adapter: KingdeeAdapter,
    material_request: Mapping[str, Any],
) -> None:
    unauthenticated = AuthContext(authenticated=False, granted_scopes=("kingdee-read",))
    wrong_scope = AuthContext(authenticated=True, granted_scopes=("gbos-read",))
    empty_scope = AuthContext(authenticated=True, granted_scopes=())

    with pytest.raises(RequestRejected, match="authenticated"):
        adapter.invoke("kingdee.material.get", material_request, auth=unauthenticated)
    with pytest.raises(RequestRejected, match="kingdee-read"):
        adapter.invoke("kingdee.material.get", material_request, auth=wrong_scope)
    with pytest.raises(RequestRejected, match="kingdee-read"):
        adapter.invoke("kingdee.material.get", material_request, auth=empty_scope)


def test_auth_context_has_no_token_or_secret_surface() -> None:
    assert set(AuthContext.__dataclass_fields__) == {"authenticated", "granted_scopes"}


def test_runtime_package_never_imports_gate2_fixture_package() -> None:
    package = Path(__file__).parents[2] / "services" / "kingdee_adapter"

    for source_path in package.glob("*.py"):
        source = source_path.read_text(encoding="utf-8")
        assert "fixtures.kingdee.gate2" not in source
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(not name.name.startswith("fixtures") for name in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("fixtures")
