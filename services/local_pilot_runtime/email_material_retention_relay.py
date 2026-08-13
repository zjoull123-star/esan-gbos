"""Injectable relay workers for email-material retention registration and callbacks."""

from __future__ import annotations

import hmac
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Protocol

from services.email_gateway.models import TenantScope as GatewayTenantScope
from services.email_gateway.terminal_retention import TerminalAuthorityRegistrationLease
from services.observer.observer.email_material_retention_callback import (
    EmailMaterialRetentionCallbackLease,
    EmailMaterialRetentionCallbackRepository,
)
from services.observer.observer.models import TenantScope as ObserverTenantScope

_REF = re.compile(r"^[A-Z]{3}-[0-9A-HJKMNP-TV-Z]{26}$")


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("relay clock must be timezone-aware")
    return value.astimezone(UTC)


class ObserverRegistrationTransport(Protocol):
    def register(self, payload: dict[str, object]) -> object: ...


class GatewayCallbackTransport(Protocol):
    def deliver(self, payload: dict[str, object]) -> object: ...


class GatewayRegistrationService(Protocol):
    def claim_registration(
        self,
        scope: GatewayTenantScope,
        *,
        worker_id: str,
        lease_duration: timedelta,
    ) -> TerminalAuthorityRegistrationLease | None: ...

    def ack_registration(
        self,
        scope: GatewayTenantScope,
        lease: TerminalAuthorityRegistrationLease,
        *,
        response: object,
    ) -> object: ...

    def fail_registration(
        self,
        scope: GatewayTenantScope,
        lease: TerminalAuthorityRegistrationLease,
        *,
        safe_code: str,
    ) -> None: ...


class GatewayAuthorityRegistrationRelay:
    def __init__(
        self,
        *,
        service: GatewayRegistrationService,
        transport: ObserverRegistrationTransport,
        worker_id: str,
        clock: Callable[[], datetime],
        lease_duration: timedelta = timedelta(minutes=5),
    ) -> None:
        if (
            not worker_id
            or len(worker_id) > 256
            or "@" in worker_id
            or not callable(clock)
            or not timedelta(seconds=1) <= lease_duration <= timedelta(minutes=5)
        ):
            raise ValueError("invalid gateway registration relay dependencies")
        self._service = service
        self._transport = transport
        self._worker_id = worker_id
        self._clock = clock
        self._lease_duration = lease_duration

    def __repr__(self) -> str:
        return "GatewayAuthorityRegistrationRelay(dependencies=<redacted>)"

    def run_once(self, scope: GatewayTenantScope) -> bool:
        _aware(self._clock())
        lease = self._service.claim_registration(
            scope,
            worker_id=self._worker_id,
            lease_duration=self._lease_duration,
        )
        if lease is None:
            return False
        try:
            response = self._transport.register(lease.authority.registration_wire())
            self._service.ack_registration(scope, lease, response=response)
        except Exception:
            self._service.fail_registration(
                scope,
                lease,
                safe_code="observer_registration_failed",
            )
        return True


class ObserverTombstoneCallbackRelay:
    def __init__(
        self,
        *,
        repository: EmailMaterialRetentionCallbackRepository,
        transport: GatewayCallbackTransport,
        worker_id: str,
        clock: Callable[[], datetime],
        lease_duration: timedelta = timedelta(minutes=5),
    ) -> None:
        if (
            not worker_id
            or len(worker_id) > 256
            or "@" in worker_id
            or not callable(clock)
            or not timedelta(seconds=1) <= lease_duration <= timedelta(minutes=5)
        ):
            raise ValueError("invalid observer callback relay dependencies")
        self._repository = repository
        self._transport = transport
        self._worker_id = worker_id
        self._clock = clock
        self._lease_duration = lease_duration

    def __repr__(self) -> str:
        return "ObserverTombstoneCallbackRelay(dependencies=<redacted>)"

    def run_once(self, scope: ObserverTenantScope) -> bool:
        now = _aware(self._clock())
        lease = self._repository.claim(
            scope,
            worker_id=self._worker_id,
            now=now,
            lease_until=now + self._lease_duration,
        )
        if lease is None:
            return False
        try:
            response = self._transport.deliver(lease.callback.to_wire())
            callback_receipt_ref = self._closed_callback_response(response, lease)
            ack_now = _aware(self._clock())
            self._repository.ack(
                scope,
                lease,
                callback_receipt_ref=callback_receipt_ref,
                now=ack_now,
            )
        except Exception:
            fail_now = _aware(self._clock())
            self._repository.fail(
                scope,
                lease,
                safe_code="gateway_callback_failed",
                next_attempt_at=fail_now + timedelta(seconds=min(300, 2**lease.attempt)),
                now=fail_now,
            )
        return True

    @staticmethod
    def _closed_callback_response(
        value: object,
        lease: EmailMaterialRetentionCallbackLease,
    ) -> str:
        fields = {
            "schema_version",
            "site_id",
            "authority_receipt_ref",
            "tombstone_receipt_ref",
            "callback_receipt_ref",
            "accepted",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ValueError("invalid gateway callback response")
        callback = lease.callback
        receipt = value.get("callback_receipt_ref")
        if (
            value.get("schema_version") != "1.0"
            or value.get("accepted") is not True
            or value.get("site_id") != callback.site_id
            or not isinstance(value.get("authority_receipt_ref"), str)
            or not hmac.compare_digest(
                str(value["authority_receipt_ref"]), callback.authority_receipt_ref
            )
            or not isinstance(value.get("tombstone_receipt_ref"), str)
            or not hmac.compare_digest(
                str(value["tombstone_receipt_ref"]), callback.tombstone_receipt_ref
            )
            or not isinstance(receipt, str)
            or _REF.fullmatch(receipt) is None
        ):
            raise ValueError("invalid gateway callback response")
        return receipt


__all__ = [
    "GatewayAuthorityRegistrationRelay",
    "GatewayCallbackTransport",
    "ObserverRegistrationTransport",
    "ObserverTombstoneCallbackRelay",
]
