"""Closed three-role configuration for production model projection composition."""

from __future__ import annotations

import json
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from .runtime_support import (
    PostgresSettings,
    RuntimeSupportError,
    TextSecretProvider,
    load_secret_file,
)

_CONFIG_FIELDS = frozenset(
    {
        "schema_version",
        "site_id",
        "controlled_egress",
        "evidence_cas_root",
        "tokenizer_vault_root",
        "connections",
    }
)
_CONNECTION_FIELDS = frozenset(
    {
        "host",
        "port",
        "database",
        "user",
        "password_file",
        "connect_timeout_seconds",
    }
)
_START_GUARD_FIELDS = frozenset({"schema_version", "site_id", "processing_purpose", "connections"})
_ROLE_USERS = {
    "observer": "gbos_observer_app",
    "context": "gbos_context_app",
    "agent": "gbos_agent_app",
}
_MAX_CONFIG_BYTES = 65_536


class ProjectionConfigError(RuntimeError):
    """The projection connection boundary failed closed."""


@dataclass(frozen=True, slots=True, repr=False)
class ProjectionConfig:
    site_id: str
    controlled_egress: bool
    evidence_cas_root: Path
    tokenizer_vault_root: Path
    connections: Mapping[str, PostgresSettings]
    schema_version: Literal["1.0"] = "1.0"

    def __repr__(self) -> str:
        return (
            "ProjectionConfig("
            f"site_id={self.site_id!r}, controlled_egress={self.controlled_egress!r}, "
            "roots=<redacted>, connections=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class CanaryStartGuardConfig:
    site_id: str
    processing_purpose: str
    connections: Mapping[str, PostgresSettings]
    schema_version: Literal["1.1"] = "1.1"

    def __repr__(self) -> str:
        return (
            "CanaryStartGuardConfig("
            f"site_id={self.site_id!r}, processing_purpose={self.processing_purpose!r}, "
            "connections=<redacted>)"
        )


def load_projection_config(
    path: Path,
    *,
    expected_site_id: str,
    secret_provider: TextSecretProvider | None = None,
) -> ProjectionConfig:
    """Load a mode-0600 closed JSON file with three independent local DB roles."""

    value = _read_private_json(Path(path))
    if set(value) != _CONFIG_FIELDS or value.get("schema_version") != "1.0":
        raise ProjectionConfigError("projection config must use the closed v1 schema")
    site_id = _text(value.get("site_id"), "site_id", maximum=140)
    if site_id != expected_site_id:
        raise ProjectionConfigError("projection config site binding is invalid")
    controlled_egress = value.get("controlled_egress")
    if not isinstance(controlled_egress, bool):
        raise ProjectionConfigError("controlled egress flag is invalid")
    cas_root = _safe_root(value.get("evidence_cas_root"), "evidence CAS root")
    vault_root = _safe_root(value.get("tokenizer_vault_root"), "tokenizer vault root")
    raw_connections = value.get("connections")
    if not isinstance(raw_connections, dict) or set(raw_connections) != set(_ROLE_USERS):
        raise ProjectionConfigError("projection connections must contain exactly three roles")
    connections: dict[str, PostgresSettings] = {}
    password_paths: set[Path] = set()
    for role in ("observer", "context", "agent"):
        raw = raw_connections.get(role)
        if not isinstance(raw, dict) or set(raw) != _CONNECTION_FIELDS:
            raise ProjectionConfigError("projection connection must use the closed schema")
        connection = _connection(
            raw,
            expected_user=_ROLE_USERS[role],
            logical_name=f"postgres_{role}_password",
            secret_provider=secret_provider,
        )
        resolved_password = connection.password_file.resolve(strict=True)
        if resolved_password in password_paths:
            raise ProjectionConfigError("projection roles cannot reuse one credential file")
        password_paths.add(resolved_password)
        connections[role] = connection
    return ProjectionConfig(
        site_id=site_id,
        controlled_egress=controlled_egress,
        evidence_cas_root=cas_root,
        tokenizer_vault_root=vault_root,
        connections=MappingProxyType(connections),
    )


def load_canary_start_guard_config(
    path: Path,
    *,
    secret_provider: TextSecretProvider | None = None,
) -> CanaryStartGuardConfig:
    """Load three independent read-only-role canary guard connections."""

    value = _read_private_json(Path(path))
    if set(value) != _START_GUARD_FIELDS or value.get("schema_version") != "1.1":
        raise ProjectionConfigError("canary start guard config must use the closed v1.1 schema")
    raw_connections = value.get("connections")
    if not isinstance(raw_connections, dict) or set(raw_connections) != set(_ROLE_USERS):
        raise ProjectionConfigError("canary start guard requires exactly three roles")
    connections: dict[str, PostgresSettings] = {}
    password_paths: set[Path] = set()
    for role in ("observer", "context", "agent"):
        raw = raw_connections.get(role)
        if not isinstance(raw, dict) or set(raw) != _CONNECTION_FIELDS:
            raise ProjectionConfigError("canary start guard connection must use the closed schema")
        connection = _connection(
            raw,
            expected_user=_ROLE_USERS[role],
            logical_name=f"postgres_{role}_password",
            secret_provider=secret_provider,
        )
        resolved_password = connection.password_file.resolve(strict=True)
        if resolved_password in password_paths:
            raise ProjectionConfigError("canary start guard roles cannot reuse credentials")
        password_paths.add(resolved_password)
        connections[role] = connection
    return CanaryStartGuardConfig(
        site_id=_text(value.get("site_id"), "site_id", maximum=140),
        processing_purpose=_text(
            value.get("processing_purpose"),
            "processing purpose",
            maximum=80,
        ),
        connections=MappingProxyType(connections),
    )


def _read_private_json(path: Path) -> dict[str, object]:
    try:
        details = path.lstat()
    except FileNotFoundError as exc:
        raise ProjectionConfigError("projection config is absent") from exc
    if (
        not stat.S_ISREG(details.st_mode)
        or stat.S_IMODE(details.st_mode) != 0o600
        or path.is_symlink()
        or not 0 < details.st_size <= _MAX_CONFIG_BYTES
    ):
        raise ProjectionConfigError("projection config must be a bounded mode-0600 regular file")
    try:
        value = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectionConfigError("projection config is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ProjectionConfigError("projection config must be a JSON object")
    return value


def _connection(
    value: dict[str, object],
    *,
    expected_user: str,
    logical_name: str,
    secret_provider: TextSecretProvider | None,
) -> PostgresSettings:
    host = _text(value.get("host"), "PostgreSQL host", maximum=253)
    port = _integer(value.get("port"), "PostgreSQL port", minimum=1, maximum=65_535)
    if host not in {"127.0.0.1", "::1", "localhost", "postgres"} or (
        host == "postgres" and port != 5432
    ):
        raise ProjectionConfigError("projection PostgreSQL endpoint is not allowed")
    database = _text(value.get("database"), "PostgreSQL database", maximum=63)
    user = _text(value.get("user"), "PostgreSQL user", maximum=63)
    if user != expected_user:
        raise ProjectionConfigError("projection PostgreSQL role is not least privilege")
    password_file = _absolute_path(value.get("password_file"), "password file")
    try:
        load_secret_file(
            password_file,
            secret_provider=secret_provider,
            logical_name=logical_name if secret_provider is not None else None,
        )
    except RuntimeSupportError as exc:
        raise ProjectionConfigError("projection password file was rejected") from exc
    timeout = _integer(
        value.get("connect_timeout_seconds"),
        "PostgreSQL connect timeout",
        minimum=1,
        maximum=30,
    )
    return PostgresSettings(
        host=host,
        port=port,
        database=database,
        user=user,
        password_file=password_file,
        connect_timeout_seconds=timeout,
    )


def _safe_root(value: object, name: str) -> Path:
    path = _absolute_path(value, name)
    try:
        details = path.lstat()
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ProjectionConfigError(f"{name} is absent") from exc
    if not stat.S_ISDIR(details.st_mode) or path.is_symlink() or resolved != path:
        raise ProjectionConfigError(f"{name} must be an absolute non-symlink directory")
    return path


def _absolute_path(value: object, name: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ProjectionConfigError(f"{name} is invalid")
    path = Path(value)
    if not path.is_absolute():
        raise ProjectionConfigError(f"{name} must be absolute")
    return path


def _text(value: object, name: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        raise ProjectionConfigError(f"{name} is invalid")
    return value


def _integer(value: object, name: str, *, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ProjectionConfigError(f"{name} is invalid")
    return value


__all__ = [
    "CanaryStartGuardConfig",
    "ProjectionConfig",
    "ProjectionConfigError",
    "load_canary_start_guard_config",
    "load_projection_config",
]
