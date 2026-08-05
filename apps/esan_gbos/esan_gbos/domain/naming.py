from __future__ import annotations

import secrets
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode_crockford(value: int, length: int) -> str:
    result = ["0"] * length
    for index in range(length - 1, -1, -1):
        value, remainder = divmod(value, 32)
        result[index] = _CROCKFORD[remainder]
    return "".join(result)


def make_ulid() -> str:
    timestamp_ms = int(time.time_ns() // 1_000_000) & ((1 << 48) - 1)
    randomness = secrets.randbits(80)
    return _encode_crockford((timestamp_ms << 80) | randomness, 26)


def make_gbos_name(prefix: str) -> str:
    normalized = prefix.strip().upper()
    if not normalized or not normalized.isascii() or not normalized.isalnum():
        raise ValueError("prefix must be non-empty ASCII alphanumeric text")
    return f"{normalized}-{make_ulid()}"
