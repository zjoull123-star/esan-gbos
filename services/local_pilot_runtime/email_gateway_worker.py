"""Shared fenced relay mechanics for Email Gateway cross-database workers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol


class RelayStatus(StrEnum):
    IDLE = "idle"
    DELIVERED = "delivered"
    RETRY = "retry"
    DEAD_LETTER = "dead_letter"
    LEASE_LOST = "lease_lost"


@dataclass(frozen=True, slots=True)
class RelayResult:
    status: RelayStatus
    attempt: int | None = None


class RelayClaim(Protocol):
    @property
    def site_id(self) -> str: ...

    @property
    def item_ref(self) -> str: ...

    @property
    def request_id(self) -> str: ...

    @property
    def payload(self) -> Mapping[str, Any] | None: ...

    @property
    def payload_digest(self) -> str: ...

    @property
    def attempt(self) -> int: ...

    @property
    def max_attempts(self) -> int: ...

    @property
    def generation(self) -> int: ...

    @property
    def fence_token(self) -> str: ...


class RelayOutbox(Protocol):
    def claim(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> RelayClaim | None: ...

    def heartbeat(
        self,
        claim: RelayClaim,
        *,
        now: datetime,
        lease_duration: timedelta,
    ) -> None: ...

    def mark_delivered(
        self,
        claim: RelayClaim,
        *,
        receipt: Mapping[str, object],
        now: datetime,
    ) -> None: ...

    def mark_failed(
        self,
        claim: RelayClaim,
        *,
        retry_at: datetime,
        error_code: str,
        now: datetime,
    ) -> str: ...


class RelayTransport(Protocol):
    def post(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> tuple[int, dict[str, Any]]: ...


class FencedHttpRelayWorker:
    """Claim one item, renew its exact fence, then acknowledge only an exact receipt."""

    def __init__(
        self,
        *,
        outbox: RelayOutbox,
        transport: RelayTransport,
        bearer_token: str,
        worker_id: str,
        clock: Callable[[], datetime],
        lease_duration: timedelta,
        retry_delay: timedelta = timedelta(seconds=30),
        timeout_seconds: float = 5.0,
    ) -> None:
        if (
            not bearer_token
            or bearer_token != bearer_token.strip()
            or not worker_id
            or worker_id != worker_id.strip()
            or lease_duration <= timedelta(0)
            or lease_duration > timedelta(minutes=5)
            or retry_delay <= timedelta(0)
            or retry_delay > timedelta(hours=1)
            or not 0 < timeout_seconds < lease_duration.total_seconds()
        ):
            raise ValueError("invalid fenced relay configuration")
        self._outbox = outbox
        self._transport = transport
        self._bearer_token = bearer_token
        self._worker_id = worker_id
        self._clock = clock
        self._lease_duration = lease_duration
        self._retry_delay = retry_delay
        self._timeout_seconds = timeout_seconds

    @property
    def endpoint(self) -> str:
        raise NotImplementedError

    @property
    def auth_ref(self) -> str:
        raise NotImplementedError

    @property
    def purpose(self) -> str:
        raise NotImplementedError

    @property
    def identity_field(self) -> str:
        raise NotImplementedError

    def run_once(self) -> RelayResult:
        now = self._clock()
        claim = self._outbox.claim(
            worker_id=self._worker_id,
            now=now,
            lease_duration=self._lease_duration,
        )
        if claim is None:
            return RelayResult(RelayStatus.IDLE)
        self._validate_claim(claim)
        if claim.payload is None:
            return self._fail(claim, now, "relay_payload_unavailable")
        try:
            self._outbox.heartbeat(
                claim,
                now=self._clock(),
                lease_duration=self._lease_duration,
            )
            status, response = self._transport.post(
                url=self.endpoint,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self._bearer_token}",
                    "Content-Type": "application/json",
                    "X-GBOS-Local-Auth-Ref": self.auth_ref,
                    "X-Payload-Digest": claim.payload_digest,
                    "X-Processing-Purpose": self.purpose,
                    "X-Request-ID": claim.request_id,
                    "X-Site-ID": claim.site_id,
                },
                payload=claim.payload,
                timeout_seconds=self._timeout_seconds,
            )
        except Exception:
            return self._fail(claim, now, "downstream_unavailable")
        receipt = self._receipt(status, response, claim)
        if receipt is None:
            code = (
                "downstream_retryable"
                if status == 429 or 500 <= status <= 599
                else "downstream_rejected"
            )
            return self._fail(claim, now, code)
        try:
            self._outbox.mark_delivered(
                claim,
                receipt=response,
                now=self._clock(),
            )
        except Exception:
            return RelayResult(RelayStatus.LEASE_LOST, claim.attempt)
        return RelayResult(RelayStatus.DELIVERED, claim.attempt)

    def _fail(self, claim: RelayClaim, now: datetime, code: str) -> RelayResult:
        try:
            state = self._outbox.mark_failed(
                claim,
                retry_at=now + self._retry_delay,
                error_code=code,
                now=now,
            )
        except Exception:
            return RelayResult(RelayStatus.LEASE_LOST, claim.attempt)
        return RelayResult(
            RelayStatus.DEAD_LETTER if state == "dead_letter" else RelayStatus.RETRY,
            claim.attempt,
        )

    def _receipt(
        self,
        status: int,
        response: object,
        claim: RelayClaim,
    ) -> str | None:
        if (
            status != 200
            or not isinstance(response, dict)
            or set(response)
            != {"schema_version", "receipt_ref", self.identity_field, "payload_digest"}
            or response.get("schema_version") != "1.0"
            or response.get(self.identity_field) != claim.item_ref
            or response.get("payload_digest") != claim.payload_digest
        ):
            return None
        receipt = response.get("receipt_ref")
        if (
            not isinstance(receipt, str)
            or not receipt
            or receipt != receipt.strip()
            or len(receipt) > 256
        ):
            return None
        return receipt

    @staticmethod
    def _validate_claim(claim: RelayClaim) -> None:
        if (
            not claim.site_id
            or not claim.item_ref
            or not claim.request_id
            or not claim.payload_digest.startswith("sha256:")
            or len(claim.payload_digest) != 71
            or not 1 <= claim.attempt <= claim.max_attempts <= 5
            or claim.generation < 1
            or not claim.fence_token
        ):
            raise ValueError("invalid relay claim")


def main() -> int:
    """No generic standalone worker exists without a scoped repository factory."""

    return 78


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "FencedHttpRelayWorker",
    "RelayClaim",
    "RelayOutbox",
    "RelayResult",
    "RelayStatus",
    "RelayTransport",
    "main",
]
