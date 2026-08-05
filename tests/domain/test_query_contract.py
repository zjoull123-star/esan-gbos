from __future__ import annotations

from pathlib import Path

import pytest
from esan_gbos.domain.query import (
    CursorError,
    decode_cursor,
    encode_cursor,
    validate_work_filters,
)


def test_cursor_round_trip_is_opaque() -> None:
    cursor = encode_cursor("2026-08-06 12:00:00.000001", "WRK-01")

    assert decode_cursor(cursor) == ("2026-08-06 12:00:00.000001", "WRK-01")
    assert "WRK-01" not in cursor


def test_invalid_cursor_is_rejected() -> None:
    with pytest.raises(CursorError, match="invalid cursor"):
        decode_cursor("not-a-cursor")


def test_work_filters_use_a_fixed_allowlist() -> None:
    assert validate_work_filters(
        {"team": "TEM-01", "business_status": "Open", "priority": "High"}
    ) == {
        "team": "TEM-01",
        "business_status": "Open",
        "priority": "High",
    }


def test_work_filters_reject_sql_or_arbitrary_frappe_filters() -> None:
    with pytest.raises(ValueError, match="unsupported filters: doctype"):
        validate_work_filters({"doctype": "Sales Order"})


def test_all_fixture_backed_query_dtos_expose_origin_for_demo_labelling() -> None:
    api_root = Path(__file__).parents[2] / "apps" / "esan_gbos" / "esan_gbos" / "api" / "v1"

    for module in ("party.py", "work_item.py", "sample.py", "sourcing.py"):
        assert '"origin"' in (api_root / module).read_text(encoding="utf-8")
