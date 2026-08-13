from __future__ import annotations

import json
from datetime import timedelta

import pytest

from services.email_gateway.models import TenantScope, stable_ref
from services.email_gateway.send_outbox import PostgresEmailSendRepository
from tests.email_gateway.fakes.provider import NOW, closed_command


def test_send_outbox_is_inert_until_approved_command_stage(scope) -> None:
    from services.email_gateway.models import OutboundNotAuthorized
    from services.email_gateway.send_outbox import DisabledSendOutboxRepository

    repository = DisabledSendOutboxRepository(outbound_enabled=False)
    with pytest.raises(OutboundNotAuthorized, match="outbound_not_authorized"):
        repository.insert(scope, object())
    with pytest.raises(OutboundNotAuthorized, match="outbound_not_authorized"):
        DisabledSendOutboxRepository(outbound_enabled=True).insert(scope, object())


def test_gateway_core_has_no_outbound_transport_protocol() -> None:
    from services.email_gateway import protocols

    names = set(vars(protocols))
    assert not names.intersection({"SmtpClient", "ProviderSender", "EmailTransport", "send_email"})


class _SendCursor:
    def __init__(self, *, fail_on: str | None = None) -> None:
        self.queries: list[tuple[str, tuple[object, ...]]] = []
        self.row: tuple[object, ...] | None = None
        self.fail_on = fail_on

    def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
        self.queries.append((query, params))
        if self.fail_on is not None and self.fail_on in query:
            raise RuntimeError("injected database failure")
        if "state.state = 'queued'" in query:
            command = closed_command()
            self.row = (
                "SOB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
                "ECR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
                "sha256:" + command["payload_sha256"],
                json.dumps(command),
                "queued",
                NOW,
                0,
                0,
            )
        elif "RETURNING send_outbox_ref" in query:
            self.row = ("SOB-01ARZ3NDEKTSV4RRFFQ69G5FAV",)
        else:
            self.row = None

    def fetchone(self) -> tuple[object, ...] | None:
        row, self.row = self.row, None
        return row

    def close(self) -> None:
        return None


class _SendConnection:
    def __init__(self, *, fail_on: str | None = None) -> None:
        self.db = _SendCursor(fail_on=fail_on)
        self.cursor_calls = 0
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _SendCursor:
        self.cursor_calls += 1
        return self.db

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _claimed(connection: _SendConnection):
    command = closed_command()
    scope = TenantScope(command["site_id"], command["processing_purpose"])
    repository = PostgresEmailSendRepository(
        connection,  # type: ignore[arg-type]
        actual_database_role="gbos_email_send_worker",
    )
    claim = repository.claim(
        scope,
        worker_id="send-worker-1",
        now=NOW,
        lease_duration=timedelta(seconds=30),
    )
    assert claim is not None
    return repository, scope, claim


@pytest.mark.parametrize("outcome", ["accepted", "delivered"])
def test_eligible_finish_creates_terminal_authorities_after_receipt_in_same_transaction(
    outcome: str,
) -> None:
    connection = _SendConnection()
    repository, scope, claim = _claimed(connection)

    repository.finish(
        scope,
        claim,
        outcome=outcome,
        safe_code=f"provider_{outcome}",
        provider_receipt_ref="provider-receipt-1",
        now=NOW,
    )

    queries = [query for query, _params in connection.db.queries]
    receipt_index = next(
        index
        for index, query in enumerate(queries)
        if "INSERT INTO email_gateway.provider_receipts" in query
    )
    authority_index = next(
        index
        for index, query in enumerate(queries)
        if "create_sent_email_material_authorities" in query
    )
    expected_receipt_ref = stable_ref("PRC", claim.fence_token, outcome, "provider-receipt-1")
    authority_params = connection.db.queries[authority_index][1]
    assert receipt_index < authority_index
    assert authority_params == (scope.site_id, expected_receipt_ref)
    assert connection.cursor_calls == 2  # claim transaction, then one finish transaction
    assert connection.commits == 2
    assert connection.rollbacks == 0


@pytest.mark.parametrize("outcome", ["bounced", "permanently_rejected"])
def test_ineligible_finish_does_not_create_terminal_authority(outcome: str) -> None:
    connection = _SendConnection()
    repository, scope, claim = _claimed(connection)

    repository.finish(
        scope,
        claim,
        outcome=outcome,
        safe_code=f"provider_{outcome}",
        provider_receipt_ref="provider-receipt-1",
        now=NOW,
    )

    assert not any(
        "create_sent_email_material_authorities" in query
        for query, _params in connection.db.queries
    )


def test_terminal_authority_failure_rolls_back_provider_receipt_transaction() -> None:
    connection = _SendConnection(fail_on="create_sent_email_material_authorities")
    repository, scope, claim = _claimed(connection)

    with pytest.raises(ValueError, match="persistence operation rejected"):
        repository.finish(
            scope,
            claim,
            outcome="accepted",
            safe_code="provider_accepted",
            provider_receipt_ref="provider-receipt-1",
            now=NOW,
        )

    assert connection.commits == 1
    assert connection.rollbacks == 1


class _ReconciliationCursor(_SendCursor):
    def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
        super().execute(query, params)
        if "state.state = 'reconciliation_required'" in query:
            command = closed_command()
            self.row = (
                "SOB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
                "ECR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
                "sha256:" + command["payload_sha256"],
                json.dumps(command),
                "reconciliation_required",
                NOW,
                1,
            )


class _ReconciliationConnection(_SendConnection):
    def __init__(self) -> None:
        super().__init__()
        self.db = _ReconciliationCursor()


@pytest.mark.parametrize("outcome", ["accepted", "delivered"])
def test_eligible_reconciliation_creates_authorities_after_receipt_in_one_transaction(
    outcome: str,
) -> None:
    connection = _ReconciliationConnection()
    repository = PostgresEmailSendRepository(
        connection,  # type: ignore[arg-type]
        actual_database_role="gbos_email_send_worker",
    )
    command = closed_command()
    scope = TenantScope(command["site_id"], command["processing_purpose"])

    repository.record_reconciliation(
        scope,
        "SOB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        outcome=outcome,
        safe_code=f"lookup_{outcome}",
        provider_receipt_ref="provider-receipt-1",
        now=NOW,
    )

    queries = [query for query, _params in connection.db.queries]
    receipt_index = next(
        index
        for index, query in enumerate(queries)
        if "INSERT INTO email_gateway.provider_receipts" in query
    )
    authority_index = next(
        index
        for index, query in enumerate(queries)
        if "create_sent_email_material_authorities" in query
    )
    stable_request_id = stable_ref("PRQ", scope.site_id, command["stable_client_request_id"])
    expected_receipt_ref = stable_ref(
        "PRC",
        stable_request_id + ":reconciliation",
        outcome,
        "provider-receipt-1",
    )
    assert receipt_index < authority_index
    assert connection.db.queries[authority_index][1] == (
        scope.site_id,
        expected_receipt_ref,
    )
    assert connection.cursor_calls == 1
    assert connection.commits == 1
    assert connection.rollbacks == 0


@pytest.mark.parametrize("outcome", ["bounced", "permanently_rejected", "unknown", "not_submitted"])
def test_ineligible_reconciliation_does_not_create_terminal_authority(outcome: str) -> None:
    connection = _ReconciliationConnection()
    repository = PostgresEmailSendRepository(
        connection,  # type: ignore[arg-type]
        actual_database_role="gbos_email_send_worker",
    )
    command = closed_command()
    scope = TenantScope(command["site_id"], command["processing_purpose"])

    repository.record_reconciliation(
        scope,
        "SOB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        outcome=outcome,
        safe_code=f"lookup_{outcome}",
        provider_receipt_ref=None,
        now=NOW,
    )

    assert not any(
        "create_sent_email_material_authorities" in query
        for query, _params in connection.db.queries
    )
