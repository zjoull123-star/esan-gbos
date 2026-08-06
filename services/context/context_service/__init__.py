"""Gate 3 Context Service persistence primitives.

This package intentionally exposes proposal and evidence metadata only. Gate 4
facts, decisions, actions, commands, and review workflows do not belong here.
"""

from .models import (
    GovernedEnvelope,
    IdempotencyConflict,
    RecordKind,
    RecordMetadata,
    TenantScope,
    ValidationError,
)
from .repositories import InMemoryContextRepository

__all__ = [
    "GovernedEnvelope",
    "IdempotencyConflict",
    "InMemoryContextRepository",
    "RecordKind",
    "RecordMetadata",
    "TenantScope",
    "ValidationError",
]
