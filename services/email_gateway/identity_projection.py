from __future__ import annotations

from .models import IdentityProjection, TenantScope
from .repository import IdentityProjectionRepository


class IdentityProjectionService:
    def __init__(self, repository: IdentityProjectionRepository) -> None:
        self.repository = repository

    def apply(self, scope: TenantScope, projection: IdentityProjection) -> IdentityProjection:
        return self.repository.apply(scope, projection)

    def get(self, scope: TenantScope, opaque_address_ref: str) -> IdentityProjection | None:
        return self.repository.get(scope, opaque_address_ref)
