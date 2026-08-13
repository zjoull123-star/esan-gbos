from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from services.observer.observer.identity_projection_outbox import (
    InMemoryIdentityProjectionOutbox,
    build_identity_projection_payload,
)
from services.observer.observer.identity_resolution import ParticipantIdentityResolution

NOW = datetime(2026, 8, 14, 9, 30, tzinfo=UTC)
SITE = "alpha.example"


class _Clock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value


class _Transport:
    def __init__(self, responses: list[tuple[int, dict[str, Any]] | BaseException]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def post(self, **kwargs: object) -> tuple[int, dict[str, Any]]:
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _payload() -> dict[str, object]:
    resolution = ParticipantIdentityResolution(
        site_id=SITE,
        identity_provider="email",
        external_subject_ref="extid:v1:email:" + "A" * 43,
        mapping_ref="EID-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        mapping_revision=3,
        team_ref="TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        target_type="Party",
        target_ref="protected-target@example.invalid",
        status="confirmed",
        resolved_at=NOW,
        recorded_at=NOW,
    )
    return build_identity_projection_payload(resolution, "sales_follow_up")


def _worker(
    responses: list[tuple[int, dict[str, Any]] | BaseException],
):
    from services.local_pilot_runtime.identity_resolution_worker import (
        IdentityProjectionOutboxAdapter,
        IdentityProjectionRelayWorker,
    )

    payload = _payload()
    outbox = InMemoryIdentityProjectionOutbox()
    outbox.append(payload, queued_at=NOW)
    clock = _Clock()
    transport = _Transport(responses)
    worker = IdentityProjectionRelayWorker(
        outbox=IdentityProjectionOutboxAdapter(outbox, site_id=SITE),
        transport=transport,
        bearer_token="identity-projection-secret",
        worker_id="identity-projector-1",
        clock=clock,
        lease_duration=timedelta(seconds=30),
        retry_delay=timedelta(seconds=30),
        timeout_seconds=3,
    )
    return worker, outbox, transport, clock, payload


def _receipt(payload: dict[str, object]) -> dict[str, Any]:
    from services.observer.observer.identity_projection_outbox import payload_digest

    return {
        "schema_version": "1.0",
        "projection_receipt": payload["projection_receipt"],
        "payload_digest": payload_digest(payload),
    }


def test_relay_posts_exact_business_purpose_headers_and_acknowledges() -> None:
    from services.local_pilot_runtime.email_gateway_worker import RelayStatus

    payload = _payload()
    worker, outbox, transport, _clock, _payload_value = _worker([(200, _receipt(payload))])

    result = worker.run_once()

    assert result.status is RelayStatus.DELIVERED
    call = transport.calls[0]
    assert call["url"] == ("http://email-gateway-api:8004/internal/v1/identity-projections/accept")
    assert call["headers"] == {
        "Accept": "application/json",
        "Authorization": "Bearer identity-projection-secret",
        "Content-Type": "application/json",
        "X-GBOS-Local-Auth-Ref": "observer-identity-projection-v1",
        "X-Payload-Digest": _receipt(payload)["payload_digest"],
        "X-Processing-Purpose": "sales_follow_up",
        "X-Request-ID": "identity-projection:"
        + str(payload["projection_receipt"]).removeprefix("sha256:"),
        "X-Site-ID": SITE,
    }
    assert call["payload"] == payload
    assert "protected-target" not in repr(call)
    assert (
        outbox.claim(
            site_id=SITE,
            worker_id="identity-projector-2",
            now=NOW + timedelta(minutes=1),
            lease_duration=timedelta(seconds=30),
        )
        is None
    )


def test_timeout_retries_but_400_and_invalid_receipt_dead_letter_immediately() -> None:
    from services.local_pilot_runtime.email_gateway_worker import RelayStatus

    timeout_worker, _outbox, _transport, clock, payload = _worker(
        [httpx.ReadTimeout("sentinel"), (200, _receipt(_payload()))]
    )
    assert timeout_worker.run_once().status is RelayStatus.RETRY
    clock.value += timedelta(seconds=30)
    assert timeout_worker.run_once().status is RelayStatus.DELIVERED

    rejected, _outbox, _transport, _clock, _payload_value = _worker([(400, {})])
    assert rejected.run_once().status is RelayStatus.DEAD_LETTER

    invalid, _outbox, _transport, _clock, _payload_value = _worker(
        [(200, {**_receipt(payload), "projection_receipt": "sha256:" + "f" * 64})]
    )
    assert invalid.run_once().status is RelayStatus.DEAD_LETTER


def test_429_and_5xx_retry_while_transport_contract_error_dead_letters() -> None:
    from services.local_pilot_runtime.email_gateway_worker import RelayStatus

    for status in (429, 500, 503):
        worker, _outbox, _transport, _clock, _payload_value = _worker([(status, {})])
        assert worker.run_once().status is RelayStatus.RETRY

    worker, _outbox, _transport, _clock, _payload_value = _worker(
        [ValueError("malformed response sentinel")]
    )
    assert worker.run_once().status is RelayStatus.DEAD_LETTER


def test_existing_identity_daemon_runs_projection_relay_without_third_service() -> None:
    from services.local_pilot_runtime.email_gateway_worker import RelayResult, RelayStatus
    from services.local_pilot_runtime.identity_resolution_worker import run_worker_daemon
    from services.observer.observer.models import TenantScope

    calls: list[str] = []

    class ResolutionWorker:
        def run_once(self, _scope: object) -> object:
            from services.local_pilot_runtime.identity_resolution_worker import (
                IdentityResolutionRunResult,
                IdentityResolutionRunStatus,
            )

            calls.append("resolution")
            return IdentityResolutionRunResult(IdentityResolutionRunStatus.IDLE)

    class ProjectionWorker:
        def run_once(self) -> RelayResult:
            calls.append("projection")
            stop.stopped = True
            return RelayResult(RelayStatus.DELIVERED, 1)

    class Stop:
        stopped = False

        def is_set(self) -> bool:
            return self.stopped

        def wait(self, _timeout: float) -> bool:
            self.stopped = True
            return True

    stop = Stop()
    run_worker_daemon(
        ResolutionWorker(),  # type: ignore[arg-type]
        TenantScope(SITE, "observation_processing"),
        stop_event=stop,
        idle_delay_seconds=0.25,
        identity_projection_worker=ProjectionWorker(),  # type: ignore[arg-type]
    )

    assert calls == ["resolution", "projection"]
