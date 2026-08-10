"""Fail-closed database guard before any local canary model egress starts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from .channel_config import (
    ChannelConfigError,
    EmailCredentialConfig,
    load_channel_config,
    load_channel_credential,
)
from .projection_config import ProjectionConfigError, load_canary_start_guard_config
from .runtime_support import RuntimeSupportError, connect_postgres

_MODEL = "deepseek-v4-flash"
_PURPOSE = "observation_processing"
_MAX_JSON_BYTES = 65_536


class CanaryStartGuardError(RuntimeError):
    """A low-cardinality rejection that is safe to emit before egress."""


class PostgresCanaryStartGuardRepository:
    """Read-only Observer-role proof over every persisted start boundary."""

    __slots__ = ("_connection",)

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def assert_safe(
        self,
        *,
        site_id: str,
        processing_purpose: str,
        connector: str,
        connector_instance_id: str,
        initial_checkpoint: str,
    ) -> None:
        try:
            with self._connection.transaction(), self._connection.cursor() as cursor:
                _bind_read_only_session(
                    cursor,
                    site_id=site_id,
                    processing_purpose=processing_purpose,
                    expected_user="gbos_observer_app",
                )

                cursor.execute(
                    """
                    SELECT count(*)
                    FROM observer.inbound_deliveries
                    WHERE site_id = %s
                      AND connector = %s
                      AND connector_instance_id = %s
                      AND processing_status IN (
                          'received', 'authenticated', 'queued', 'processing'
                      )
                    """,
                    (site_id, connector, connector_instance_id),
                )
                if _count(cursor.fetchone()) != 0:
                    raise CanaryStartGuardError("inbound_delivery_backlog_not_empty")

                cursor.execute(
                    """
                    SELECT count(*)
                    FROM observer.processing_jobs
                    WHERE site_id = %s
                      AND status IN ('queued', 'processing', 'retry_wait')
                    """,
                    (site_id,),
                )
                if _count(cursor.fetchone()) != 0:
                    raise CanaryStartGuardError("processing_job_backlog_not_empty")

                cursor.execute(
                    """
                    SELECT count(*)
                    FROM observer.identity_resolution_work
                    WHERE site_id = %s
                      AND (
                          status IN ('queued', 'leased', 'retry_wait')
                          OR (
                              status IN ('unresolved', 'confirmed', 'revoked')
                              AND next_attempt_at <= now()
                          )
                      )
                    """,
                    (site_id,),
                )
                if _count(cursor.fetchone()) != 0:
                    raise CanaryStartGuardError("identity_resolution_backlog_not_empty")

                cursor.execute(
                    """
                    SELECT count(*)
                    FROM observer.context_publication_outbox
                    WHERE site_id = %s
                      AND status IN ('queued', 'leased', 'retry_wait')
                    """,
                    (site_id,),
                )
                if _count(cursor.fetchone()) != 0:
                    raise CanaryStartGuardError("model_projection_backlog_not_empty")

                cursor.execute(
                    """
                    SELECT count(*)
                    FROM observer.model_fatal_latches
                    WHERE site_id = %s AND processing_purpose = %s
                    """,
                    (site_id, processing_purpose),
                )
                if _count(cursor.fetchone()) != 0:
                    raise CanaryStartGuardError("model_fatal_latch_closed")

                cursor.execute(
                    """
                    SELECT
                        instance.status,
                        instance.control_revision,
                        checkpoint.cursor_value,
                        checkpoint.checkpoint_version,
                        checkpoint.lease_owner,
                        checkpoint.lease_expires_at,
                        checkpoint.last_error_code,
                        checkpoint.status
                    FROM observer.connector_instances AS instance
                    LEFT JOIN observer.connector_checkpoints AS checkpoint
                      ON checkpoint.site_id = instance.site_id
                     AND checkpoint.connector = instance.connector
                     AND checkpoint.connector_instance_id = instance.connector_instance_id
                    WHERE instance.site_id = %s
                      AND instance.connector = %s
                      AND instance.connector_instance_id = %s
                    """,
                    (site_id, connector, connector_instance_id),
                )
                checkpoint = cursor.fetchone()
                if checkpoint is not None and not _safe_checkpoint(
                    checkpoint,
                    initial_checkpoint=initial_checkpoint,
                ):
                    raise CanaryStartGuardError("checkpoint_binding_unsafe")

                cursor.execute(
                    """
                    SELECT count(*)
                    FROM observer.connector_control_commands
                    WHERE site_id = %s
                      AND connector = %s
                      AND connector_instance_id = %s
                    """,
                    (site_id, connector, connector_instance_id),
                )
                if _count(cursor.fetchone()) != 0:
                    raise CanaryStartGuardError("connector_control_binding_unsafe")
        except CanaryStartGuardError:
            raise
        except Exception as exc:
            raise CanaryStartGuardError("database_guard_failed") from exc


class PostgresContextCanaryStartGuardRepository:
    """Context-role proof that no old draft can enter the canary window."""

    __slots__ = ("_connection",)

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def assert_safe(self, *, site_id: str, processing_purpose: str) -> None:
        try:
            with self._connection.transaction(), self._connection.cursor() as cursor:
                _bind_read_only_session(
                    cursor,
                    site_id=site_id,
                    processing_purpose=processing_purpose,
                    expected_user="gbos_context_app",
                )
                cursor.execute(
                    """
                    SELECT count(*)
                    FROM context.communication_draft_outbox
                    WHERE site_id = %s
                      AND status IN ('pending', 'running', 'retry')
                    """,
                    (site_id,),
                )
                if _count(cursor.fetchone()) != 0:
                    raise CanaryStartGuardError("communication_draft_backlog_not_empty")
        except CanaryStartGuardError:
            raise
        except Exception as exc:
            raise CanaryStartGuardError("database_guard_failed") from exc


class PostgresAgentCanaryStartGuardRepository:
    """Agent-role proof that no old task or materialization can run."""

    __slots__ = ("_connection",)

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def assert_safe(self, *, site_id: str, processing_purpose: str) -> None:
        try:
            with self._connection.transaction(), self._connection.cursor() as cursor:
                _bind_read_only_session(
                    cursor,
                    site_id=site_id,
                    processing_purpose=processing_purpose,
                    expected_user="gbos_agent_app",
                )
                cursor.execute(
                    """
                    SELECT count(*)
                    FROM agent_runtime.agent_tasks
                    WHERE site_id = %s
                      AND (
                          status IN ('leased', 'running')
                          OR (
                              status IN ('queued', 'recheck')
                              AND due_at <= now()
                          )
                      )
                    """,
                    (site_id,),
                )
                if _count(cursor.fetchone()) != 0:
                    raise CanaryStartGuardError("agent_task_backlog_not_empty")
                cursor.execute(
                    """
                    SELECT count(*)
                    FROM agent_runtime.proposal_materialization_outbox
                    WHERE site_id = %s
                      AND status IN ('pending', 'running', 'retry')
                    """,
                    (site_id,),
                )
                if _count(cursor.fetchone()) != 0:
                    raise CanaryStartGuardError("materialization_backlog_not_empty")
        except CanaryStartGuardError:
            raise
        except Exception as exc:
            raise CanaryStartGuardError("database_guard_failed") from exc


def run_canary_start_guard(
    *,
    config_path: Path,
    manifest_path: Path,
    control_path: Path,
    connector_config_path: Path,
    connector: Callable[..., object] | None = None,
) -> None:
    """Bind private controls, prove persisted state, and close the DB connection."""

    try:
        config = load_canary_start_guard_config(config_path)
    except (OSError, ProjectionConfigError, RuntimeSupportError) as exc:
        raise CanaryStartGuardError("guard_configuration_invalid") from exc

    try:
        manifest, manifest_bytes = _private_json(manifest_path)
        control, _control_bytes = _private_json(control_path)
        _validate_bindings(
            manifest=manifest,
            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            control=control,
            expected_site_id=config.site_id,
            expected_processing_purpose=config.processing_purpose,
        )
        _private_json(connector_config_path)
        channels = load_channel_config(
            connector_config_path,
            expected_site_id=config.site_id,
            manifest=manifest,
        )
        credential = load_channel_credential(channels, "email")
        if not isinstance(credential, EmailCredentialConfig) or not credential.initial_checkpoint:
            raise CanaryStartGuardError("checkpoint_binding_unsafe")
    except CanaryStartGuardError:
        raise
    except (ChannelConfigError, OSError, TypeError, ValueError) as exc:
        raise CanaryStartGuardError("canary_binding_invalid") from exc

    connections: list[object] = []
    try:
        observer_connection = connect_postgres(config.connections["observer"], connector=connector)
        connections.append(observer_connection)
        context_connection = connect_postgres(config.connections["context"], connector=connector)
        connections.append(context_connection)
        agent_connection = connect_postgres(config.connections["agent"], connector=connector)
        connections.append(agent_connection)
        PostgresCanaryStartGuardRepository(observer_connection).assert_safe(
            site_id=config.site_id,
            processing_purpose=config.processing_purpose,
            connector="email",
            connector_instance_id=credential.instance_id,
            initial_checkpoint=credential.initial_checkpoint,
        )
        PostgresContextCanaryStartGuardRepository(context_connection).assert_safe(
            site_id=config.site_id,
            processing_purpose=config.processing_purpose,
        )
        PostgresAgentCanaryStartGuardRepository(agent_connection).assert_safe(
            site_id=config.site_id,
            processing_purpose=config.processing_purpose,
        )
    except CanaryStartGuardError:
        raise
    except Exception as exc:
        raise CanaryStartGuardError("database_guard_failed") from exc
    finally:
        for connection in connections:
            close = getattr(connection, "close", None)
            if callable(close):
                with suppress(Exception):
                    close()


def _validate_bindings(
    *,
    manifest: Mapping[str, Any],
    manifest_sha256: str,
    control: Mapping[str, Any],
    expected_site_id: str,
    expected_processing_purpose: str,
) -> None:
    channels = manifest.get("channels")
    email = channels.get("email") if isinstance(channels, Mapping) else None
    deepseek = manifest.get("deepseek")
    capabilities = manifest.get("capabilities")
    if (
        expected_processing_purpose != _PURPOSE
        or manifest.get("schema_version") != "1.0"
        or manifest.get("mode") != "local_pilot"
        or manifest.get("site_id") != expected_site_id
        or manifest.get("production_go") is not False
        or manifest.get("local_pilot_go") is not True
        or manifest.get("local_pilot_status") != "ready"
        or not isinstance(email, Mapping)
        or email.get("enabled") is not True
        or email.get("backfill_history") is not False
        or not isinstance(deepseek, Mapping)
        or deepseek.get("enabled") is not True
        or deepseek.get("kill_switch") is not False
        or deepseek.get("model") != _MODEL
        or not isinstance(capabilities, Mapping)
        or capabilities.get("external_send") is not False
        or capabilities.get("formal_business_commands") is not False
        or control.get("schema_version") != "1.1"
        or control.get("state") != "prepared"
        or control.get("manifest_sha256") != manifest_sha256
        or control.get("activation_time") != email.get("activation_time")
        or control.get("scope")
        != {
            "channels": ["email"],
            "model": _MODEL,
            "external_send": False,
            "formal_commands": False,
        }
    ):
        raise CanaryStartGuardError("canary_binding_invalid")


def _private_json(path: Path) -> tuple[dict[str, Any], bytes]:
    descriptor = -1
    try:
        before = path.lstat()
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        details = os.fstat(descriptor)
        if (
            stat.S_ISLNK(before.st_mode)
            or before.st_dev != details.st_dev
            or before.st_ino != details.st_ino
            or not stat.S_ISREG(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o600
            or not 0 < details.st_size <= _MAX_JSON_BYTES
        ):
            raise ValueError
        payload = os.read(descriptor, _MAX_JSON_BYTES + 1)
        after = os.fstat(descriptor)
        if len(payload) != details.st_size or after.st_size != details.st_size:
            raise ValueError
    except (OSError, ValueError) as exc:
        raise CanaryStartGuardError("canary_binding_invalid") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        value = json.loads(payload, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CanaryStartGuardError("canary_binding_invalid") from exc
    if not isinstance(value, dict):
        raise CanaryStartGuardError("canary_binding_invalid")
    return value, payload


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _count(row: object) -> int:
    if (
        not isinstance(row, tuple)
        or len(row) != 1
        or not isinstance(row[0], int)
        or isinstance(row[0], bool)
        or row[0] < 0
    ):
        raise CanaryStartGuardError("database_guard_failed")
    return row[0]


def _bind_read_only_session(
    cursor: Any,
    *,
    site_id: str,
    processing_purpose: str,
    expected_user: str,
) -> None:
    cursor.execute("SET TRANSACTION READ ONLY")
    cursor.execute("SELECT set_config('app.site_id', %s, true)", (site_id,))
    cursor.execute(
        "SELECT set_config('app.processing_purpose', %s, true)",
        (processing_purpose,),
    )
    cursor.execute("SELECT current_user, current_setting('transaction_read_only')")
    if cursor.fetchone() != (expected_user, "on"):
        raise CanaryStartGuardError("least_privilege_read_only_role_required")


def _safe_checkpoint(row: object, *, initial_checkpoint: str) -> bool:
    if not isinstance(row, tuple) or len(row) != 8:
        return False
    (
        instance_status,
        control_revision,
        cursor_value,
        checkpoint_version,
        lease_owner,
        lease_expires_at,
        last_error_code,
        checkpoint_status,
    ) = row
    return (
        instance_status == "healthy"
        and control_revision == 0
        and cursor_value == initial_checkpoint
        and isinstance(checkpoint_version, int)
        and not isinstance(checkpoint_version, bool)
        and checkpoint_version >= 1
        and lease_owner is None
        and lease_expires_at is None
        and last_error_code is None
        and checkpoint_status == "healthy"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/config/canary-start-guard.json"))
    parser.add_argument("--manifest", type=Path, default=Path("/config/local-pilot-manifest.json"))
    parser.add_argument("--control", type=Path, default=Path("/config/canary-run.json"))
    parser.add_argument("--connectors", type=Path, default=Path("/config/connectors.json"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        run_canary_start_guard(
            config_path=args.config,
            manifest_path=args.manifest,
            control_path=args.control,
            connector_config_path=args.connectors,
        )
    except CanaryStartGuardError as exc:
        print(f"CANARY START GUARD FAILED: {exc}", file=sys.stderr)
        return 78
    print("Canary start database guard passed with model egress still disabled.")
    return 0


__all__ = [
    "CanaryStartGuardError",
    "PostgresAgentCanaryStartGuardRepository",
    "PostgresCanaryStartGuardRepository",
    "PostgresContextCanaryStartGuardRepository",
    "main",
    "run_canary_start_guard",
]


if __name__ == "__main__":
    raise SystemExit(main())
