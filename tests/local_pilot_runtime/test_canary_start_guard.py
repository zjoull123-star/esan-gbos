from __future__ import annotations

import hashlib
import json
import os
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from services.local_pilot_runtime.canary_start_guard import (
    CanaryStartGuardError,
    PostgresAgentCanaryStartGuardRepository,
    PostgresCanaryStartGuardRepository,
    PostgresContextCanaryStartGuardRepository,
    run_canary_start_guard,
)
from services.local_pilot_runtime.canary_verifier_runtime import read_only_postgres_connector

SITE_ID = "gbos.localhost"
PURPOSE = "observation_processing"
INITIAL_CHECKPOINT = json.dumps(
    {"mailbox": "INBOX", "uid": 41, "uidvalidity": 701, "version": 1},
    separators=(",", ":"),
    sort_keys=True,
)


class _Cursor:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.row: tuple[Any, ...] | None = None

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, params: object = None) -> None:
        normalized = " ".join(query.split())
        self.connection.queries.append((normalized, params))
        if "current_user" in normalized:
            self.row = (self.connection.role, "on")
        elif "FROM observer.context_publication_outbox" in normalized:
            self.row = (self.connection.backlog,)
        elif "FROM observer.inbound_deliveries" in normalized:
            self.row = (self.connection.inbound_backlog,)
        elif "FROM observer.processing_jobs" in normalized:
            self.row = (self.connection.processing_backlog,)
        elif "FROM observer.identity_resolution_work" in normalized:
            self.row = (self.connection.identity_backlog,)
        elif "FROM observer.model_fatal_latches" in normalized:
            self.row = (self.connection.fatal_latches,)
        elif "FROM observer.connector_instances AS instance" in normalized:
            self.row = self.connection.checkpoint_row
        elif "FROM observer.connector_control_commands" in normalized:
            self.row = (self.connection.control_commands,)
        elif "FROM context.communication_draft_outbox" in normalized:
            self.row = (self.connection.draft_backlog,)
        elif "FROM agent_runtime.agent_tasks" in normalized:
            self.row = (self.connection.agent_task_backlog,)
        elif "FROM agent_runtime.proposal_materialization_outbox" in normalized:
            self.row = (self.connection.materialization_backlog,)
        else:
            self.row = None

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.row


class _Connection:
    def __init__(
        self,
        *,
        role: str = "gbos_observer_app",
        backlog: int = 0,
        inbound_backlog: int = 0,
        processing_backlog: int = 0,
        identity_backlog: int = 0,
        fatal_latches: int = 0,
        checkpoint_row: tuple[Any, ...] | None = None,
        control_commands: int = 0,
        draft_backlog: int = 0,
        agent_task_backlog: int = 0,
        materialization_backlog: int = 0,
    ) -> None:
        self.role = role
        self.backlog = backlog
        self.inbound_backlog = inbound_backlog
        self.processing_backlog = processing_backlog
        self.identity_backlog = identity_backlog
        self.fatal_latches = fatal_latches
        self.checkpoint_row = checkpoint_row
        self.control_commands = control_commands
        self.draft_backlog = draft_backlog
        self.agent_task_backlog = agent_task_backlog
        self.materialization_backlog = materialization_backlog
        self.queries: list[tuple[str, object]] = []
        self.closed = False

    def transaction(self) -> nullcontext[None]:
        return nullcontext()

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def close(self) -> None:
        self.closed = True


def _private_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
    path.chmod(0o600)


def _files(tmp_path: Path) -> dict[str, Path]:
    passwords: dict[str, Path] = {}
    for role in ("observer", "context", "agent"):
        password = tmp_path / f"postgres_{role}_password"
        password.write_text(f"{role}-password", encoding="utf-8")
        password.chmod(0o600)
        passwords[role] = password
    credential = tmp_path / "email_credential"
    _private_json(
        credential,
        {
            "instance_id": "email-primary",
            "team_ref": "team-main",
            "agent_task_type": "sales",
            "account_user_ref": "user@example.com",
            "host": "imap.example.com",
            "port": 993,
            "mailbox": "INBOX",
            "folder": "INBOX",
            "username": "user@example.com",
            "password": "secret",
            "poll_limit": 10,
            "max_message_bytes": 1_000_000,
            "max_attachment_bytes": 500_000,
            "max_attachments": 5,
            "rescan_max_window_seconds": 86_400,
            "rescan_max_uids": 100,
            "initial_checkpoint": INITIAL_CHECKPOINT,
        },
    )
    activation = "2026-08-11T00:00:00Z"
    manifest_value = {
        "schema_version": "1.0",
        "mode": "local_pilot",
        "site_id": SITE_ID,
        "production_go": False,
        "local_pilot_go": True,
        "local_pilot_status": "ready",
        "deepseek": {
            "enabled": True,
            "kill_switch": False,
            "model": "deepseek-v4-flash",
        },
        "channels": {
            "email": {
                "enabled": True,
                "activation_time": activation,
                "backfill_history": False,
            },
            "wecom": {
                "enabled": False,
                "activation_time": None,
                "backfill_history": False,
            },
            "whatsapp": {
                "enabled": False,
                "activation_time": None,
                "backfill_history": False,
            },
            "media": {
                "enabled": False,
                "activation_time": None,
                "backfill_history": False,
            },
        },
        "capabilities": {
            "external_send": False,
            "formal_business_commands": False,
        },
    }
    manifest = tmp_path / "pilot-manifest.json"
    _private_json(manifest, manifest_value)
    manifest_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()
    control = tmp_path / "canary-run.json"
    _private_json(
        control,
        {
            "schema_version": "1.1",
            "state": "prepared",
            "run_id": "0fdb899b-d4b4-4f1b-a313-08f9f18e1550",
            "activation_time": activation,
            "manifest_sha256": manifest_sha256,
            "scope": {
                "channels": ["email"],
                "model": "deepseek-v4-flash",
                "external_send": False,
                "formal_commands": False,
            },
        },
    )
    config = tmp_path / "canary-start-guard.json"
    _private_json(
        config,
        {
            "schema_version": "1.1",
            "site_id": SITE_ID,
            "processing_purpose": PURPOSE,
            "connections": {
                role: {
                    "host": "postgres",
                    "port": 5432,
                    "database": "gbos_local_pilot",
                    "user": f"gbos_{role}_app",
                    "password_file": str(passwords[role]),
                    "connect_timeout_seconds": 5,
                }
                for role in ("observer", "context", "agent")
            },
        },
    )
    connectors = tmp_path / "connectors.json"
    _private_json(
        connectors,
        {
            "schema_version": "1.0",
            "site_id": SITE_ID,
            "external_send": False,
            "evidence_cas_root": "/var/lib/gbos/evidence",
            "channels": {
                name: {
                    "enabled": name == "email",
                    "kill_switch": name != "email",
                    "activation_time": activation if name == "email" else None,
                    "backfill_history": False,
                    "credential_file": str(credential) if name == "email" else f"/abs/{name}",
                }
                for name in ("email", "wecom", "whatsapp", "media")
            },
        },
    )
    return {
        "config": config,
        "manifest": manifest,
        "control": control,
        "connectors": connectors,
    }


def test_repository_proves_read_only_role_empty_backlog_open_latch_and_clean_bindings() -> None:
    connection = _Connection()
    repository = PostgresCanaryStartGuardRepository(connection)

    repository.assert_safe(
        site_id=SITE_ID,
        processing_purpose=PURPOSE,
        connector="email",
        connector_instance_id="email-primary",
        initial_checkpoint=INITIAL_CHECKPOINT,
    )

    sql = "\n".join(query for query, _params in connection.queries)
    assert "SET TRANSACTION READ ONLY" in sql
    assert "status IN ('queued', 'leased', 'retry_wait')" in sql
    assert "observer.inbound_deliveries" in sql
    assert "observer.processing_jobs" in sql
    assert "observer.identity_resolution_work" in sql
    assert "lease_expires_at" not in next(
        query for query, _params in connection.queries if "context_publication_outbox" in query
    )
    assert "model_fatal_latches" in sql
    assert "connector_checkpoints" in sql
    assert "connector_control_commands" in sql


@pytest.mark.parametrize(
    ("field", "code"),
    [
        ("inbound_backlog", "inbound_delivery_backlog_not_empty"),
        ("processing_backlog", "processing_job_backlog_not_empty"),
        ("identity_backlog", "identity_resolution_backlog_not_empty"),
    ],
)
def test_any_active_observer_queue_fails_closed(field: str, code: str) -> None:
    connection = _Connection(**{field: 1})

    with pytest.raises(CanaryStartGuardError, match=code):
        PostgresCanaryStartGuardRepository(connection).assert_safe(
            site_id=SITE_ID,
            processing_purpose=PURPOSE,
            connector="email",
            connector_instance_id="email-primary",
            initial_checkpoint=INITIAL_CHECKPOINT,
        )


@pytest.mark.parametrize(
    ("repository", "connection", "code"),
    [
        (
            PostgresContextCanaryStartGuardRepository,
            _Connection(role="gbos_context_app", draft_backlog=1),
            "communication_draft_backlog_not_empty",
        ),
        (
            PostgresAgentCanaryStartGuardRepository,
            _Connection(role="gbos_agent_app", agent_task_backlog=1),
            "agent_task_backlog_not_empty",
        ),
        (
            PostgresAgentCanaryStartGuardRepository,
            _Connection(role="gbos_agent_app", materialization_backlog=1),
            "materialization_backlog_not_empty",
        ),
    ],
)
def test_any_active_downstream_queue_fails_closed(
    repository: type[PostgresContextCanaryStartGuardRepository]
    | type[PostgresAgentCanaryStartGuardRepository],
    connection: _Connection,
    code: str,
) -> None:
    with pytest.raises(CanaryStartGuardError, match=code):
        repository(connection).assert_safe(
            site_id=SITE_ID,
            processing_purpose=PURPOSE,
        )


@pytest.mark.parametrize("backlog_status", ["queued", "retry_wait", "leased", "expired_leased"])
def test_any_projection_backlog_fails_closed(backlog_status: str) -> None:
    del backlog_status
    connection = _Connection(backlog=1)

    with pytest.raises(CanaryStartGuardError, match="model_projection_backlog_not_empty"):
        PostgresCanaryStartGuardRepository(connection).assert_safe(
            site_id=SITE_ID,
            processing_purpose=PURPOSE,
            connector="email",
            connector_instance_id="email-primary",
            initial_checkpoint=INITIAL_CHECKPOINT,
        )


@pytest.mark.parametrize(
    ("connection", "code"),
    [
        (_Connection(fatal_latches=1), "model_fatal_latch_closed"),
        (
            _Connection(checkpoint_row=("healthy", 0, "different", 1, None, None, None, "healthy")),
            "checkpoint_binding_unsafe",
        ),
        (_Connection(control_commands=1), "connector_control_binding_unsafe"),
    ],
)
def test_unsafe_latch_checkpoint_or_control_fails_closed(
    connection: _Connection,
    code: str,
) -> None:
    with pytest.raises(CanaryStartGuardError, match=code):
        PostgresCanaryStartGuardRepository(connection).assert_safe(
            site_id=SITE_ID,
            processing_purpose=PURPOSE,
            connector="email",
            connector_instance_id="email-primary",
            initial_checkpoint=INITIAL_CHECKPOINT,
        )


def test_guard_binds_manifest_control_and_closes_database_connection(tmp_path: Path) -> None:
    files = _files(tmp_path)
    connections = {
        "gbos_observer_app": _Connection(role="gbos_observer_app"),
        "gbos_context_app": _Connection(role="gbos_context_app"),
        "gbos_agent_app": _Connection(role="gbos_agent_app"),
    }

    run_canary_start_guard(
        config_path=files["config"],
        manifest_path=files["manifest"],
        control_path=files["control"],
        connector_config_path=files["connectors"],
        connector=lambda **kwargs: connections[str(kwargs["user"])],
    )

    assert all(connection.closed for connection in connections.values())


def test_guard_rejects_changed_manifest_before_connecting(tmp_path: Path) -> None:
    files = _files(tmp_path)
    manifest = json.loads(files["manifest"].read_text(encoding="utf-8"))
    manifest["local_pilot_status"] = "running"
    _private_json(files["manifest"], manifest)
    connected = False

    def connect(**_kwargs: object) -> _Connection:
        nonlocal connected
        connected = True
        return _Connection()

    with pytest.raises(CanaryStartGuardError, match="canary_binding_invalid"):
        run_canary_start_guard(
            config_path=files["config"],
            manifest_path=files["manifest"],
            control_path=files["control"],
            connector_config_path=files["connectors"],
            connector=connect,
        )

    assert connected is False


def test_guard_config_and_password_must_be_private_regular_files(tmp_path: Path) -> None:
    files = _files(tmp_path)
    os.chmod(files["config"], 0o644)

    with pytest.raises(CanaryStartGuardError, match="guard_configuration_invalid"):
        run_canary_start_guard(
            config_path=files["config"],
            manifest_path=files["manifest"],
            control_path=files["control"],
            connector_config_path=files["connectors"],
            connector=lambda **_kwargs: _Connection(),
        )


def test_guard_rejects_non_private_rendered_connector_config(tmp_path: Path) -> None:
    files = _files(tmp_path)
    os.chmod(files["connectors"], 0o644)

    with pytest.raises(CanaryStartGuardError, match="canary_binding_invalid"):
        run_canary_start_guard(
            config_path=files["config"],
            manifest_path=files["manifest"],
            control_path=files["control"],
            connector_config_path=files["connectors"],
            connector=lambda **_kwargs: _Connection(),
        )


def test_chain_verifier_forces_every_postgres_session_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    sentinel = object()

    def connect(**kwargs: object) -> object:
        captured.update(kwargs)
        return sentinel

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))

    assert read_only_postgres_connector(host="postgres", port=5432) is sentinel
    assert captured == {
        "host": "postgres",
        "port": 5432,
        "options": "-c default_transaction_read_only=on",
    }
