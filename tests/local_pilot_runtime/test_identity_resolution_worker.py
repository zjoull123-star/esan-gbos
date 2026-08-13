from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from services.local_pilot_runtime.identity_resolution_worker import (
    DEFAULT_FRAPPE_BASE_URL,
    DEFAULT_FRAPPE_UNIX_SOCKET,
    FrappeIdentityResolverClient,
    HeartbeatRunner,
    HttpxIdentityResolverTransport,
    IdentityResolutionClientError,
    IdentityResolutionComponents,
    IdentityResolutionRunStatus,
    IdentityResolutionWorker,
    main,
    run_worker_daemon,
)
from services.observer.observer.identity_resolution import (
    InMemoryIdentityResolutionRepository,
    ParticipantIdentityResolution,
)
from services.observer.observer.identity_resolution_work import (
    IdentityResolutionLeaseConflict,
    InMemoryIdentityResolutionWorkRepository,
)
from services.observer.observer.models import TenantScope

NOW = datetime(2026, 8, 10, 9, tzinfo=UTC)
SCOPE = TenantScope("alpha.example", "observation_processing")
IDENTITY_REF = "extid:v1:email:N6juwc4ZaH0TL-KQUdymKdFk4sSVi6FB1fQTOjPwaI8"
MAPPING_REF = "EID-01ARZ3NDEKTSV4RRFFQ69G5FAV"


class _Clock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


class _Transport:
    def __init__(self, responses: list[tuple[int, dict[str, Any]] | BaseException]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def request(self, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _resolution(*, status: str = "confirmed", revision: int = 1) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "site_id": SCOPE.site_id,
        "identity_provider": "email",
        "external_subject_ref": IDENTITY_REF,
        "mapping_ref": MAPPING_REF,
        "mapping_revision": revision,
        "team_ref": "team-sales",
        "target_type": "User",
        "target_ref": "member@example.invalid",
        "status": status,
        "resolved_at": "2026-08-10T08:59:00Z",
    }


def _response(*, status: str = "confirmed", revision: int = 1) -> dict[str, Any]:
    return {"message": {"resolutions": [_resolution(status=status, revision=revision)]}}


def _error(code: str) -> dict[str, Any]:
    return {"message": {"error": {"code": code}}}


def _client(transport: _Transport) -> FrappeIdentityResolverClient:
    return FrappeIdentityResolverClient(
        base_url=DEFAULT_FRAPPE_BASE_URL,
        unix_socket=None,
        site_id=SCOPE.site_id,
        auth_ref="observer-identity-resolver-v1",
        api_key="resolver-key-sentinel",
        api_secret="resolver-secret-sentinel",
        timeout_seconds=2.0,
        lease_duration=timedelta(seconds=10),
        transport=transport,
    )


def _enqueue(work: InMemoryIdentityResolutionWorkRepository, *, max_attempts: int = 3) -> str:
    item = work.enqueue(
        SCOPE,
        identity_provider="email",
        identity_ref=IDENTITY_REF,
        team_ref="team-sales",
        now=NOW,
        max_attempts=max_attempts,
    )
    return item.work_id


def _worker(
    transport: _Transport,
    *,
    work: InMemoryIdentityResolutionWorkRepository | None = None,
    projection: InMemoryIdentityResolutionRepository | None = None,
    clock: _Clock | None = None,
    heartbeat_runner: HeartbeatRunner | None = None,
) -> tuple[
    IdentityResolutionWorker,
    InMemoryIdentityResolutionWorkRepository,
    InMemoryIdentityResolutionRepository,
    _Clock,
]:
    active_work = work or InMemoryIdentityResolutionWorkRepository()
    active_projection = projection or InMemoryIdentityResolutionRepository()
    active_clock = clock or _Clock()
    worker = IdentityResolutionWorker(
        work_repository=active_work,
        projection_repository=active_projection,
        client=_client(transport),
        worker_id="identity-worker-1",
        clock=active_clock,
        lease_duration=timedelta(seconds=10),
        unresolved_recheck=timedelta(minutes=5),
        successful_recheck=timedelta(hours=1),
        retry_base=timedelta(seconds=10),
        retry_cap=timedelta(minutes=5),
        heartbeat_runner=heartbeat_runner,
    )
    return worker, active_work, active_projection, active_clock


def test_client_posts_one_lookup_with_exact_governed_request_and_no_revision() -> None:
    transport = _Transport([(200, _response())])
    client = _client(transport)

    result = client.resolve(
        identity_provider="email",
        identity_ref=IDENTITY_REF,
        team_ref="team-sales",
        request_id="identity-resolution-request-1",
    )

    assert result.status == "confirmed"
    assert result.resolution is not None
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == (
        "http://frappe-backend:8000/api/method/esan_gbos.api.internal.identity_resolution.resolve"
    )
    assert call["headers"] == {
        "Accept": "application/json",
        "Authorization": "token resolver-key-sentinel:resolver-secret-sentinel",
        "Content-Type": "application/json",
        "Host": SCOPE.site_id,
        "X-GBOS-Frappe-Auth-Ref": "observer-identity-resolver-v1",
        "X-Processing-Purpose": "identity_resolution",
        "X-Request-ID": "identity-resolution-request-1",
        "X-Site-ID": SCOPE.site_id,
    }
    payload = call["payload"]["payload"]
    assert set(payload) == {
        "site_id",
        "processing_purpose",
        "request_id",
        "auth_ref",
        "lookups",
    }
    assert payload["lookups"] == [
        {
            "identity_provider": "email",
            "external_subject_ref": IDENTITY_REF,
            "expected_team_ref": "team-sales",
        }
    ]
    assert "expected_mapping_revision" not in repr(payload)
    assert call["timeout_seconds"] == 2.0


@pytest.mark.parametrize(
    ("base_url", "unix_socket"),
    (
        ("http://127.0.0.1:8000", None),
        ("http://frappe-backend:8001", None),
        ("http://frappe-backend.evil:8000", None),
        ("http://user:secret@frappe-backend:8000", None),
        ("http://frappe-backend:8000/path", None),
        ("http://frappe-backend:8000?query=1", None),
        ("http://frappe-backend:8000#fragment", None),
        (DEFAULT_FRAPPE_BASE_URL, Path("/tmp/frappe.sock")),
    ),
)
def test_client_rejects_every_target_except_exact_service_and_governed_uds(
    base_url: str,
    unix_socket: Path | None,
) -> None:
    with pytest.raises(IdentityResolutionClientError, match="endpoint"):
        FrappeIdentityResolverClient(
            base_url=base_url,
            unix_socket=unix_socket,
            site_id=SCOPE.site_id,
            auth_ref="observer-identity-resolver-v1",
            api_key="resolver-key",
            api_secret="resolver-secret",
            timeout_seconds=2,
            lease_duration=timedelta(seconds=10),
            transport=_Transport([]),
        )


def test_client_accepts_only_the_fixed_governed_uds_path() -> None:
    client = FrappeIdentityResolverClient(
        base_url=DEFAULT_FRAPPE_BASE_URL,
        unix_socket=DEFAULT_FRAPPE_UNIX_SOCKET,
        site_id=SCOPE.site_id,
        auth_ref="observer-identity-resolver-v1",
        api_key="resolver-key",
        api_secret="resolver-secret",
        timeout_seconds=2,
        lease_duration=timedelta(seconds=10),
        transport=_Transport([]),
    )

    assert "resolver-secret" not in repr(client)


def test_http_transport_itself_rejects_an_ungoverned_uds_path() -> None:
    with pytest.raises(IdentityResolutionClientError, match="endpoint"):
        HttpxIdentityResolverTransport(Path("/tmp/frappe.sock"))


@pytest.mark.parametrize(
    ("status", "body", "code", "transient"),
    (
        (401, _error("authentication_required"), "authentication_failed", False),
        (403, _error("permission_denied"), "permission_denied", False),
        (403, _error("team_scope_mismatch"), "team_mismatch", False),
        (409, _error("mapping_conflict"), "invalid_resolver_response", False),
        (429, _error("rate_limited"), "resolver_unavailable", True),
        (500, _error("internal_error"), "resolver_unavailable", True),
        (302, {}, "invalid_resolver_response", False),
    ),
)
def test_client_maps_closed_status_categories(
    status: int,
    body: dict[str, Any],
    code: str,
    transient: bool,
) -> None:
    client = _client(_Transport([(status, body)]))

    with pytest.raises(IdentityResolutionClientError) as caught:
        client.resolve(
            identity_provider="email",
            identity_ref=IDENTITY_REF,
            team_ref="team-sales",
            request_id="identity-resolution-request-1",
        )

    assert caught.value.code == code
    assert caught.value.transient is transient
    assert IDENTITY_REF not in repr(caught.value)
    assert "member@example.invalid" not in repr(caught.value)


def test_client_maps_exact_404_to_unresolved_and_timeout_to_safe_error() -> None:
    unresolved = _client(_Transport([(404, _error("mapping_not_resolved"))])).resolve(
        identity_provider="email",
        identity_ref=IDENTITY_REF,
        team_ref="team-sales",
        request_id="identity-resolution-request-1",
    )
    timeout = _client(_Transport([httpx.ReadTimeout("private response sentinel")]))

    assert unresolved.status == "unresolved"
    assert unresolved.resolution is None
    with pytest.raises(IdentityResolutionClientError) as caught:
        timeout.resolve(
            identity_provider="email",
            identity_ref=IDENTITY_REF,
            team_ref="team-sales",
            request_id="identity-resolution-request-2",
        )
    assert caught.value.code == "resolver_timeout"
    assert caught.value.transient is True
    assert "private response sentinel" not in repr(caught.value)


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: value.update(extra="field"),
        lambda value: value.update(schema_version="2.0"),
        lambda value: value.update(site_id="other.example"),
        lambda value: value.update(
            external_subject_ref="extid:v1:email:p5N7ZLjKpY8Dchu2us9ceMsjX-vg5wsbhM2ZVBRhoI4"
        ),
        lambda value: value.update(team_ref="team-other"),
        lambda value: value.update(identity_provider="wecom"),
        lambda value: value.update(status="pending"),
        lambda value: value.update(mapping_revision=True),
        lambda value: value.update(resolved_at="not-a-time"),
    ),
)
def test_client_rejects_malformed_or_mismatched_closed_response(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    resolution = _resolution()
    mutate(resolution)
    client = _client(_Transport([(200, {"message": {"resolutions": [resolution]}})]))

    with pytest.raises(IdentityResolutionClientError) as caught:
        client.resolve(
            identity_provider="email",
            identity_ref=IDENTITY_REF,
            team_ref="team-sales",
            request_id="identity-resolution-request-1",
        )

    assert caught.value.code in {"invalid_resolver_response", "team_mismatch"}
    assert caught.value.transient is False


def test_worker_refreshes_unresolved_to_confirmed_to_revoked() -> None:
    transport = _Transport(
        [
            (404, _error("mapping_not_resolved")),
            (200, _response()),
            (200, _response(status="revoked", revision=2)),
        ]
    )
    worker, work, projection, clock = _worker(transport)
    work_id = _enqueue(work)

    assert worker.run_once(SCOPE).status is IdentityResolutionRunStatus.UNRESOLVED
    unresolved = work.get(SCOPE, work_id)
    assert unresolved is not None and unresolved.status == "unresolved"
    assert unresolved.last_resolution_status == "unresolved"
    clock.advance(timedelta(minutes=5))
    assert worker.run_once(SCOPE).status is IdentityResolutionRunStatus.CONFIRMED
    confirmed = projection.latest(SCOPE, "email", IDENTITY_REF)
    assert confirmed is not None and confirmed.status == "confirmed"
    queued_confirmed = work.get(SCOPE, work_id)
    assert queued_confirmed is not None and queued_confirmed.last_resolution_status == "confirmed"
    clock.advance(timedelta(hours=1))
    assert worker.run_once(SCOPE).status is IdentityResolutionRunStatus.REVOKED
    revoked = projection.latest(SCOPE, "email", IDENTITY_REF)
    assert revoked is not None and revoked.status == "revoked"
    assert len(projection.history(SCOPE, "email", IDENTITY_REF)) == 2


@pytest.mark.parametrize(
    ("failure", "expected"),
    (
        (
            IdentityResolutionClientError("safe", code="resolver_unavailable", transient=True),
            IdentityResolutionRunStatus.RETRY,
        ),
        (
            IdentityResolutionClientError("safe", code="resolver_timeout", transient=True),
            IdentityResolutionRunStatus.RETRY,
        ),
        (
            IdentityResolutionClientError("safe", code="authentication_failed", transient=False),
            IdentityResolutionRunStatus.CLOSED,
        ),
        (
            IdentityResolutionClientError(
                "safe", code="invalid_resolver_response", transient=False
            ),
            IdentityResolutionRunStatus.CLOSED,
        ),
    ),
)
def test_worker_retries_transient_failures_but_closes_auth_and_schema(
    failure: IdentityResolutionClientError,
    expected: IdentityResolutionRunStatus,
) -> None:
    transport = _Transport([failure])
    worker, work, projection, _clock = _worker(transport)
    work_id = _enqueue(work)

    result = worker.run_once(SCOPE)

    item = work.get(SCOPE, work_id)
    assert result.status is expected
    assert item is not None and item.status == "retry_wait"
    assert item.next_attempt_at == NOW + timedelta(seconds=10)
    assert projection.latest(SCOPE, "email", IDENTITY_REF) is None


def test_worker_backoff_is_deterministic_bounded_and_dead_letters() -> None:
    failures: list[tuple[int, dict[str, Any]] | BaseException] = [
        IdentityResolutionClientError("safe", code="resolver_unavailable", transient=True)
        for _ in range(3)
    ]
    worker, work, _projection, clock = _worker(_Transport(failures))
    work_id = _enqueue(work, max_attempts=3)

    first = worker.run_once(SCOPE)
    assert first.status is IdentityResolutionRunStatus.RETRY
    clock.advance(timedelta(seconds=10))
    second = worker.run_once(SCOPE)
    assert second.status is IdentityResolutionRunStatus.RETRY
    second_item = work.get(SCOPE, work_id)
    assert second_item is not None
    assert second_item.next_attempt_at == clock.value + timedelta(seconds=20)
    clock.advance(timedelta(seconds=20))
    third = worker.run_once(SCOPE)

    assert third.status is IdentityResolutionRunStatus.DEAD_LETTER
    dead = work.get(SCOPE, work_id)
    assert dead is not None and dead.status == "dead_letter"


class _HeartbeatRunner:
    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.calls = 0

    def run(self, execute: Callable[[], Any], heartbeat: Callable[[], object]) -> Any:
        self.clock.advance(timedelta(seconds=6))
        heartbeat()
        self.calls += 1
        return execute()


def test_worker_renews_lease_during_slow_call_through_runner_seam() -> None:
    clock = _Clock()
    runner = _HeartbeatRunner(clock)
    worker, work, _projection, _clock = _worker(
        _Transport([(404, _error("mapping_not_resolved"))]),
        clock=clock,
        heartbeat_runner=runner,
    )
    work_id = _enqueue(work)

    result = worker.run_once(SCOPE)

    assert runner.calls == 1
    assert result.status is IdentityResolutionRunStatus.UNRESOLVED
    item = work.get(SCOPE, work_id)
    assert item is not None and item.updated_at == NOW + timedelta(seconds=6)


class _LeaseLostRunner:
    def run(self, execute: Callable[[], Any], heartbeat: Callable[[], object]) -> Any:
        del execute, heartbeat
        raise IdentityResolutionLeaseConflict("stale sentinel")


def test_worker_never_completes_or_projects_after_lease_loss() -> None:
    transport = _Transport([(200, _response())])
    worker, work, projection, _clock = _worker(
        transport,
        heartbeat_runner=_LeaseLostRunner(),
    )
    work_id = _enqueue(work)

    result = worker.run_once(SCOPE)

    assert result.status is IdentityResolutionRunStatus.LEASE_LOST
    assert transport.calls == []
    assert projection.latest(SCOPE, "email", IDENTITY_REF) is None
    item = work.get(SCOPE, work_id)
    assert item is not None and item.status == "leased"


class _LeaseLostAfterResponseRunner:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, execute: Callable[[], Any], heartbeat: Callable[[], object]) -> Any:
        del heartbeat
        self.calls += 1
        if self.calls == 2:
            raise IdentityResolutionLeaseConflict("stale after response sentinel")
        return execute()


def test_lease_loss_after_response_prevents_stale_projection_and_completion() -> None:
    transport = _Transport([(200, _response())])
    runner = _LeaseLostAfterResponseRunner()
    worker, work, projection, _clock = _worker(transport, heartbeat_runner=runner)
    work_id = _enqueue(work)

    result = worker.run_once(SCOPE)

    assert runner.calls == 2
    assert result.status is IdentityResolutionRunStatus.LEASE_LOST
    assert len(transport.calls) == 1
    assert projection.latest(SCOPE, "email", IDENTITY_REF) is None
    item = work.get(SCOPE, work_id)
    assert item is not None and item.status == "leased"


def test_future_authoritative_timestamp_fails_closed_without_projection() -> None:
    resolution = _resolution()
    resolution["resolved_at"] = "2026-08-10T09:00:01Z"
    transport = _Transport([(200, {"message": {"resolutions": [resolution]}})])
    worker, work, projection, _clock = _worker(transport)
    work_id = _enqueue(work)

    result = worker.run_once(SCOPE)

    assert result.status is IdentityResolutionRunStatus.CLOSED
    item = work.get(SCOPE, work_id)
    assert item is not None and item.last_error_code == "invalid_resolver_response"
    assert projection.latest(SCOPE, "email", IDENTITY_REF) is None


class _ResponseLossRepository:
    def __init__(self, inner: InMemoryIdentityResolutionRepository) -> None:
        self.inner = inner
        self.fail_once = True

    def record(
        self,
        scope: TenantScope,
        resolution: ParticipantIdentityResolution,
    ) -> ParticipantIdentityResolution:
        stored = self.inner.record(scope, resolution)
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("response lost after commit sentinel")
        return stored

    def latest(
        self,
        scope: TenantScope,
        identity_provider: str,
        external_subject_ref: str,
    ) -> ParticipantIdentityResolution | None:
        return self.inner.latest(scope, identity_provider, external_subject_ref)

    def history(
        self,
        scope: TenantScope,
        identity_provider: str,
        external_subject_ref: str,
    ) -> tuple[ParticipantIdentityResolution, ...]:
        return self.inner.history(scope, identity_provider, external_subject_ref)


def test_response_loss_restart_and_replay_are_idempotent() -> None:
    transport = _Transport([(200, _response()), (200, _response())])
    work = InMemoryIdentityResolutionWorkRepository()
    projection = InMemoryIdentityResolutionRepository()
    clock = _Clock()
    lossy = _ResponseLossRepository(projection)
    worker = IdentityResolutionWorker(
        work_repository=work,
        projection_repository=lossy,
        client=_client(transport),
        worker_id="identity-worker-1",
        clock=clock,
        lease_duration=timedelta(seconds=10),
        retry_base=timedelta(seconds=10),
    )
    work_id = _enqueue(work)

    assert worker.run_once(SCOPE).status is IdentityResolutionRunStatus.RETRY
    assert len(projection.history(SCOPE, "email", IDENTITY_REF)) == 1
    clock.advance(timedelta(seconds=10))
    restarted = IdentityResolutionWorker(
        work_repository=work,
        projection_repository=lossy,
        client=_client(transport),
        worker_id="identity-worker-2",
        clock=clock,
        lease_duration=timedelta(seconds=10),
        retry_base=timedelta(seconds=10),
    )

    assert restarted.run_once(SCOPE).status is IdentityResolutionRunStatus.CONFIRMED
    assert len(projection.history(SCOPE, "email", IDENTITY_REF)) == 1
    item = work.get(SCOPE, work_id)
    assert item is not None and item.status == "confirmed"


def test_projection_conflict_is_terminal() -> None:
    first = _resolution()
    second = _resolution(revision=2)
    second["target_ref"] = "other@example.invalid"
    transport = _Transport(
        [
            (200, {"message": {"resolutions": [first]}}),
            (200, {"message": {"resolutions": [second]}}),
        ]
    )
    worker, work, projection, clock = _worker(transport)
    work_id = _enqueue(work)

    assert worker.run_once(SCOPE).status is IdentityResolutionRunStatus.CONFIRMED
    clock.advance(timedelta(hours=1))
    assert worker.run_once(SCOPE).status is IdentityResolutionRunStatus.CONFLICT
    item = work.get(SCOPE, work_id)
    assert item is not None and item.status == "conflict"
    assert len(projection.history(SCOPE, "email", IDENTITY_REF)) == 1


class _Stop:
    def __init__(self) -> None:
        self.stopped = False
        self.waits: list[float] = []

    def is_set(self) -> bool:
        return self.stopped

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        self.stopped = True
        return True


def test_daemon_waits_only_when_idle_and_stops_cleanly() -> None:
    transport = _Transport([])
    worker, _work, _projection, _clock = _worker(transport)
    stop = _Stop()

    run_worker_daemon(worker, SCOPE, stop_event=stop, idle_delay_seconds=0.25)

    assert stop.waits == [0.25]
    assert transport.calls == []


def test_idle_iteration_records_a_readiness_heartbeat() -> None:
    worker, work, _projection, clock = _worker(_Transport([]))

    result = worker.run_once(SCOPE)

    snapshot = work.snapshot(
        SCOPE,
        now=clock.value,
        readiness_window=timedelta(seconds=30),
    )
    assert result.status is IdentityResolutionRunStatus.IDLE
    assert snapshot.ready is True
    assert snapshot.worker_last_heartbeat_at == NOW


def test_expired_crashed_claim_is_reclaimed_after_restart() -> None:
    work = InMemoryIdentityResolutionWorkRepository()
    projection = InMemoryIdentityResolutionRepository()
    work_id = _enqueue(work)
    crashed = work.claim(
        SCOPE,
        worker_id="crashed-worker",
        now=NOW,
        lease_duration=timedelta(seconds=5),
    )
    assert crashed is not None
    clock = _Clock(NOW + timedelta(seconds=6))
    restarted, _work, _projection, _clock = _worker(
        _Transport([(200, _response())]),
        work=work,
        projection=projection,
        clock=clock,
    )

    result = restarted.run_once(SCOPE)

    assert result.status is IdentityResolutionRunStatus.CONFIRMED
    assert result.attempt == 2
    item = work.get(SCOPE, work_id)
    assert item is not None and item.status == "confirmed"
    assert len(projection.history(SCOPE, "email", IDENTITY_REF)) == 1


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _manifest(
    path: Path,
    *,
    enabled: bool,
    identity_channel_enabled: bool | None = None,
    identity_projection_enabled: bool = False,
) -> Path:
    channel_enabled = enabled if identity_channel_enabled is None else identity_channel_enabled
    value = {
        "schema_version": "1.0",
        "mode": "local_pilot",
        "site_id": SCOPE.site_id,
        "production_go": False,
        "local_pilot_go": enabled,
        "local_pilot_status": "ready" if enabled else "disabled",
        "deepseek": {"enabled": False, "kill_switch": True},
        "email_gateway": {
            "kill_switch": not identity_projection_enabled,
            "identity_projection_kill_switch": not identity_projection_enabled,
        },
        "channels": {
            "email": {"enabled": channel_enabled},
            "wecom": {"enabled": False},
            "whatsapp": {"enabled": False},
            "media": {"enabled": False},
        },
    }
    _write_json(path, value)
    return path


def _secret(path: Path, value: str) -> Path:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return path


def _recording_transport_factory(
    calls: list[object],
) -> Callable[[Path | None], _Transport]:
    def create(socket: Path | None) -> _Transport:
        calls.append(socket)
        return _Transport([])

    return create


def _runtime_config(path: Path, tmp_path: Path) -> Path:
    value = {
        "schema_version": "1.0",
        "site_id": SCOPE.site_id,
        "postgres": {
            "host": "postgres",
            "port": 5432,
            "database": "observer",
            "user": "observer",
            "password_file": str(_secret(tmp_path / "postgres-password", "db-secret")),
            "connect_timeout_seconds": 2,
        },
        "auth": {
            "agent_api_bearer_file": "/run/secrets/agent",
            "context_api_bearer_file": "/run/secrets/context-api",
            "context_client_bearer_file": "/run/secrets/context-client",
            "context_auth_ref": "context-auth-v1",
        },
        "context_endpoint": {
            "base_url": DEFAULT_FRAPPE_BASE_URL,
            "unix_socket": None,
        },
        "listen": {
            "host": "127.0.0.1",
            "agent_api_port": 8002,
            "context_api_port": 8001,
        },
        "components": {
            name: {
                "enabled": False,
                "kill_switch": True,
                "provider_mode": "disabled",
                "synthetic_e2e": False,
            }
            for name in ("agent_api", "context_api", "agent_worker", "model_worker")
        },
        "worker": {
            "worker_id": "identity-worker-1",
            "idle_delay_seconds": 0.25,
            "heartbeat_interval_seconds": 1,
        },
    }
    _write_json(path, value)
    return path


@pytest.mark.parametrize(
    "environment",
    (
        {},
        {"GBOS_LOCAL_RUNTIME_ENABLED": "true"},
        {
            "GBOS_LOCAL_RUNTIME_ENABLED": "true",
            "GBOS_IDENTITY_RESOLUTION_KILL_SWITCH": "false",
            "GBOS_IDENTITY_RESOLVER_API_SECRET": "plaintext-forbidden",
        },
    ),
)
def test_preflight_kill_switch_or_plaintext_secret_never_connects_or_builds_http(
    tmp_path: Path,
    environment: dict[str, str],
) -> None:
    db_calls: list[object] = []
    transport_calls: list[object] = []

    result = main(
        manifest_path=_manifest(tmp_path / "manifest.json", enabled=True),
        runtime_config_path=_runtime_config(tmp_path / "runtime.json", tmp_path),
        api_key_file=tmp_path / "missing-key",
        api_secret_file=tmp_path / "missing-secret",
        environ=environment,
        connector=lambda **kwargs: db_calls.append(kwargs),
        transport_factory=_recording_transport_factory(transport_calls),
    )

    assert result == 78
    assert db_calls == []
    assert transport_calls == []


def test_disabled_formal_manifest_and_missing_private_secret_fail_before_db_http(
    tmp_path: Path,
) -> None:
    db_calls: list[object] = []
    http_calls: list[object] = []
    environment = {
        "GBOS_LOCAL_RUNTIME_ENABLED": "true",
        "GBOS_IDENTITY_RESOLUTION_KILL_SWITCH": "false",
    }

    disabled = main(
        manifest_path=_manifest(tmp_path / "formal.json", enabled=False),
        runtime_config_path=_runtime_config(tmp_path / "runtime.json", tmp_path),
        environ=environment,
        connector=lambda **kwargs: db_calls.append(kwargs),
        transport_factory=_recording_transport_factory(http_calls),
    )
    enabled_missing_secret = main(
        manifest_path=_manifest(tmp_path / "local.json", enabled=True),
        runtime_config_path=tmp_path / "runtime.json",
        api_key_file=tmp_path / "missing-key",
        api_secret_file=tmp_path / "missing-secret",
        environ=environment,
        connector=lambda **kwargs: db_calls.append(kwargs),
        transport_factory=_recording_transport_factory(http_calls),
    )

    assert disabled == 78
    assert enabled_missing_secret == 78
    assert db_calls == []
    assert http_calls == []


def test_enabled_manifest_without_identity_channel_fails_before_db_http(
    tmp_path: Path,
) -> None:
    db_calls: list[object] = []
    http_calls: list[object] = []

    result = main(
        manifest_path=_manifest(
            tmp_path / "manifest.json",
            enabled=True,
            identity_channel_enabled=False,
        ),
        runtime_config_path=_runtime_config(tmp_path / "runtime.json", tmp_path),
        api_key_file=_secret(tmp_path / "api-key", "resolver-key"),
        api_secret_file=_secret(tmp_path / "api-secret", "resolver-secret"),
        environ={
            "GBOS_LOCAL_RUNTIME_ENABLED": "true",
            "GBOS_IDENTITY_RESOLUTION_KILL_SWITCH": "false",
        },
        connector=lambda **kwargs: db_calls.append(kwargs),
        transport_factory=_recording_transport_factory(http_calls),
    )

    assert result == 78
    assert db_calls == []
    assert http_calls == []


class _Connection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_enabled_main_loads_files_then_builds_repositories_and_closes_connection(
    tmp_path: Path,
) -> None:
    connection = _Connection()
    captured: list[IdentityResolutionComponents] = []
    api_key = _secret(tmp_path / "api-key", "resolver-key")
    api_secret = _secret(tmp_path / "api-secret", "resolver-secret")

    def factory(active_connection: object) -> IdentityResolutionComponents:
        assert active_connection is connection
        components = IdentityResolutionComponents(
            work_repository=InMemoryIdentityResolutionWorkRepository(),
            projection_repository=InMemoryIdentityResolutionRepository(),
        )
        captured.append(components)
        return components

    result = main(
        manifest_path=_manifest(tmp_path / "manifest.json", enabled=True),
        runtime_config_path=_runtime_config(tmp_path / "runtime.json", tmp_path),
        api_key_file=api_key,
        api_secret_file=api_secret,
        environ={
            "GBOS_LOCAL_RUNTIME_ENABLED": "true",
            "GBOS_IDENTITY_RESOLUTION_KILL_SWITCH": "false",
        },
        connector=lambda **_kwargs: connection,
        components_factory=factory,
        transport_factory=lambda _socket: _Transport([]),
        daemon_runner=lambda *_args, **_kwargs: None,
    )

    assert result == 0
    assert len(captured) == 1
    assert connection.closed is True


def test_identity_projection_opt_in_validates_both_secrets_before_database(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    result = main(
        manifest_path=_manifest(
            tmp_path / "manifest.json",
            enabled=True,
            identity_projection_enabled=True,
        ),
        runtime_config_path=_runtime_config(tmp_path / "runtime.json", tmp_path),
        api_key_file=_secret(tmp_path / "api-key", "resolver-key"),
        api_secret_file=_secret(tmp_path / "api-secret", "resolver-secret"),
        identity_projection_bearer_file=tmp_path / "missing-projection-bearer",
        identity_projector_password_file=_secret(
            tmp_path / "projector-password",
            "projector-password-value",
        ),
        environ={
            "GBOS_LOCAL_RUNTIME_ENABLED": "true",
            "GBOS_IDENTITY_RESOLUTION_KILL_SWITCH": "false",
            "GBOS_IDENTITY_PROJECTION_KILL_SWITCH": "false",
        },
        connector=lambda **_: calls.append("db"),
        transport_factory=lambda _socket: _Transport([]),
    )

    assert result == 78
    assert calls == []


def test_identity_projection_opt_in_connects_dedicated_role_and_composes_relay(
    tmp_path: Path,
) -> None:
    connections: list[_Connection] = []
    captured: dict[str, object] = {}

    def connector(**_kwargs: object) -> _Connection:
        connection = _Connection()
        connections.append(connection)
        return connection

    def components_factory(_connection: object) -> IdentityResolutionComponents:
        return IdentityResolutionComponents(
            work_repository=InMemoryIdentityResolutionWorkRepository(),
            projection_repository=InMemoryIdentityResolutionRepository(),
        )

    def daemon_runner(*_args: object, **kwargs: object) -> None:
        captured.update(kwargs)

    result = main(
        manifest_path=_manifest(
            tmp_path / "manifest.json",
            enabled=True,
            identity_projection_enabled=True,
        ),
        runtime_config_path=_runtime_config(tmp_path / "runtime.json", tmp_path),
        api_key_file=_secret(tmp_path / "api-key", "resolver-key"),
        api_secret_file=_secret(tmp_path / "api-secret", "resolver-secret"),
        identity_projection_bearer_file=_secret(
            tmp_path / "projection-bearer",
            "identity-projection-secret",
        ),
        identity_projector_password_file=_secret(
            tmp_path / "projector-password",
            "projector-password-value",
        ),
        environ={
            "GBOS_LOCAL_RUNTIME_ENABLED": "true",
            "GBOS_IDENTITY_RESOLUTION_KILL_SWITCH": "false",
            "GBOS_IDENTITY_PROJECTION_KILL_SWITCH": "false",
        },
        connector=connector,
        components_factory=components_factory,
        transport_factory=lambda _socket: _Transport([]),
        identity_projection_transport_factory=lambda: _Transport([]),  # type: ignore[arg-type]
        daemon_runner=daemon_runner,
    )

    assert result == 0
    assert len(connections) == 2
    assert all(connection.closed for connection in connections)
    assert "identity_projection_worker" in captured


def test_repr_errors_and_logs_never_render_identity_target_or_credentials(caplog: Any) -> None:
    transport = _Transport([(200, _response())])
    client = _client(transport)
    result = client.resolve(
        identity_provider="email",
        identity_ref=IDENTITY_REF,
        team_ref="team-sales",
        request_id="identity-resolution-request-1",
    )

    rendered = "\n".join((repr(client), repr(result), repr(result.resolution), caplog.text))
    for sentinel in (
        IDENTITY_REF,
        "member@example.invalid",
        "resolver-key-sentinel",
        "resolver-secret-sentinel",
    ):
        assert sentinel not in rendered
