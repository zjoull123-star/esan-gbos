"""Secret-safe, import-safe composition support for local pilot services."""

from __future__ import annotations

import ipaddress
import json
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

_CONFIG_FIELDS = frozenset(
    {
        "schema_version",
        "site_id",
        "postgres",
        "auth",
        "context_endpoint",
        "listen",
        "components",
        "worker",
    }
)
_POSTGRES_FIELDS = frozenset(
    {
        "host",
        "port",
        "database",
        "user",
        "password_file",
        "connect_timeout_seconds",
    }
)
_AUTH_FIELDS = frozenset(
    {
        "agent_api_bearer_file",
        "context_api_bearer_file",
        "context_client_bearer_file",
        "context_auth_ref",
    }
)
_ENDPOINT_FIELDS = frozenset({"base_url", "unix_socket"})
_LISTEN_FIELDS = frozenset({"host", "agent_api_port", "context_api_port"})
_COMPONENT_FIELDS = frozenset({"enabled", "kill_switch", "provider_mode", "synthetic_e2e"})
_WORKER_FIELDS = frozenset({"worker_id", "idle_delay_seconds", "heartbeat_interval_seconds"})
_COMPONENT_NAMES = (
    "agent_api",
    "context_api",
    "agent_worker",
    "model_worker",
)
_MAX_CONFIG_BYTES = 65_536
_MAX_SECRET_BYTES = 4096

ProviderMode = Literal["disabled", "deterministic", "deepseek"]


class RuntimeSupportError(RuntimeError):
    """A local runtime configuration or secret boundary failed closed."""


@dataclass(frozen=True, slots=True)
class SecretValue:
    _value: str = field(repr=False)

    def reveal(self) -> str:
        return self._value

    def __str__(self) -> str:
        return "<redacted>"


@dataclass(frozen=True, slots=True)
class PostgresSettings:
    host: str
    port: int
    database: str
    user: str
    password_file: Path
    connect_timeout_seconds: int


@dataclass(frozen=True, slots=True)
class AuthSettings:
    agent_api_bearer_file: Path
    context_api_bearer_file: Path
    context_client_bearer_file: Path
    context_auth_ref: str


@dataclass(frozen=True, slots=True)
class ContextEndpointSettings:
    base_url: str
    unix_socket: Path | None


@dataclass(frozen=True, slots=True)
class ListenSettings:
    host: str
    agent_api_port: int
    context_api_port: int


@dataclass(frozen=True, slots=True)
class ComponentSettings:
    enabled: bool
    kill_switch: bool
    provider_mode: ProviderMode
    synthetic_e2e: bool


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    worker_id: str
    idle_delay_seconds: float
    heartbeat_interval_seconds: float


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    site_id: str
    postgres: PostgresSettings
    auth: AuthSettings
    context_endpoint: ContextEndpointSettings
    listen: ListenSettings
    components: Mapping[str, ComponentSettings]
    worker: WorkerSettings
    schema_version: Literal["1.0"] = "1.0"


def load_runtime_config(path: Path) -> RuntimeConfig:
    config_path = Path(path)
    if (
        not config_path.is_file()
        or config_path.is_symlink()
        or config_path.stat().st_size > _MAX_CONFIG_BYTES
    ):
        raise RuntimeSupportError("runtime config is absent, unsafe, or unbounded")
    try:
        value = json.loads(config_path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeSupportError("runtime config is invalid JSON") from exc
    if (
        not isinstance(value, dict)
        or set(value) != _CONFIG_FIELDS
        or value.get("schema_version") != "1.0"
    ):
        raise RuntimeSupportError("runtime config must use the closed v1 schema")
    site_id = _text(value, "site_id", maximum=140)
    postgres = _postgres(_mapping(value, "postgres"))
    auth = _auth(_mapping(value, "auth"))
    endpoint = _endpoint(_mapping(value, "context_endpoint"))
    listen = _listen(_mapping(value, "listen"))
    components_value = _mapping(value, "components")
    if set(components_value) != set(_COMPONENT_NAMES):
        raise RuntimeSupportError("runtime component config must be closed")
    components = {name: _component(_mapping(components_value, name)) for name in _COMPONENT_NAMES}
    worker = _worker(_mapping(value, "worker"))
    return RuntimeConfig(
        site_id=site_id,
        postgres=postgres,
        auth=auth,
        context_endpoint=endpoint,
        listen=listen,
        components=components,
        worker=worker,
    )


def load_secret_file(path: Path) -> SecretValue:
    secret_path = Path(path)
    try:
        details = secret_path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeSupportError("required secret file is absent") from exc
    if (
        not stat.S_ISREG(details.st_mode)
        or stat.S_IMODE(details.st_mode) != 0o600
        or secret_path.is_symlink()
    ):
        raise RuntimeSupportError("secret file must be a regular non-symlink with mode 0600")
    if not 0 < details.st_size <= _MAX_SECRET_BYTES:
        raise RuntimeSupportError("secret file is empty or unbounded")
    try:
        value = secret_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeSupportError("secret file is not valid UTF-8") from exc
    if value.endswith("\n"):
        value = value[:-1]
    if not value or "\x00" in value or "\r" in value or "\n" in value:
        raise RuntimeSupportError("secret file contains invalid characters")
    return SecretValue(value)


def reject_plaintext_secret_environment(environ: Mapping[str, str]) -> None:
    for name, value in environ.items():
        normalized = name.upper()
        if not value or normalized.endswith("_FILE"):
            continue
        if (
            "PASSWORD" in normalized
            or "SECRET" in normalized
            or normalized.endswith("TOKEN")
            or normalized.endswith("BEARER")
            or normalized.endswith("API_KEY")
        ):
            raise RuntimeSupportError("plaintext secret environment variables are forbidden")


def connect_postgres(
    settings: PostgresSettings,
    *,
    connector: Callable[..., object] | None = None,
) -> object:
    password = load_secret_file(settings.password_file)
    if connector is None:
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeSupportError("psycopg is required for local PostgreSQL") from exc
        active_connector: Callable[..., object] = psycopg.connect
    else:
        active_connector = connector
    try:
        return active_connector(
            host=settings.host,
            port=settings.port,
            dbname=settings.database,
            user=settings.user,
            password=password.reveal(),
            connect_timeout=settings.connect_timeout_seconds,
        )
    except RuntimeSupportError:
        raise
    except Exception as exc:
        raise RuntimeSupportError("local PostgreSQL connection failed") from exc


def validate_manifest_binding(
    manifest: Mapping[str, Any],
    config: RuntimeConfig,
) -> None:
    if (
        manifest.get("schema_version") != "1.0"
        or manifest.get("mode") != "local_pilot"
        or manifest.get("site_id") != config.site_id
        or manifest.get("production_go") is not False
        or manifest.get("local_pilot_go") is not True
        or manifest.get("local_pilot_status") not in {"ready", "running"}
    ):
        raise RuntimeSupportError("runtime config does not match an enabled local manifest")


def component_settings(config: RuntimeConfig, name: str) -> ComponentSettings:
    try:
        component = config.components[name]
    except KeyError as exc:
        raise RuntimeSupportError("runtime component is not configured") from exc
    if not component.enabled or component.kill_switch:
        raise RuntimeSupportError("runtime component is disabled by its kill switch")
    return component


def close_connection(connection: object) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()


def _postgres(value: Mapping[str, Any]) -> PostgresSettings:
    if set(value) != _POSTGRES_FIELDS:
        raise RuntimeSupportError("postgres runtime config must be closed")
    host = _text(value, "host", maximum=255)
    if not _local_postgres_host(host):
        raise RuntimeSupportError("PostgreSQL host must be a local runtime target")
    return PostgresSettings(
        host=host,
        port=_port(value, "port"),
        database=_text(value, "database", maximum=128),
        user=_text(value, "user", maximum=128),
        password_file=_absolute_path(value, "password_file"),
        connect_timeout_seconds=_bounded_int(
            value,
            "connect_timeout_seconds",
            minimum=1,
            maximum=10,
        ),
    )


def _auth(value: Mapping[str, Any]) -> AuthSettings:
    if set(value) != _AUTH_FIELDS:
        raise RuntimeSupportError("auth runtime config must be closed")
    return AuthSettings(
        agent_api_bearer_file=_absolute_path(value, "agent_api_bearer_file"),
        context_api_bearer_file=_absolute_path(value, "context_api_bearer_file"),
        context_client_bearer_file=_absolute_path(value, "context_client_bearer_file"),
        context_auth_ref=_text(value, "context_auth_ref", maximum=256),
    )


def _endpoint(value: Mapping[str, Any]) -> ContextEndpointSettings:
    if set(value) != _ENDPOINT_FIELDS:
        raise RuntimeSupportError("Context endpoint config must be closed")
    base_url = _text(value, "base_url", maximum=2048)
    socket_value = value.get("unix_socket")
    if socket_value is not None and not isinstance(socket_value, str):
        raise RuntimeSupportError("Context unix socket must be a path or null")
    unix_socket = (
        None
        if socket_value is None
        else _absolute_path({"unix_socket": socket_value}, "unix_socket")
    )
    return ContextEndpointSettings(base_url=base_url, unix_socket=unix_socket)


def _listen(value: Mapping[str, Any]) -> ListenSettings:
    if set(value) != _LISTEN_FIELDS:
        raise RuntimeSupportError("listen runtime config must be closed")
    host = _text(value, "host", maximum=64)
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise RuntimeSupportError("listen host must be a literal local address") from exc
    if not (address.is_loopback or address.is_unspecified):
        raise RuntimeSupportError("listen host must be local")
    return ListenSettings(
        host=host,
        agent_api_port=_port(value, "agent_api_port"),
        context_api_port=_port(value, "context_api_port"),
    )


def _component(value: Mapping[str, Any]) -> ComponentSettings:
    if set(value) != _COMPONENT_FIELDS:
        raise RuntimeSupportError("component runtime config must be closed")
    enabled = value.get("enabled")
    kill_switch = value.get("kill_switch")
    synthetic_e2e = value.get("synthetic_e2e")
    provider_mode = value.get("provider_mode")
    if (
        not isinstance(enabled, bool)
        or not isinstance(kill_switch, bool)
        or not isinstance(synthetic_e2e, bool)
        or provider_mode not in {"disabled", "deterministic", "deepseek"}
    ):
        raise RuntimeSupportError("component runtime config is invalid")
    return ComponentSettings(
        enabled=enabled,
        kill_switch=kill_switch,
        provider_mode=provider_mode,
        synthetic_e2e=synthetic_e2e,
    )


def _worker(value: Mapping[str, Any]) -> WorkerSettings:
    if set(value) != _WORKER_FIELDS:
        raise RuntimeSupportError("worker runtime config must be closed")
    idle = value.get("idle_delay_seconds")
    heartbeat = value.get("heartbeat_interval_seconds")
    if (
        not isinstance(idle, int | float)
        or isinstance(idle, bool)
        or not 0 < idle <= 60
        or not isinstance(heartbeat, int | float)
        or isinstance(heartbeat, bool)
        or not 0 < heartbeat <= 60
    ):
        raise RuntimeSupportError("worker timing config is invalid")
    return WorkerSettings(
        worker_id=_text(value, "worker_id", maximum=256),
        idle_delay_seconds=float(idle),
        heartbeat_interval_seconds=float(heartbeat),
    )


def _local_postgres_host(host: str) -> bool:
    if host == "postgres":
        return True
    if host.startswith("/"):
        return ".." not in Path(host).parts
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    item = value.get(key)
    if not isinstance(item, dict):
        raise RuntimeSupportError(f"{key} must be an object")
    return item


def _text(value: Mapping[str, Any], key: str, *, maximum: int) -> str:
    item = value.get(key)
    if (
        not isinstance(item, str)
        or not item
        or len(item) > maximum
        or any(character in item for character in ("\x00", "\r", "\n"))
    ):
        raise RuntimeSupportError(f"{key} must be a bounded string")
    return item


def _absolute_path(value: Mapping[str, Any], key: str) -> Path:
    raw = _text(value, key, maximum=4096)
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise RuntimeSupportError(f"{key} must be an absolute normalized path")
    return path


def _bounded_int(
    value: Mapping[str, Any],
    key: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool) or not minimum <= item <= maximum:
        raise RuntimeSupportError(f"{key} is outside its allowed range")
    return item


def _port(value: Mapping[str, Any], key: str) -> int:
    return _bounded_int(value, key, minimum=1, maximum=65535)


__all__ = [
    "AuthSettings",
    "ComponentSettings",
    "ContextEndpointSettings",
    "ListenSettings",
    "PostgresSettings",
    "RuntimeConfig",
    "RuntimeSupportError",
    "SecretValue",
    "WorkerSettings",
    "close_connection",
    "component_settings",
    "connect_postgres",
    "load_runtime_config",
    "load_secret_file",
    "reject_plaintext_secret_environment",
    "validate_manifest_binding",
]
