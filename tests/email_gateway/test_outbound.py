from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from services.action_guard.policy import ActionGuard
from services.email_gateway.api import build_email_command_ingest_api
from services.email_gateway.models import IdempotencyConflict, TenantScope
from services.email_gateway.outbound import (
    CommandIngestService,
    CommandPublication,
    InMemoryOutboundRepository,
)
from services.email_gateway.send_outbox import PostgresSendOutboxRepository
from tests.email_gateway.fakes.provider import NOW, authority_for, closed_command


def _publication(command: dict[str, object]) -> CommandPublication:
    return CommandPublication(
        publication_ref="PUB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        attempt=1,
        generation=1,
        fence_token="FNC-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        payload_digest="sha256:" + str(command["payload_sha256"]),
    )


def _service(command: dict[str, object], repository: InMemoryOutboundRepository):
    return CommandIngestService(
        repository=repository,
        action_guard=ActionGuard(),
        authority_resolver=lambda _scope, _command: authority_for(command),
        clock=lambda: NOW,
    )


def test_ingest_atomically_creates_one_receipt_and_one_immutable_outbox() -> None:
    command = closed_command()
    repository = InMemoryOutboundRepository()
    service = _service(command, repository)
    scope = TenantScope(command["site_id"], command["processing_purpose"])

    first = service.accept(scope, publication=_publication(command), command=command)
    replay = service.accept(scope, publication=_publication(command), command=command)

    assert replay == first
    assert first.command_receipt_ref.startswith("ECR-")
    assert first.send_outbox_ref.startswith("SOB-")
    assert first.payload_digest == command["payload_sha256"]
    assert repository.command_receipt_count(scope) == 1
    assert repository.outbox_count(scope) == 1
    outbox = repository.get(scope, first.send_outbox_ref)
    assert outbox is not None
    assert outbox.envelope.command_ref == command["command_id"]
    assert outbox.envelope.participants[0].opaque_address_ref.startswith("extid:v1:email:")
    assert "@" not in repr(outbox)


def test_replay_payload_drift_conflicts_without_a_second_record() -> None:
    command = closed_command()
    repository = InMemoryOutboundRepository()
    service = _service(command, repository)
    scope = TenantScope(command["site_id"], command["processing_purpose"])
    service.accept(scope, publication=_publication(command), command=command)
    drift = copy.deepcopy(command)
    drift["payload_sha256"] = "9" * 64

    with pytest.raises(IdempotencyConflict, match="command replay drift"):
        service.accept(scope, publication=_publication(drift), command=drift)
    assert repository.command_receipt_count(scope) == repository.outbox_count(scope) == 1


def test_concurrent_same_publication_replays_one_immutable_outbox() -> None:
    command = closed_command()
    repository = InMemoryOutboundRepository()
    service = _service(command, repository)
    scope = TenantScope(command["site_id"], command["processing_purpose"])

    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = tuple(
            executor.map(
                lambda _index: service.accept(
                    scope,
                    publication=_publication(command),
                    command=command,
                ),
                range(2),
            )
        )

    assert receipts[0] == receipts[1]
    assert repository.command_receipt_count(scope) == 1
    assert repository.outbox_count(scope) == 1


def test_transaction_failure_rolls_back_receipt_and_outbox_together() -> None:
    command = closed_command()
    repository = InMemoryOutboundRepository(
        transaction_failure_injector=lambda phase: (
            (_ for _ in ()).throw(RuntimeError("injected"))
            if phase == "after_command_receipt"
            else None
        )
    )
    service = _service(command, repository)
    scope = TenantScope(command["site_id"], command["processing_purpose"])

    with pytest.raises(RuntimeError, match="injected"):
        service.accept(scope, publication=_publication(command), command=command)
    assert repository.command_receipt_count(scope) == repository.outbox_count(scope) == 0


def test_closed_http_boundary_authenticates_scope_digest_and_replays_stably() -> None:
    command = closed_command()
    repository = InMemoryOutboundRepository()
    app = build_email_command_ingest_api(
        intake=_service(command, repository),
        bearer_token="gateway-command-ingest-secret",
        auth_ref="email-command-ingest-v1",
    )
    body = {
        "publication_ref": "PUB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "attempt": 1,
        "generation": 1,
        "fence_token": "FNC-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "payload_digest": "sha256:" + command["payload_sha256"],
        "command": command,
    }
    headers = {
        "Authorization": "Bearer gateway-command-ingest-secret",
        "X-GBOS-Local-Auth-Ref": "email-command-ingest-v1",
        "X-Site-ID": command["site_id"],
        "X-Processing-Purpose": command["processing_purpose"],
        "X-Audience": "email-command-executor",
        "X-GBOS-Scope": "email-send-execute",
        "X-Payload-Digest": body["payload_digest"],
        "X-Request-ID": "command-ingest-request-1",
    }

    first = TestClient(app).post("/internal/v1/email-commands/accept", json=body, headers=headers)
    replay = TestClient(app).post("/internal/v1/email-commands/accept", json=body, headers=headers)
    rejected = TestClient(app).post(
        "/internal/v1/email-commands/accept",
        json=body,
        headers={**headers, "X-Audience": "email-send-worker"},
    )

    assert first.status_code == 200
    assert replay.json() == first.json()
    assert set(first.json()) == {"command_receipt_ref", "send_outbox_ref", "payload_digest"}
    assert first.headers["cache-control"] == "no-store"
    assert rejected.status_code == 403
    assert (
        repository.outbox_count(TenantScope(command["site_id"], command["processing_purpose"])) == 1
    )


class _Cursor:
    def __init__(self, *, fail_on: str | None = None) -> None:
        self.queries: list[tuple[str, tuple[object, ...]]] = []
        self.fail_on = fail_on

    def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
        self.queries.append((query, params))
        if self.fail_on is not None and self.fail_on in query:
            raise RuntimeError("database failure")

    def fetchone(self):
        return None

    def close(self) -> None:
        return None


class _Connection:
    def __init__(self, *, fail_on: str | None = None) -> None:
        self.db = _Cursor(fail_on=fail_on)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _Cursor:
        return self.db

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_postgres_executor_commits_receipt_outbox_and_state_in_one_transaction() -> None:
    command = closed_command()
    connection = _Connection()
    repository = PostgresSendOutboxRepository(
        connection,
        actual_database_role="gbos_email_command_executor",
    )
    service = _service(command, repository)  # type: ignore[arg-type]
    scope = TenantScope(command["site_id"], command["processing_purpose"])

    receipt = service.accept(scope, publication=_publication(command), command=command)

    sql = "\n".join(query for query, _params in connection.db.queries)
    assert receipt.send_outbox_ref.startswith("SOB-")
    assert connection.commits == 1 and connection.rollbacks == 0
    assert "INSERT INTO email_gateway.command_inbox" in sql
    assert "INSERT INTO email_gateway.send_outbox" in sql
    assert "INSERT INTO email_gateway.send_outbox_state" in sql
    assert "pg_advisory_xact_lock" in sql
    assert "FOR UPDATE OF command" not in sql


def test_postgres_executor_rejects_wrong_role_and_rolls_back_partial_write() -> None:
    connection = _Connection()
    with pytest.raises(ValueError, match="database role binding rejected"):
        PostgresSendOutboxRepository(
            connection,
            actual_database_role="gbos_email_gateway_app",
        )

    command = closed_command()
    failing = _Connection(fail_on="INSERT INTO email_gateway.send_outbox (")
    repository = PostgresSendOutboxRepository(
        failing,
        actual_database_role="gbos_email_command_executor",
    )
    with pytest.raises(ValueError, match="persistence operation rejected"):
        _service(command, repository).accept(  # type: ignore[arg-type]
            TenantScope(command["site_id"], command["processing_purpose"]),
            publication=_publication(command),
            command=command,
        )
    assert failing.commits == 0 and failing.rollbacks == 1
