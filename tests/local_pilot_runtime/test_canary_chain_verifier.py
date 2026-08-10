from __future__ import annotations

import json
import os
import stat
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest

from services.local_pilot_runtime.canary_chain_verifier import (
    AgentInvocationProjection,
    CanaryChainVerificationError,
    CanaryProjectionRepositories,
    ContextChainProjection,
    ObserverLatchProjection,
    PostgresAgentCanaryRepository,
    PostgresContextCanaryRepository,
    PostgresObserverCanaryRepository,
    create_projection_repositories,
    validate_canary_chain_attestation,
    verify_canary_chain,
)

SITE_ID = "gbos.localhost"
PURPOSE = "observation_processing"
MODEL = "deepseek-v4-flash"
SOURCE_COMMIT = "a" * 40
ACTIVATION = datetime(2026, 8, 11, 9, 0, tzinfo=UTC)
WINDOW_START = datetime(2026, 8, 11, 10, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 11, 11, 0, tzinfo=UTC)
GENERATED_AT = datetime(2026, 8, 11, 11, 1, tzinfo=UTC)
INVOCATION_DIGEST = "1" * 64
INTELLIGENCE_DIGEST = "2" * 64
RECEIPT_DIGEST = "3" * 64
RAW_INVOCATION_REF = "invocation-technical-ref"
RAW_INTELLIGENCE_REF = "intelligence-technical-ref"


def _private_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


def _canary_dir(tmp_path: Path) -> tuple[Path, str]:
    directory = tmp_path / "canary"
    directory.mkdir(mode=0o700)
    manifest = {
        "schema_version": "1.0",
        "site_id": SITE_ID,
        "local_pilot_go": True,
        "local_pilot_status": "running",
        "production_go": False,
        "channels": {"email": {"enabled": True, "activation_time": "2026-08-11T09:00:00Z"}},
        "deepseek": {"enabled": True, "kill_switch": False, "model": MODEL},
        "capabilities": {"external_send": False, "formal_business_commands": False},
    }
    manifest_path = directory / "pilot-manifest.json"
    _private_json(manifest_path, manifest)
    manifest_sha256 = __import__("hashlib").sha256(manifest_path.read_bytes()).hexdigest()
    _private_json(
        directory / "canary-run.json",
        {
            "schema_version": "1.1",
            "run_id": "4b9a25cc-b7c6-40bb-9589-c014e9dfd427",
            "state": "prepared",
            "source_commit": SOURCE_COMMIT,
            "manifest_sha256": manifest_sha256,
            "activation_time": "2026-08-11T09:00:00Z",
            "stability_assessment": {
                "continuous_runtime_required": False,
                "seventy_two_hour_run": "deferred_by_user",
            },
            "scope": {
                "channels": ["email"],
                "model": MODEL,
                "external_send": False,
                "formal_commands": False,
            },
        },
    )
    return directory, manifest_sha256


def _agent(**changes: object) -> AgentInvocationProjection:
    value = AgentInvocationProjection(
        site_id=SITE_ID,
        invocation_ref_sha256=INVOCATION_DIGEST,
        requested_model=MODEL,
        response_reported_observed_model=MODEL,
        response_id_present=True,
        network_call_count=1,
        tool_call_count=0,
        external_send_count=0,
        status="succeeded",
        error_code=None,
        started_at=WINDOW_START + timedelta(minutes=5),
        completed_at=WINDOW_START + timedelta(minutes=6),
    )
    return replace(value, **changes)


def _context(**changes: object) -> ContextChainProjection:
    value = ContextChainProjection(
        site_id=SITE_ID,
        processing_purpose=PURPOSE,
        invocation_ref_sha256=INVOCATION_DIGEST,
        intelligence_ref_sha256=INTELLIGENCE_DIGEST,
        invocation_ordinal=1,
        review_status="AI Draft",
        model_name=MODEL,
        model_version=MODEL,
        draft_state_bound=True,
        draft_status="succeeded",
        receipt_doctype="GBOS Informal Observation",
        receipt_name_present=True,
        receipt_revision=1,
        receipt_request_present=True,
        receipt_request_bound=True,
        receipt_digest=RECEIPT_DIGEST,
        created_at=WINDOW_START + timedelta(minutes=7),
        updated_at=WINDOW_START + timedelta(minutes=8),
    )
    return replace(value, **changes)


class _AgentRepository:
    def __init__(self, rows: tuple[AgentInvocationProjection, ...]) -> None:
        self.rows = rows

    def bounded_window(self, **_: object) -> tuple[AgentInvocationProjection, ...]:
        return self.rows


class _ContextRepository:
    def __init__(self, rows: tuple[ContextChainProjection, ...]) -> None:
        self.rows = rows

    def bounded_window(self, **_: object) -> tuple[ContextChainProjection, ...]:
        return self.rows


class _ObserverRepository:
    def __init__(self, row: ObserverLatchProjection) -> None:
        self.row = row

    def latch(self, **_: object) -> ObserverLatchProjection:
        return self.row


def _repositories(
    *,
    agents: tuple[AgentInvocationProjection, ...] = (_agent(),),
    contexts: tuple[ContextChainProjection, ...] = (_context(),),
    latch: ObserverLatchProjection | None = None,
) -> CanaryProjectionRepositories:
    return CanaryProjectionRepositories(
        agent=_AgentRepository(agents),
        context=_ContextRepository(contexts),
        observer=_ObserverRepository(
            latch
            or ObserverLatchProjection(
                site_id=SITE_ID,
                processing_purpose=PURPOSE,
                is_open=True,
            )
        ),
        close=lambda: None,
    )


def test_exact_machine_projection_chain_writes_private_content_free_attestation(
    tmp_path: Path,
) -> None:
    canary_dir, manifest_sha256 = _canary_dir(tmp_path)
    output = tmp_path / "chain-attestation.json"

    attestation = verify_canary_chain(
        canary_dir=canary_dir,
        output_path=output,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        expected_source_commit=SOURCE_COMMIT,
        repositories=_repositories(),
        clock=lambda: GENERATED_AT,
    )

    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert json.loads(output.read_text(encoding="utf-8")) == attestation
    assert attestation["schema_version"] == "1.0"
    assert attestation["attestation_type"] == "local_email_deepseek_context_frappe_chain"
    assert attestation["source_commit"] == SOURCE_COMMIT
    assert attestation["manifest_sha256"] == manifest_sha256
    assert attestation["activation_time"] == "2026-08-11T09:00:00Z"
    assert attestation["observation_window"] == {
        "started_at": "2026-08-11T10:00:00Z",
        "ended_at": "2026-08-11T11:00:00Z",
    }
    chain = attestation["chain"]
    assert chain["response_reported_observed_model"] == MODEL
    assert chain["invocation_ref_sha256"] == INVOCATION_DIGEST
    assert chain["intelligence_ref_sha256"] == INTELLIGENCE_DIGEST
    assert chain["receipt_digest"] == RECEIPT_DIGEST
    assert chain["fatal_or_mismatch_invocation_count"] == 0
    assert chain["observer_fatal_latch_open"] is True
    assert len(attestation["payload_sha256"]) == 64
    serialized = json.dumps(attestation, sort_keys=True)
    for forbidden in (
        "prompt",
        "response_body",
        "raw_body",
        "tokenization",
        "mapping",
        'receipt_name"',
        "@",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    ("repositories", "code"),
    [
        (_repositories(agents=()), "agent_invocation_count_invalid"),
        (_repositories(agents=(_agent(), _agent())), "agent_invocation_count_invalid"),
        (
            _repositories(agents=(_agent(response_reported_observed_model="different"),)),
            "agent_invocation_invalid",
        ),
        (
            _repositories(agents=(_agent(error_code="model_mismatch", status="failed"),)),
            "agent_invocation_invalid",
        ),
        (_repositories(contexts=()), "context_chain_count_invalid"),
        (
            _repositories(contexts=(_context(invocation_ref_sha256="4" * 64),)),
            "cross_projection_binding_invalid",
        ),
        (
            _repositories(contexts=(_context(receipt_request_bound=False),)),
            "context_chain_invalid",
        ),
        (
            _repositories(contexts=(_context(draft_state_bound=False),)),
            "context_chain_invalid",
        ),
        (
            _repositories(contexts=(_context(receipt_revision=0),)),
            "context_chain_invalid",
        ),
        (
            _repositories(
                latch=ObserverLatchProjection(
                    site_id=SITE_ID,
                    processing_purpose=PURPOSE,
                    is_open=False,
                )
            ),
            "model_fatal_latch_closed",
        ),
    ],
)
def test_verifier_fails_closed_on_missing_multiple_drift_or_latched_projection(
    tmp_path: Path,
    repositories: CanaryProjectionRepositories,
    code: str,
) -> None:
    canary_dir, _ = _canary_dir(tmp_path)

    with pytest.raises(CanaryChainVerificationError, match=f"^{code}$"):
        verify_canary_chain(
            canary_dir=canary_dir,
            output_path=tmp_path / "chain-attestation.json",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            expected_source_commit=SOURCE_COMMIT,
            repositories=repositories,
            clock=lambda: GENERATED_AT,
        )

    assert not (tmp_path / "chain-attestation.json").exists()


def test_verifier_rejects_cross_site_and_window_drift_without_leaking_values(
    tmp_path: Path,
) -> None:
    canary_dir, _ = _canary_dir(tmp_path)
    cross_site = _repositories(agents=(_agent(site_id="other.localhost"),))

    with pytest.raises(CanaryChainVerificationError) as error:
        verify_canary_chain(
            canary_dir=canary_dir,
            output_path=tmp_path / "chain-attestation.json",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            expected_source_commit=SOURCE_COMMIT,
            repositories=cross_site,
            clock=lambda: GENERATED_AT,
        )

    assert str(error.value) == "agent_invocation_invalid"
    assert "other.localhost" not in repr(error.value)
    out_of_window = _repositories(agents=(_agent(completed_at=WINDOW_END + timedelta(seconds=1)),))
    with pytest.raises(CanaryChainVerificationError, match="^agent_invocation_invalid$"):
        verify_canary_chain(
            canary_dir=canary_dir,
            output_path=tmp_path / "chain-attestation.json",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            expected_source_commit=SOURCE_COMMIT,
            repositories=out_of_window,
            clock=lambda: GENERATED_AT,
        )


def test_verifier_rejects_existing_or_repository_internal_output(tmp_path: Path) -> None:
    canary_dir, _ = _canary_dir(tmp_path)
    output = tmp_path / "chain-attestation.json"
    _private_json(output, {"arbitrary": True})

    with pytest.raises(CanaryChainVerificationError, match="^attestation_output_invalid$"):
        verify_canary_chain(
            canary_dir=canary_dir,
            output_path=output,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            expected_source_commit=SOURCE_COMMIT,
            repositories=_repositories(),
            clock=lambda: GENERATED_AT,
        )

    assert json.loads(output.read_text(encoding="utf-8")) == {"arbitrary": True}
    assert stat.S_IMODE(canary_dir.stat().st_mode) == 0o700
    assert os.path.commonpath((str(output), str(canary_dir))) != str(canary_dir)


def test_verifier_rejects_malformed_run_id_and_symlinked_output_parent(tmp_path: Path) -> None:
    canary_dir, _ = _canary_dir(tmp_path)
    control_path = canary_dir / "canary-run.json"
    control = json.loads(control_path.read_text(encoding="utf-8"))
    control["run_id"] = "manually-entered-run"
    _private_json(control_path, control)

    with pytest.raises(CanaryChainVerificationError, match="^canary_binding_invalid$"):
        verify_canary_chain(
            canary_dir=canary_dir,
            output_path=tmp_path / "chain-attestation.json",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            expected_source_commit=SOURCE_COMMIT,
            repositories=_repositories(),
            clock=lambda: GENERATED_AT,
        )

    control["run_id"] = "4b9a25cc-b7c6-40bb-9589-c014e9dfd427"
    _private_json(control_path, control)
    real_parent = tmp_path / "real-output"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-output"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(CanaryChainVerificationError, match="^attestation_output_invalid$"):
        verify_canary_chain(
            canary_dir=canary_dir,
            output_path=linked_parent / "chain-attestation.json",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            expected_source_commit=SOURCE_COMMIT,
            repositories=_repositories(),
            clock=lambda: GENERATED_AT,
        )


class _Cursor:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.rows: list[tuple[object, ...]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, query: str, params: object = None) -> None:
        self.connection.queries.append((query, params))
        if "FROM agent_runtime.model_invocations" in query:
            self.rows = [
                (
                    SITE_ID,
                    RAW_INVOCATION_REF,
                    MODEL,
                    MODEL,
                    True,
                    1,
                    0,
                    0,
                    "succeeded",
                    None,
                    WINDOW_START + timedelta(minutes=5),
                    WINDOW_START + timedelta(minutes=6),
                )
            ]
        elif "FROM context.communication_intelligence AS intelligence" in query:
            self.rows = [
                (
                    SITE_ID,
                    PURPOSE,
                    RAW_INVOCATION_REF,
                    RAW_INTELLIGENCE_REF,
                    1,
                    "AI Draft",
                    MODEL,
                    MODEL,
                    True,
                    "succeeded",
                    "GBOS Informal Observation",
                    True,
                    1,
                    True,
                    True,
                    RECEIPT_DIGEST,
                    WINDOW_START + timedelta(minutes=7),
                    WINDOW_START + timedelta(minutes=8),
                )
            ]
        elif "FROM observer.model_fatal_latches" in query:
            self.rows = [(True,)]
        else:
            self.rows = []

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.rows

    def fetchone(self) -> tuple[object, ...] | None:
        return None if not self.rows else self.rows[0]


class _Connection:
    def __init__(self) -> None:
        self.queries: list[tuple[str, object]] = []
        self.closed = False

    @contextmanager
    def transaction(self):  # type: ignore[no-untyped-def]
        yield

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def close(self) -> None:
        self.closed = True


def test_postgres_projection_queries_return_only_bounded_content_free_fields() -> None:
    agent_connection = _Connection()
    context_connection = _Connection()
    observer_connection = _Connection()

    agents = PostgresAgentCanaryRepository(agent_connection).bounded_window(
        site_id=SITE_ID,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        limit=2,
    )
    contexts = PostgresContextCanaryRepository(context_connection).bounded_window(
        site_id=SITE_ID,
        processing_purpose=PURPOSE,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        limit=2,
    )
    latch = PostgresObserverCanaryRepository(observer_connection).latch(
        site_id=SITE_ID,
        processing_purpose=PURPOSE,
    )

    expected_invocation_digest = sha256(RAW_INVOCATION_REF.encode()).hexdigest()
    expected_intelligence_digest = sha256(RAW_INTELLIGENCE_REF.encode()).hexdigest()
    assert agents == (_agent(invocation_ref_sha256=expected_invocation_digest),)
    assert contexts == (
        _context(
            invocation_ref_sha256=expected_invocation_digest,
            intelligence_ref_sha256=expected_intelligence_digest,
        ),
    )
    assert latch == ObserverLatchProjection(SITE_ID, PURPOSE, True)
    all_queries = "\n".join(
        query
        for connection in (agent_connection, context_connection, observer_connection)
        for query, _ in connection.queries
    ).lower()
    assert " limit %s" in all_queries
    for forbidden in (
        "prompt_version",
        "output_schema_version",
        "observation_event_refs",
        "tokenization_receipt_refs",
        "summary_zh",
        "subject",
        "team_ref",
        "observation_id",
        "receipt_name,",
        "receipt_request_id,",
    ):
        assert forbidden not in all_queries
    assert "sha256(" not in all_queries
    assert RAW_INVOCATION_REF not in repr(agents)
    assert RAW_INTELLIGENCE_REF not in repr(contexts)


def _projection_config(tmp_path: Path) -> Path:
    cas = tmp_path / "cas"
    vault = tmp_path / "vault"
    secrets = tmp_path / "secrets"
    for directory in (cas, vault, secrets):
        directory.mkdir()
    connections: dict[str, object] = {}
    for role, user in (
        ("observer", "gbos_observer_app"),
        ("context", "gbos_context_app"),
        ("agent", "gbos_agent_app"),
    ):
        password = secrets / role
        password.write_text(f"{role}-secret", encoding="utf-8")
        password.chmod(0o600)
        connections[role] = {
            "host": "127.0.0.1",
            "port": 55432,
            "database": "gbos_local_pilot",
            "user": user,
            "password_file": str(password),
            "connect_timeout_seconds": 3,
        }
    config = tmp_path / "projection-connections.json"
    _private_json(
        config,
        {
            "schema_version": "1.0",
            "site_id": SITE_ID,
            "controlled_egress": True,
            "evidence_cas_root": str(cas),
            "tokenizer_vault_root": str(vault),
            "connections": connections,
        },
    )
    return config


def test_projection_repository_factory_uses_exact_three_roles_and_closes_on_request(
    tmp_path: Path,
) -> None:
    config = _projection_config(tmp_path)
    users: list[str] = []
    connections: list[_Connection] = []

    def connector(**kwargs: object) -> _Connection:
        users.append(str(kwargs["user"]))
        connection = _Connection()
        connections.append(connection)
        return connection

    repositories = create_projection_repositories(
        projection_config_path=config,
        expected_site_id=SITE_ID,
        connector=connector,
    )
    repositories.close()

    assert users == ["gbos_observer_app", "gbos_context_app", "gbos_agent_app"]
    assert all(connection.closed for connection in connections)
    assert "secret" not in repr(repositories).lower()


def test_projection_repository_factory_rejects_unsafe_secret_before_connect(
    tmp_path: Path,
) -> None:
    config = _projection_config(tmp_path)
    value = json.loads(config.read_text(encoding="utf-8"))
    secret_path = Path(value["connections"]["observer"]["password_file"])
    secret_path.chmod(0o644)
    called = False

    def connector(**_: object) -> _Connection:
        nonlocal called
        called = True
        return _Connection()

    with pytest.raises(CanaryChainVerificationError, match="^projection_configuration_invalid$"):
        create_projection_repositories(
            projection_config_path=config,
            expected_site_id=SITE_ID,
            connector=connector,
        )

    assert called is False


def test_verifier_uses_closed_projection_config_and_closes_all_connections(
    tmp_path: Path,
) -> None:
    canary_dir, _ = _canary_dir(tmp_path)
    config = _projection_config(tmp_path)
    connections: list[_Connection] = []

    def connector(**_: object) -> _Connection:
        connection = _Connection()
        connections.append(connection)
        return connection

    attestation = verify_canary_chain(
        canary_dir=canary_dir,
        projection_config_path=config,
        output_path=tmp_path / "chain-attestation.json",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        expected_source_commit=SOURCE_COMMIT,
        connector=connector,
        clock=lambda: GENERATED_AT,
    )

    assert (
        attestation["chain"]["invocation_ref_sha256"]
        == sha256(RAW_INVOCATION_REF.encode()).hexdigest()
    )
    assert len(connections) == 3
    assert all(connection.closed for connection in connections)


def test_verifier_converts_database_error_to_safe_closed_failure(tmp_path: Path) -> None:
    canary_dir, _ = _canary_dir(tmp_path)

    class FailingAgent:
        def bounded_window(self, **_: object) -> tuple[AgentInvocationProjection, ...]:
            raise RuntimeError("password=RAW-SECRET")

    repositories = CanaryProjectionRepositories(
        agent=FailingAgent(),
        context=_ContextRepository((_context(),)),
        observer=_ObserverRepository(ObserverLatchProjection(SITE_ID, PURPOSE, True)),
        close=lambda: None,
    )
    with pytest.raises(CanaryChainVerificationError) as error:
        verify_canary_chain(
            canary_dir=canary_dir,
            output_path=tmp_path / "chain-attestation.json",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            expected_source_commit=SOURCE_COMMIT,
            repositories=repositories,
            clock=lambda: GENERATED_AT,
        )

    assert str(error.value) == "projection_read_failed"
    assert "RAW-SECRET" not in repr(error.value)


def test_attestation_validator_rechecks_closed_content_digest_and_bindings(
    tmp_path: Path,
) -> None:
    canary_dir, manifest_sha256 = _canary_dir(tmp_path)
    attestation = verify_canary_chain(
        canary_dir=canary_dir,
        output_path=tmp_path / "chain-attestation.json",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        expected_source_commit=SOURCE_COMMIT,
        repositories=_repositories(),
        clock=lambda: GENERATED_AT,
    )

    assert (
        validate_canary_chain_attestation(
            attestation,
            expected_run_id="4b9a25cc-b7c6-40bb-9589-c014e9dfd427",
            expected_site_id=SITE_ID,
            expected_source_commit=SOURCE_COMMIT,
            expected_manifest_sha256=manifest_sha256,
            expected_activation_time="2026-08-11T09:00:00Z",
        )
        == attestation
    )

    for mutate in (
        lambda value: value.update({"unclosed": True}),
        lambda value: value["chain"].update({"response_reported_observed_model": "different"}),
        lambda value: value["chain"].update({"fatal_or_mismatch_invocation_count": 1}),
        lambda value: value["chain"].update({"observer_fatal_latch_open": False}),
        lambda value: value.update({"source_commit": "b" * 40}),
        lambda value: value.update({"payload_sha256": "f" * 64}),
    ):
        changed = json.loads(json.dumps(attestation))
        mutate(changed)
        with pytest.raises(CanaryChainVerificationError):
            validate_canary_chain_attestation(
                changed,
                expected_run_id="4b9a25cc-b7c6-40bb-9589-c014e9dfd427",
                expected_site_id=SITE_ID,
                expected_source_commit=SOURCE_COMMIT,
                expected_manifest_sha256=manifest_sha256,
                expected_activation_time="2026-08-11T09:00:00Z",
            )
