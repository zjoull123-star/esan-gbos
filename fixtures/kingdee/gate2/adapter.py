"""Deterministic Gate 2 Kingdee projection adapter.

This module only maps in-memory synthetic rows into the frozen projection
contract. It has no transport, credential, process, or dynamic query surface.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping

DEMO_TIME = "2026-08-06T00:00:00Z"
SOURCE_SYSTEM = "kingdee-gate2-synthetic"
DEFAULT_SITE_ID = "gbos.localhost"
SYNTHETIC_ACCOUNT_SET_REF = "account-set-synthetic-gate2"
DICTIONARY_VERSION = "0.1.0"
ALLOWLIST_VERSION = "0.1.0"
MAX_ROWS = 50

TOOL_TO_OBJECT: dict[str, str] = {
    "kingdee.material.get": "material",
    "kingdee.customer.get": "customer",
    "kingdee.supplier.get": "supplier",
    "kingdee.sales_order.get": "sales_order",
    "kingdee.purchase_order.get": "purchase_order",
    "kingdee.inventory.get": "inventory",
    "kingdee.receivable.get": "receivable",
}
READ_TOOLS: frozenset[str] = frozenset(TOOL_TO_OBJECT)
REQUEST_FIELDS: tuple[str, ...] = (
    "request_id",
    "site_id",
    "account_set_ref",
    "limit",
    "offset",
)
REQUEST_FIELD_SET = frozenset(REQUEST_FIELDS)

type Scalar = str | int | float | bool | None
type FixtureRow = dict[str, Scalar]
type Projection = dict[str, object]

_ROWS: dict[str, tuple[FixtureRow, ...]] = {
    "material": (
        {
            "record_ref": "material-synthetic-0001",
            "material_number": "MAT-SYNTH-0001",
            "material_name": "Synthetic Fragrance Base",
            "document_status": "synthetic-approved",
        },
        {
            "record_ref": "material-synthetic-0002",
            "material_number": "MAT-SYNTH-0002",
            "material_name": "Synthetic Packaging Component",
            "document_status": "synthetic-approved",
        },
        {
            "record_ref": "material-synthetic-0003",
            "material_number": "MAT-SYNTH-0003",
            "material_name": "Synthetic Sample Material",
            "document_status": "synthetic-draft",
        },
    ),
    "customer": (
        {
            "record_ref": "customer-synthetic-0001",
            "customer_number": "CUS-SYNTH-0001",
            "customer_name": "Synthetic Customer One",
            "document_status": "synthetic-approved",
        },
        {
            "record_ref": "customer-synthetic-0002",
            "customer_number": "CUS-SYNTH-0002",
            "customer_name": "Synthetic Customer Two",
            "document_status": "synthetic-approved",
        },
    ),
    "supplier": (
        {
            "record_ref": "supplier-synthetic-0001",
            "supplier_number": "SUP-SYNTH-0001",
            "supplier_name": "Synthetic Supplier One",
            "document_status": "synthetic-approved",
        },
        {
            "record_ref": "supplier-synthetic-0002",
            "supplier_number": "SUP-SYNTH-0002",
            "supplier_name": "Synthetic Supplier Two",
            "document_status": "synthetic-approved",
        },
    ),
    "sales_order": (
        {
            "record_ref": "sales_order-synthetic-0001",
            "order_number": "SO-SYNTH-0001",
            "customer_number": "CUS-SYNTH-0001",
            "order_date": "2026-08-01",
            "total_amount": 12500.0,
        },
        {
            "record_ref": "sales_order-synthetic-0002",
            "order_number": "SO-SYNTH-0002",
            "customer_number": "CUS-SYNTH-0002",
            "order_date": "2026-08-02",
            "total_amount": 8100.0,
        },
    ),
    "purchase_order": (
        {
            "record_ref": "purchase_order-synthetic-0001",
            "order_number": "PO-SYNTH-0001",
            "supplier_number": "SUP-SYNTH-0001",
            "order_date": "2026-08-01",
            "total_amount": 4900.0,
        },
        {
            "record_ref": "purchase_order-synthetic-0002",
            "order_number": "PO-SYNTH-0002",
            "supplier_number": "SUP-SYNTH-0002",
            "order_date": "2026-08-03",
            "total_amount": 3100.0,
        },
    ),
    "inventory": (
        {
            "record_ref": "inventory-synthetic-0001",
            "material_number": "MAT-SYNTH-0001",
            "warehouse_number": "WH-SYNTH-0001",
            "base_quantity": 1200.0,
        },
        {
            "record_ref": "inventory-synthetic-0002",
            "material_number": "MAT-SYNTH-0002",
            "warehouse_number": "WH-SYNTH-0001",
            "base_quantity": 5400.0,
        },
    ),
    "receivable": (
        {
            "record_ref": "receivable-synthetic-0001",
            "bill_number": "AR-SYNTH-0001",
            "customer_number": "CUS-SYNTH-0001",
            "due_date": "2026-08-31",
            "open_amount": 4200.0,
        },
        {
            "record_ref": "receivable-synthetic-0002",
            "bill_number": "AR-SYNTH-0002",
            "customer_number": "CUS-SYNTH-0002",
            "due_date": "2026-09-15",
            "open_amount": 1800.0,
        },
    ),
}


def _synthetic_request_id(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError("request_id must be a non-empty synthetic string")
    lowered = value.lower()
    forbidden = ("password", "secret", "token", "@gmail.", "@qq.", "@163.")
    allowed_characters = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_")
    if (
        value != lowered
        or not value.startswith("request-")
        or not set(value) <= allowed_characters
        or "synthetic" not in value
        or any(token in value for token in forbidden)
    ):
        raise ValueError("request_id must contain only synthetic, non-credential data")
    return value


class Gate2KingdeeMock:
    """Exact seven-tool, read-only mock with fixed synthetic projections."""

    read_tools = READ_TOOLS
    request_fields = REQUEST_FIELDS

    @staticmethod
    def default_request(
        *,
        tool_name: str,
        request_id: str = "request-gate2-synthetic-0001",
        limit: int = MAX_ROWS,
        offset: int = 0,
    ) -> dict[str, object]:
        if tool_name not in READ_TOOLS:
            raise ValueError("unsupported Gate 2 Kingdee read tool")
        return {
            "request_id": request_id,
            "site_id": DEFAULT_SITE_ID,
            "account_set_ref": SYNTHETIC_ACCOUNT_SET_REF,
            "limit": limit,
            "offset": offset,
        }

    def invoke(self, tool_name: str, request: Mapping[str, object]) -> Projection:
        if tool_name not in READ_TOOLS:
            raise ValueError("unsupported Gate 2 Kingdee read tool")
        normalized = self._validate_request(request)
        logical_object = TOOL_TO_OBJECT[tool_name]
        source_rows = _ROWS[logical_object]
        limit = normalized["limit"]
        offset = normalized["offset"]
        assert isinstance(limit, int)
        assert isinstance(offset, int)
        selected_rows = source_rows[offset : offset + limit]
        rows = [
            {
                "record_ref": row["record_ref"],
                "synthetic": True,
                "values": {
                    key: copy.deepcopy(value) for key, value in row.items() if key != "record_ref"
                },
            }
            for row in selected_rows
        ]
        request_id = normalized["request_id"]
        assert isinstance(request_id, str)
        projection_suffix = request_id.replace(".", "-").replace("_", "-")
        return {
            "schema_version": "1.0",
            "projection_id": f"projection-gate2-{logical_object}-{projection_suffix}",
            "site_id": DEFAULT_SITE_ID,
            "account_set_ref": SYNTHETIC_ACCOUNT_SET_REF,
            "logical_object": logical_object,
            "tool_name": tool_name,
            "source_system": SOURCE_SYSTEM,
            "mode": "mock_only",
            "synthetic": True,
            "dictionary_version": DICTIONARY_VERSION,
            "allowlist_version": ALLOWLIST_VERSION,
            "crosswalk_status": "synthetic_only",
            "evidence_status": "synthetic_fixture_only",
            "query_time": DEMO_TIME,
            "rows": rows,
            "page": {
                "limit": limit,
                "offset": offset,
                "returned_rows": len(rows),
                "has_more": offset + limit < len(source_rows),
            },
            "controls": {
                "network_allowed": False,
                "network_calls": 0,
                "credentials_allowed": False,
                "credentials_accessed": False,
                "subprocess_calls": 0,
            },
        }

    dispatch = invoke
    handle_request = invoke

    @staticmethod
    def _validate_request(request: Mapping[str, object]) -> dict[str, object]:
        if not isinstance(request, Mapping):
            raise ValueError("request must be an object")
        if set(request) != REQUEST_FIELD_SET:
            missing = sorted(REQUEST_FIELD_SET - set(request))
            unknown = sorted(set(request) - REQUEST_FIELD_SET)
            details: list[str] = []
            if missing:
                details.append(f"missing={missing}")
            if unknown:
                details.append(f"unknown={unknown}")
            raise ValueError("invalid Gate 2 request fields: " + ", ".join(details))

        normalized = dict(request)
        normalized["request_id"] = _synthetic_request_id(normalized["request_id"])
        if normalized["site_id"] != DEFAULT_SITE_ID:
            raise ValueError("site_id is not available in the synthetic Gate 2 fixture")
        if normalized["account_set_ref"] != SYNTHETIC_ACCOUNT_SET_REF:
            raise ValueError("account_set_ref must be the frozen synthetic reference")
        limit = normalized["limit"]
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_ROWS:
            raise ValueError("limit must be an integer in 1..50")
        offset = normalized["offset"]
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        return normalized


def handle_request(tool_name: str, request: Mapping[str, object]) -> Projection:
    """Module-level entry point for fixture runners."""

    return Gate2KingdeeMock().invoke(tool_name, request)
