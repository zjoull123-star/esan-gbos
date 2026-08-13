from __future__ import annotations

from datetime import datetime

from .models import ContentProjection, TenantScope, require_scope


class RetentionPlanner:
    def plan(
        self,
        scope: TenantScope,
        projections: tuple[ContentProjection, ...],
        *,
        now: datetime,
    ) -> tuple[str, ...]:
        eligible: list[str] = []
        for item in projections:
            require_scope(scope, site_id=item.site_id)
            if (
                not item.confirmed
                and item.active_draft_ref is None
                and item.observer_expiration_receipt_ref is not None
                and item.expires_at <= now
                and item.kind in {"unconfirmed_display", "unconfirmed_subject"}
            ):
                eligible.append(item.projection_ref)
        return tuple(sorted(eligible))
