from __future__ import annotations

from .models import OutboundNotAuthorized, TenantScope


class DisabledSendOutboxRepository:
    """Schema placeholder that cannot create external work before Chunk 4."""

    def __init__(self, *, outbound_enabled: bool) -> None:
        self.outbound_enabled = outbound_enabled

    def insert(self, scope: TenantScope, command: object) -> None:
        raise OutboundNotAuthorized("outbound_not_authorized")
