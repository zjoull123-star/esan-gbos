"""Focused Email Gateway persistence adapters."""

from .phase1_read import InMemoryPhase1ReadRepository, PostgresPhase1ReadRepository

__all__ = ["InMemoryPhase1ReadRepository", "PostgresPhase1ReadRepository"]
