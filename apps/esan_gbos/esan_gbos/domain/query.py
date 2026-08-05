from __future__ import annotations

import base64
import json
from collections.abc import Mapping


class CursorError(ValueError):
    """Raised when a list cursor cannot be decoded safely."""


WORK_FILTERS = frozenset({"team", "business_status", "assigned_to", "priority", "due_date"})


def encode_cursor(modified: str, name: str) -> str:
    raw = json.dumps([modified, name], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode())
        if (
            not isinstance(value, list)
            or len(value) != 2
            or not all(isinstance(item, str) and item for item in value)
        ):
            raise ValueError
        return value[0], value[1]
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError) as error:
        raise CursorError("invalid cursor") from error


def validate_work_filters(filters: Mapping[str, object] | None) -> dict[str, object]:
    if not filters:
        return {}
    unexpected = set(filters) - WORK_FILTERS
    if unexpected:
        raise ValueError(f"unsupported filters: {', '.join(sorted(unexpected))}")
    return dict(filters)
