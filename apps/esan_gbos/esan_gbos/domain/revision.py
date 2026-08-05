from __future__ import annotations


class RevisionConflict(ValueError):
    """Raised when a caller writes against an obsolete record revision."""


def next_revision(*, expected: int, current: int) -> int:
    if expected != current:
        raise RevisionConflict(f"expected revision {expected}, current revision {current}")
    return current + 1
