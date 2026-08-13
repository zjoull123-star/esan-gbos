from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from .models import IdentityProjection, TenantScope
from .repository import IdentityProjectionRepository

_RECEIPT_FIELDS = frozenset(
    {
        "site_id",
        "processing_purpose",
        "opaque_address_ref",
        "external_identity_ref",
        "external_identity_revision",
        "identity_type",
        "team_ref",
        "status",
        "observed_at",
    }
)


def projection_receipt(value: Mapping[str, object]) -> str:
    """Return the deterministic receipt for the closed projection fields."""

    if set(value) != _RECEIPT_FIELDS:
        raise ValueError("invalid identity projection receipt fields")
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class IdentityProjectionService:
    def __init__(self, repository: IdentityProjectionRepository) -> None:
        self.repository = repository

    def apply(self, scope: TenantScope, projection: IdentityProjection) -> IdentityProjection:
        return self.repository.apply(scope, projection)

    def get(self, scope: TenantScope, opaque_address_ref: str) -> IdentityProjection | None:
        return self.repository.get(scope, opaque_address_ref)


__all__ = ["IdentityProjectionService", "projection_receipt"]
