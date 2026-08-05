"""Controlled, read-only Kingdee K3Cloud mock for Gate 1.

The mock is intentionally self-contained and never opens a network connection.
Every request and response carries synthetic identifiers plus a fixed demo
timestamp, making recordings suitable for deterministic tests and audit.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

DEMO_TIME = "2026-08-06T00:00:00Z"
SYSTEM_NAME = "kingdee-gate1-synthetic"
DEFAULT_TENANT = "tenant-gate1-synthetic"
DEFAULT_USER = "user-gate1-synthetic"

READ_ONLY_METHODS: frozenset[str] = frozenset(
    {
        "query_metadata",
        "query_bill",
        "query_bill_json",
        "count_bill",
        "query_bill_all",
        "query_bill_to_file",
        "query_bill_range",
        "view_bill",
    }
)

# This is a request-shape allowlist, not an ORM or HTTP escape hatch.  Unknown
# keys are rejected so a future writer-shaped option cannot be smuggled in.
REQUEST_FIELDS: tuple[str, ...] = (
    "request_id",
    "user_id",
    "tenant_id",
    "form_id",
    "field_keys",
    "filter",
    "order_by",
    "limit",
    "start_row",
    "permission_context",
)
REQUEST_FIELD_SET = frozenset(REQUEST_FIELDS)
DEFAULT_FIELD_KEYS: tuple[str, ...] = ("FNumber", "FName", "FDocumentStatus")

DEFAULT_REQUEST: dict[str, Any] = {
    "request_id": "req-gate1-synthetic-0001",
    "user_id": DEFAULT_USER,
    "tenant_id": DEFAULT_TENANT,
    "form_id": "BD_MATERIAL",
    "field_keys": list(DEFAULT_FIELD_KEYS),
    "filter": {},
    "order_by": "FNumber asc",
    "limit": 50,
    "start_row": 0,
    "permission_context": {"allow_read": True, "site_id": "gbos.localhost"},
}

_FORM_ROWS: dict[str, tuple[dict[str, Any], ...]] = {
    "BD_MATERIAL": tuple(
        {
            "FNumber": f"KD-SYNTH-MATERIAL-{index:04d}",
            "FName": f"Synthetic Material {index:04d}",
            "FDocumentStatus": "C",
            "FDescription": "Gate 1 synthetic read-only material",
        }
        for index in range(1, 13)
    ),
    "SAL_SaleOrder": tuple(
        {
            "FNumber": f"KD-SYNTH-SALE-{index:04d}",
            "FName": f"Synthetic Sale Snapshot {index:04d}",
            "FDocumentStatus": "C",
            "FDescription": "Gate 1 synthetic read-only sales snapshot",
        }
        for index in range(1, 7)
    ),
}


def _ensure_synthetic_identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty synthetic string")
    lowered = value.lower()
    if any(token in lowered for token in ("@gmail.", "@qq.", "@163.", "password", "secret")):
        raise ValueError(f"{field_name} cannot contain personal or credential data")
    return value


class KingdeeMock:
    """Allowlisted read methods with deterministic response envelopes."""

    read_only_methods = READ_ONLY_METHODS
    request_fields = REQUEST_FIELDS

    @staticmethod
    def default_request(
        *,
        request_id: str = DEFAULT_REQUEST["request_id"],
        form_id: str = DEFAULT_REQUEST["form_id"],
        field_keys: list[str] | tuple[str, ...] = DEFAULT_FIELD_KEYS,
        limit: int = DEFAULT_REQUEST["limit"],
        start_row: int = DEFAULT_REQUEST["start_row"],
    ) -> dict[str, Any]:
        request = copy.deepcopy(DEFAULT_REQUEST)
        request.update(
            {
                "request_id": request_id,
                "form_id": form_id,
                "field_keys": list(field_keys),
                "limit": limit,
                "start_row": start_row,
            }
        )
        return request

    def invoke(self, method: str, request: Mapping[str, Any]) -> dict[str, Any]:
        if method not in READ_ONLY_METHODS:
            raise ValueError("unsupported Kingdee operation")
        normalized = self._validate_request(request)
        rows = self._rows_for(method, normalized)
        return self._response(normalized, rows)

    # ``dispatch`` and ``handle_request`` are aliases for connector adapters.
    dispatch = invoke
    handle_request = invoke

    def query_metadata(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self.invoke("query_metadata", request)

    def query_bill(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self.invoke("query_bill", request)

    def query_bill_json(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self.invoke("query_bill_json", request)

    def count_bill(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self.invoke("count_bill", request)

    def query_bill_all(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self.invoke("query_bill_all", request)

    def query_bill_to_file(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self.invoke("query_bill_to_file", request)

    def query_bill_range(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self.invoke("query_bill_range", request)

    def view_bill(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self.invoke("view_bill", request)

    def _validate_request(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(request, Mapping):
            raise ValueError("request must be an object")
        if set(request) != REQUEST_FIELD_SET:
            missing = sorted(REQUEST_FIELD_SET - set(request))
            unknown = sorted(set(request) - REQUEST_FIELD_SET)
            detail = []
            if missing:
                detail.append(f"missing={missing}")
            if unknown:
                detail.append(f"unknown={unknown}")
            raise ValueError("invalid request fields: " + ", ".join(detail))

        normalized = dict(request)
        for field_name in ("request_id", "user_id", "tenant_id", "form_id"):
            _ensure_synthetic_identifier(normalized[field_name], field_name)
        field_keys = normalized["field_keys"]
        if not isinstance(field_keys, (list, tuple)) or not 1 <= len(field_keys) <= 50:
            raise ValueError("field_keys must contain 1..50 entries")
        if any(not isinstance(field, str) or not field for field in field_keys):
            raise ValueError("field_keys must contain non-empty strings")
        if not isinstance(normalized["filter"], (Mapping, list, tuple)):
            raise ValueError("filter must be an object or list")
        if not isinstance(normalized["order_by"], (str, list, tuple)):
            raise ValueError("order_by must be a string or list")
        limit = normalized["limit"]
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("limit must be an integer in 1..500")
        start_row = normalized["start_row"]
        if not isinstance(start_row, int) or isinstance(start_row, bool) or start_row < 0:
            raise ValueError("start_row must be a non-negative integer")
        permission_context = normalized["permission_context"]
        if not isinstance(permission_context, Mapping):
            raise ValueError("permission_context must be an object")
        if permission_context.get("allow_read") is not True:
            raise ValueError("permission_context.allow_read must be true")
        normalized["field_keys"] = list(field_keys)
        return normalized

    def _rows_for(self, method: str, request: Mapping[str, Any]) -> list[dict[str, Any]]:
        form_id = request["form_id"]
        field_keys = request["field_keys"]
        source_rows = list(_FORM_ROWS.get(form_id, ()))
        if method == "query_metadata":
            return [
                {
                    "field_key": field_key,
                    "label": f"Synthetic {field_key}",
                    "data_type": "string",
                }
                for field_key in field_keys
            ]
        if method == "count_bill":
            return [{"count": len(source_rows)}]
        if method == "view_bill":
            filter_value = request["filter"]
            requested_number = (
                filter_value.get("FNumber") if isinstance(filter_value, Mapping) else None
            )
            if requested_number:
                source_rows = [row for row in source_rows if row["FNumber"] == requested_number]
            source_rows = source_rows[:1]
        return [{key: row.get(key, None) for key in field_keys} for row in source_rows]

    @staticmethod
    def _response(request: Mapping[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
        limit = request["limit"]
        start_row = request["start_row"]
        if rows and set(rows[0]) == {"count"}:
            page_rows = rows
            has_more = False
        else:
            page_rows = rows[start_row : start_row + limit]
            has_more = start_row + limit < len(rows)
        columns = list(page_rows[0]) if page_rows else list(request["field_keys"])
        return {
            "success": True,
            "request_id": request["request_id"],
            "source": {
                "system": SYSTEM_NAME,
                "form_id": request["form_id"],
                "query_time": DEMO_TIME,
            },
            "columns": columns,
            "rows": page_rows,
            "page": {"limit": limit, "start_row": start_row, "has_more": has_more},
        }


def handle_request(method: str, request: Mapping[str, Any]) -> dict[str, Any]:
    """Module-level adapter used by simple fixture runners."""

    return KingdeeMock().invoke(method, request)
