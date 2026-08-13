from __future__ import annotations

from typing import Protocol

from .models import AuthorityRoute, IdentityProjection, TenantScope


class FrappeRouteAuthority(Protocol):
    def resolve(
        self,
        *,
        scope: TenantScope,
        team_ref: str,
        projection: IdentityProjection,
        expected_party_revision: int | None = None,
    ) -> AuthorityRoute: ...


class MonotonicClock(Protocol):
    def __call__(self) -> float: ...
