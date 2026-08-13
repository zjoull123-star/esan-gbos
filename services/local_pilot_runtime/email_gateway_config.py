"""Closed, default-off composition for the independent Email Gateway."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from .runtime_support import ComponentSettings, PostgresSettings, WorkerSettings

EMAIL_GATEWAY_API_URL = "http://email-gateway-api:8004"
OBSERVER_CONFIG_API_URL = "http://observer-api:8003"
EMAIL_PUBLICATION_AUTH_REF = "observer-email-publication-v1"
EMAIL_GATEWAY_BFF_AUTH_REF = "email-gateway-bff-v1"
MAILBOX_PROJECTION_AUTH_REF = "gateway-mailbox-projection-v1"

_TOP_FIELDS = frozenset(
    {
        "schema_version",
        "site_id",
        "external_send",
        "postgres",
        "endpoints",
        "auth",
        "listen",
        "components",
        "worker",
        "mailboxes",
    }
)
_POSTGRES_FIELDS = frozenset(
    {"host", "port", "database", "user", "password_file", "connect_timeout_seconds"}
)
_ENDPOINT_FIELDS = frozenset({"email_gateway_api", "observer_config_api"})
_AUTH_FIELDS = frozenset(
    {
        "email_gateway_data_key_file",
        "email_publication_bearer_file",
        "email_publication_auth_ref",
        "email_gateway_bff_bearer_file",
        "email_gateway_bff_auth_ref",
        "mailbox_projection_bearer_file",
        "mailbox_projection_auth_ref",
    }
)
_LISTEN_FIELDS = frozenset({"host", "port"})
_COMPONENT_FIELDS = frozenset({"enabled", "kill_switch"})
COMPONENT_NAMES = (
    "email_gateway_api",
    "email_gateway_worker",
    "email_publication_worker",
    "mailbox_config_projection_worker",
)
_WORKER_FIELDS = frozenset({"worker_id", "idle_delay_seconds", "heartbeat_interval_seconds"})
_MAILBOX_FIELDS = frozenset(
    {
        "mailbox_ref",
        "provider_kind",
        "business_mode",
        "enabled",
        "cutover_publication_revision",
        "activation_watermark",
        "legacy_migration",
        "backfill_history",
    }
)
_ROLE_PASSWORD_FILES = {
    "gbos_email_gateway_app": "/run/secrets/postgres_email_gateway_password",
    "gbos_email_gateway_worker": "/run/secrets/postgres_email_gateway_password",
    "gbos_observer_publisher": "/run/secrets/postgres_observer_publisher_password",
}
_MAX_CONFIG_BYTES = 65_536


class EmailGatewayConfigError(RuntimeError):
    """Gateway composition was not an exact, secret-safe local declaration."""


@dataclass(frozen=True, slots=True)
class EmailGatewayEndpoints:
    email_gateway_api: str
    observer_config_api: str


@dataclass(frozen=True, slots=True, repr=False)
class EmailGatewayAuth:
    email_gateway_data_key_file: Path
    email_publication_bearer_file: Path
    email_publication_auth_ref: str
    email_gateway_bff_bearer_file: Path
    email_gateway_bff_auth_ref: str
    mailbox_projection_bearer_file: Path
    mailbox_projection_auth_ref: str


@dataclass(frozen=True, slots=True)
class EmailGatewayListen:
    host: str
    port: int


@dataclass(frozen=True, slots=True)
class MailboxRuntimeDeclaration:
    mailbox_ref: str
    provider_kind: Literal["fake", "imap_smtp", "wecom_app_mail"]
    business_mode: Literal["primary", "selective_archive", "migration"]
    enabled: bool
    cutover_publication_revision: int
    activation_watermark: str
    legacy_migration: bool
    backfill_history: Literal[False]


@dataclass(frozen=True, slots=True, repr=False)
class EmailGatewayRuntimeConfig:
    site_id: str
    external_send: Literal[False]
    postgres: PostgresSettings
    endpoints: EmailGatewayEndpoints
    auth: EmailGatewayAuth
    listen: EmailGatewayListen
    components: Mapping[str, ComponentSettings]
    worker: WorkerSettings
    mailboxes: tuple[MailboxRuntimeDeclaration, ...]
    schema_version: Literal["1.0"] = "1.0"


def load_email_gateway_config(path: Path) -> EmailGatewayRuntimeConfig:
    config_path = Path(path)
    if (
        not config_path.is_file()
        or config_path.is_symlink()
        or config_path.stat().st_size > _MAX_CONFIG_BYTES
    ):
        raise EmailGatewayConfigError("gateway config is absent, unsafe, or unbounded")
    try:
        value = json.loads(config_path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EmailGatewayConfigError("gateway config is invalid JSON") from exc
    if (
        not isinstance(value, dict)
        or set(value) != _TOP_FIELDS
        or value.get("schema_version") != "1.0"
    ):
        raise EmailGatewayConfigError("gateway config must use the closed v1 schema")
    site_id = _text(value.get("site_id"), "site_id", maximum=140)
    if value.get("external_send") is not False:
        raise EmailGatewayConfigError("external send must remain disabled")
    postgres = _postgres(_mapping(value.get("postgres"), "postgres"))
    endpoints_value = _mapping(value.get("endpoints"), "endpoints")
    if set(endpoints_value) != _ENDPOINT_FIELDS or endpoints_value != {
        "email_gateway_api": EMAIL_GATEWAY_API_URL,
        "observer_config_api": OBSERVER_CONFIG_API_URL,
    }:
        raise EmailGatewayConfigError("gateway endpoints must be exact local services")
    auth = _auth(_mapping(value.get("auth"), "auth"))
    listen = _listen(_mapping(value.get("listen"), "listen"))
    components_value = _mapping(value.get("components"), "components")
    if set(components_value) != set(COMPONENT_NAMES):
        raise EmailGatewayConfigError("gateway component set must be closed")
    components: dict[str, ComponentSettings] = {}
    for name in COMPONENT_NAMES:
        raw = _mapping(components_value.get(name), "component")
        if set(raw) != _COMPONENT_FIELDS:
            raise EmailGatewayConfigError("gateway component config must be closed")
        enabled = raw.get("enabled")
        kill_switch = raw.get("kill_switch")
        if (
            not isinstance(enabled, bool)
            or not isinstance(kill_switch, bool)
            or enabled == kill_switch
        ):
            raise EmailGatewayConfigError("gateway component switch binding is invalid")
        components[name] = ComponentSettings(
            enabled=enabled,
            kill_switch=kill_switch,
            provider_mode="disabled",
            synthetic_e2e=False,
        )
    worker = _worker(_mapping(value.get("worker"), "worker"))
    mailboxes_value = value.get("mailboxes")
    if not isinstance(mailboxes_value, list) or len(mailboxes_value) > 100:
        raise EmailGatewayConfigError("gateway mailbox list must be bounded")
    mailboxes = tuple(_mailbox(item) for item in mailboxes_value)
    if len({item.mailbox_ref for item in mailboxes}) != len(mailboxes):
        raise EmailGatewayConfigError("gateway mailbox list contains duplicates")
    return EmailGatewayRuntimeConfig(
        site_id=site_id,
        external_send=False,
        postgres=postgres,
        endpoints=EmailGatewayEndpoints(EMAIL_GATEWAY_API_URL, OBSERVER_CONFIG_API_URL),
        auth=auth,
        listen=listen,
        components=MappingProxyType(components),
        worker=worker,
        mailboxes=mailboxes,
    )


def require_gateway_component(config: EmailGatewayRuntimeConfig, name: str) -> ComponentSettings:
    try:
        component = config.components[name]
    except KeyError as exc:
        raise EmailGatewayConfigError("gateway component is not configured") from exc
    if not component.enabled or component.kill_switch:
        raise EmailGatewayConfigError("gateway component is disabled")
    return component


def _postgres(value: Mapping[str, Any]) -> PostgresSettings:
    if set(value) != _POSTGRES_FIELDS:
        raise EmailGatewayConfigError("gateway postgres config must be closed")
    user = _text(value.get("user"), "postgres user", maximum=128)
    expected_file = _ROLE_PASSWORD_FILES.get(user)
    if expected_file is None or value.get("password_file") != expected_file:
        raise EmailGatewayConfigError("gateway database role secret binding is invalid")
    if value.get("host") != "postgres" or value.get("database") != "gbos_local_pilot":
        raise EmailGatewayConfigError("gateway database target is invalid")
    port = _int(value.get("port"), "postgres port", minimum=1, maximum=65_535)
    timeout = _int(value.get("connect_timeout_seconds"), "connect timeout", minimum=1, maximum=10)
    return PostgresSettings(
        host="postgres",
        port=port,
        database="gbos_local_pilot",
        user=user,
        password_file=Path(expected_file),
        connect_timeout_seconds=timeout,
    )


def _auth(value: Mapping[str, Any]) -> EmailGatewayAuth:
    if set(value) != _AUTH_FIELDS:
        raise EmailGatewayConfigError("gateway auth config must be closed")
    if (
        value.get("email_publication_bearer_file") != "/run/secrets/email_publication_bearer"
        or value.get("email_gateway_data_key_file") != "/run/secrets/email_gateway_data_key"
        or value.get("email_gateway_bff_bearer_file") != "/run/secrets/email_gateway_bff_bearer"
        or value.get("mailbox_projection_bearer_file") != "/run/secrets/mailbox_projection_bearer"
        or value.get("email_publication_auth_ref") != EMAIL_PUBLICATION_AUTH_REF
        or value.get("email_gateway_bff_auth_ref") != EMAIL_GATEWAY_BFF_AUTH_REF
        or value.get("mailbox_projection_auth_ref") != MAILBOX_PROJECTION_AUTH_REF
    ):
        raise EmailGatewayConfigError("gateway auth reference is invalid")
    return EmailGatewayAuth(
        email_gateway_data_key_file=Path("/run/secrets/email_gateway_data_key"),
        email_publication_bearer_file=Path("/run/secrets/email_publication_bearer"),
        email_publication_auth_ref=EMAIL_PUBLICATION_AUTH_REF,
        email_gateway_bff_bearer_file=Path("/run/secrets/email_gateway_bff_bearer"),
        email_gateway_bff_auth_ref=EMAIL_GATEWAY_BFF_AUTH_REF,
        mailbox_projection_bearer_file=Path("/run/secrets/mailbox_projection_bearer"),
        mailbox_projection_auth_ref=MAILBOX_PROJECTION_AUTH_REF,
    )


def _listen(value: Mapping[str, Any]) -> EmailGatewayListen:
    if set(value) != _LISTEN_FIELDS or value.get("host") != "0.0.0.0":
        raise EmailGatewayConfigError("gateway listen target is invalid")
    port = _int(value.get("port"), "listen port", minimum=1, maximum=65_535)
    if port != 8004:
        raise EmailGatewayConfigError("gateway listen port is invalid")
    return EmailGatewayListen(host="0.0.0.0", port=8004)


def _worker(value: Mapping[str, Any]) -> WorkerSettings:
    if set(value) != _WORKER_FIELDS:
        raise EmailGatewayConfigError("gateway worker config must be closed")
    worker_id = _text(value.get("worker_id"), "worker_id", maximum=256)
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
        raise EmailGatewayConfigError("gateway worker timing is invalid")
    return WorkerSettings(worker_id, float(idle), float(heartbeat))


def _mailbox(value: object) -> MailboxRuntimeDeclaration:
    raw = _mapping(value, "mailbox")
    if set(raw) != _MAILBOX_FIELDS:
        raise EmailGatewayConfigError("gateway mailbox declaration must be closed")
    mailbox_ref = _text(raw.get("mailbox_ref"), "mailbox_ref", maximum=80)
    provider = raw.get("provider_kind")
    mode = raw.get("business_mode")
    enabled = raw.get("enabled")
    revision = _int(
        raw.get("cutover_publication_revision"), "cutover revision", minimum=1, maximum=2**63 - 1
    )
    watermark = _text(raw.get("activation_watermark"), "activation watermark", maximum=4096)
    legacy = raw.get("legacy_migration")
    if provider not in {"fake", "imap_smtp", "wecom_app_mail"}:
        raise EmailGatewayConfigError("gateway mailbox provider is invalid")
    if mode not in {"primary", "selective_archive", "migration"}:
        raise EmailGatewayConfigError("gateway mailbox business mode is invalid")
    if (
        not isinstance(enabled, bool)
        or not isinstance(legacy, bool)
        or raw.get("backfill_history") is not False
    ):
        raise EmailGatewayConfigError("gateway mailbox activation is invalid")
    if legacy and (
        provider != "imap_smtp" or mode not in {"selective_archive", "migration"} or enabled
    ):
        raise EmailGatewayConfigError("legacy IMAP must remain a disabled migration mailbox")
    return MailboxRuntimeDeclaration(
        mailbox_ref=mailbox_ref,
        provider_kind=provider,
        business_mode=mode,
        enabled=enabled,
        cutover_publication_revision=revision,
        activation_watermark=watermark,
        legacy_migration=legacy,
        backfill_history=False,
    )


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise EmailGatewayConfigError(f"gateway {name} must be an object")
    return value


def _text(value: object, name: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(char in value for char in "\x00\r\n")
    ):
        raise EmailGatewayConfigError(f"gateway {name} is invalid")
    return value


def _int(value: object, name: str, *, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise EmailGatewayConfigError(f"gateway {name} is invalid")
    return value


__all__ = [
    "COMPONENT_NAMES",
    "EMAIL_GATEWAY_API_URL",
    "EMAIL_GATEWAY_BFF_AUTH_REF",
    "EMAIL_PUBLICATION_AUTH_REF",
    "MAILBOX_PROJECTION_AUTH_REF",
    "OBSERVER_CONFIG_API_URL",
    "EmailGatewayConfigError",
    "EmailGatewayRuntimeConfig",
    "MailboxRuntimeDeclaration",
    "load_email_gateway_config",
    "require_gateway_component",
]
