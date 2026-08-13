from __future__ import annotations

from .models import AuditEvent, TenantScope
from .repository import AuditRepository


class AuditService:
    def __init__(self, repository: AuditRepository) -> None:
        self.repository = repository

    def append(self, scope: TenantScope, event: AuditEvent) -> AuditEvent:
        return self.repository.append(scope, event)
