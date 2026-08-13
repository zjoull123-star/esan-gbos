"""Closed, secret-safe local channel configuration loading."""

from __future__ import annotations

import json
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Protocol

from .secret_provider import (
    MountedFileSecretProvider,
    SecretBytes,
    SecretProviderError,
    SecretSpec,
)

_CONFIG_FIELDS = frozenset(
    {"schema_version", "site_id", "external_send", "evidence_cas_root", "channels"}
)
_CHANNEL_FIELDS = frozenset(
    {
        "enabled",
        "kill_switch",
        "activation_time",
        "backfill_history",
        "credential_file",
    }
)
_CHANNEL_NAMES = frozenset({"email", "wecom", "whatsapp", "media"})
_EMAIL_FIELDS = frozenset(
    {
        "instance_id",
        "team_ref",
        "agent_task_type",
        "account_user_ref",
        "host",
        "port",
        "mailbox",
        "folder",
        "username",
        "password",
        "poll_limit",
        "max_message_bytes",
        "max_attachment_bytes",
        "max_attachments",
        "rescan_max_window_seconds",
        "rescan_max_uids",
        "initial_checkpoint",
    }
)
_WHATSAPP_FIELDS = frozenset(
    {
        "instance_id",
        "team_ref",
        "agent_task_type",
        "account_user_ref",
        "app_secret",
        "verify_token",
        "path",
        "max_body_bytes",
    }
)
_WECOM_FIELDS = frozenset(
    {
        "instance_id",
        "team_ref",
        "agent_task_type",
        "account_user_ref",
        "corp_id",
        "secret",
        "private_key",
        "initial_checkpoint",
    }
)
_TASK_TYPES = frozenset({"sales", "purchase", "product_sample", "ceo"})
_WEBHOOK_PATH = re.compile(r"^/[A-Za-z0-9][A-Za-z0-9_/-]{0,254}$")
_MAX_CONFIG_BYTES = 65_536
_MAX_CREDENTIAL_BYTES = 65_536


class ChannelConfigError(RuntimeError):
    """A channel config boundary failed closed without rendering provider data."""


@dataclass(frozen=True, slots=True, repr=False)
class ChannelSettings:
    enabled: bool
    kill_switch: bool
    activation_time: datetime | None
    backfill_history: bool
    credential_file: Path

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(enabled={self.enabled!r}, "
            f"kill_switch={self.kill_switch!r}, "
            f"activation_time={self.activation_time!r}, "
            f"backfill_history={self.backfill_history!r}, "
            "credential_file=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ChannelConfig:
    site_id: str
    external_send: Literal[False]
    evidence_cas_root: Path
    channels: Mapping[str, ChannelSettings]
    schema_version: Literal["1.0"] = "1.0"

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(site_id={self.site_id!r}, external_send=False, "
            "evidence_cas_root=<redacted>, "
            f"channel_count={len(self.channels)})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class EmailCredentialConfig:
    instance_id: str
    team_ref: str | None
    agent_task_type: str | None
    account_user_ref: str | None
    host: str
    port: int
    mailbox: str
    folder: str
    username: str
    password: str
    poll_limit: int
    max_message_bytes: int
    max_attachment_bytes: int
    max_attachments: int
    rescan_max_window_seconds: int
    rescan_max_uids: int
    initial_checkpoint: str | None

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(instance_id=<redacted>, routing=<redacted>, "
            "account_user_ref=<redacted>, "
            "host=<redacted>, port=<redacted>, mailbox=<redacted>, folder=<redacted>, "
            "username=<redacted>, password=<redacted>, "
            f"poll_limit={self.poll_limit}, max_message_bytes={self.max_message_bytes}, "
            f"max_attachment_bytes={self.max_attachment_bytes}, "
            f"max_attachments={self.max_attachments}, "
            f"rescan_max_window_seconds={self.rescan_max_window_seconds}, "
            f"rescan_max_uids={self.rescan_max_uids}, initial_checkpoint=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class WhatsAppCredentialConfig:
    instance_id: str
    team_ref: str | None
    agent_task_type: str | None
    account_user_ref: str | None
    app_secret: str
    verify_token: str
    path: str
    max_body_bytes: int

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(instance_id=<redacted>, routing=<redacted>, "
            "account_user_ref=<redacted>, "
            "app_secret=<redacted>, verify_token=<redacted>, "
            f"path={self.path!r}, max_body_bytes={self.max_body_bytes})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class WeComCredentialConfig:
    instance_id: str
    team_ref: str | None
    agent_task_type: str | None
    account_user_ref: str | None
    corp_id: str
    secret: str
    private_key: str
    initial_checkpoint: str | None

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(instance_id=<redacted>, routing=<redacted>, "
            "account_user_ref=<redacted>, "
            "corp_id=<redacted>, secret=<redacted>, private_key=<redacted>, "
            "initial_checkpoint=<redacted>)"
        )


ChannelCredential = EmailCredentialConfig | WhatsAppCredentialConfig | WeComCredentialConfig


@dataclass(frozen=True, slots=True)
class LegacyImapMailboxMigration:
    mailbox_ref: str
    provider_kind: Literal["imap_smtp"]
    business_mode: Literal["selective_archive", "migration"]
    enabled: Literal[False]
    cutover_publication_revision: int
    activation_watermark: str
    backfill_history: Literal[False]


class ChannelCredentialSecretProvider(Protocol):
    """Narrow provider surface required by channel credential loading."""

    def read_json_bytes(self, name: str) -> SecretBytes | None: ...


def load_channel_config(
    path: Path,
    *,
    expected_site_id: str,
    manifest: Mapping[str, Any],
) -> ChannelConfig:
    """Load and bind the closed connector config to runtime and manifest site state."""

    value = _read_json_object(Path(path), maximum=_MAX_CONFIG_BYTES, private=False)
    if set(value) != _CONFIG_FIELDS or value.get("schema_version") != "1.0":
        raise ChannelConfigError("connector config must use the closed v1 schema")
    site_id = _text(value, "site_id", maximum=140)
    if (
        site_id != expected_site_id
        or manifest.get("site_id") != expected_site_id
        or manifest.get("production_go") is not False
        or manifest.get("local_pilot_go") is not True
        or manifest.get("local_pilot_status") not in {"ready", "running"}
    ):
        raise ChannelConfigError("connector config site or local manifest binding is invalid")
    if value.get("external_send") is not False:
        raise ChannelConfigError("connector runtime external send must remain disabled")
    root = _absolute_path(value, "evidence_cas_root")
    raw_channels = value.get("channels")
    manifest_channels = manifest.get("channels")
    if (
        not isinstance(raw_channels, dict)
        or set(raw_channels) != _CHANNEL_NAMES
        or not isinstance(manifest_channels, Mapping)
        or set(manifest_channels) != _CHANNEL_NAMES
    ):
        raise ChannelConfigError("connector channel set must be closed")
    channels: dict[str, ChannelSettings] = {}
    for name in sorted(_CHANNEL_NAMES):
        raw = raw_channels.get(name)
        manifest_channel = manifest_channels.get(name)
        if not isinstance(raw, dict) or set(raw) != _CHANNEL_FIELDS:
            raise ChannelConfigError("connector channel config must be closed")
        if not isinstance(manifest_channel, Mapping):
            raise ChannelConfigError("connector manifest channel is invalid")
        channel = _channel(raw)
        if (
            manifest_channel.get("enabled") is not channel.enabled
            or manifest_channel.get("activation_time") != raw.get("activation_time")
            or manifest_channel.get("backfill_history") is not channel.backfill_history
        ):
            raise ChannelConfigError("connector config does not match manifest channel state")
        channels[name] = channel
    return ChannelConfig(
        site_id=site_id,
        external_send=False,
        evidence_cas_root=root,
        channels=MappingProxyType(channels),
    )


def require_active_channel(
    config: ChannelConfig,
    name: str,
    *,
    now: datetime,
) -> ChannelSettings:
    """Require explicit activation at or before an aware runtime clock."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ChannelConfigError("channel activation clock is invalid")
    try:
        channel = config.channels[name]
    except KeyError as exc:
        raise ChannelConfigError("channel is not configured") from exc
    if (
        not channel.enabled
        or channel.kill_switch
        or channel.backfill_history
        or channel.activation_time is None
        or now < channel.activation_time
    ):
        raise ChannelConfigError("channel is disabled or has not reached activation")
    return channel


def translate_legacy_imap_mailbox(
    config: ChannelConfig,
    *,
    mailbox_ref: str,
    cutover_publication_revision: int,
    activation_watermark: str,
    business_mode: Literal["selective_archive", "migration"],
) -> LegacyImapMailboxMigration:
    """Describe a non-backfilled cutover without reading the legacy credential."""

    email = config.channels.get("email")
    if (
        email is None
        or email.enabled
        or not email.kill_switch
        or email.activation_time is not None
        or email.backfill_history
        or business_mode not in {"selective_archive", "migration"}
        or not isinstance(mailbox_ref, str)
        or not mailbox_ref
        or mailbox_ref != mailbox_ref.strip()
        or len(mailbox_ref) > 80
        or not isinstance(cutover_publication_revision, int)
        or isinstance(cutover_publication_revision, bool)
        or cutover_publication_revision < 1
        or not isinstance(activation_watermark, str)
        or not activation_watermark
        or activation_watermark != activation_watermark.strip()
        or len(activation_watermark) > 4096
    ):
        raise ChannelConfigError("legacy IMAP cutover must remain disabled and bounded")
    return LegacyImapMailboxMigration(
        mailbox_ref=mailbox_ref,
        provider_kind="imap_smtp",
        business_mode=business_mode,
        enabled=False,
        cutover_publication_revision=cutover_publication_revision,
        activation_watermark=activation_watermark,
        backfill_history=False,
    )


def load_channel_credential(config: ChannelConfig, name: str) -> ChannelCredential:
    """Load a path-configured credential through the strict secret provider."""

    try:
        path = config.channels[name].credential_file
    except KeyError as exc:
        raise ChannelConfigError("channel is not configured") from exc
    if name not in {"email", "wecom", "whatsapp"}:
        raise ChannelConfigError("channel does not accept provider credentials")
    logical_name = f"{name}_credential"
    try:
        provider = MountedFileSecretProvider(
            path.parent,
            (
                SecretSpec(
                    logical_name,
                    path.name,
                    "closed_json",
                    1,
                    _MAX_CREDENTIAL_BYTES,
                ),
            ),
        )
    except SecretProviderError:
        raise ChannelConfigError("channel credential provider request failed") from None
    return load_channel_credential_from_provider(config, name, provider)


def load_channel_credential_from_provider(
    config: ChannelConfig,
    name: str,
    provider: ChannelCredentialSecretProvider,
) -> ChannelCredential:
    """Load one channel credential from its closed deployment logical name."""

    if name not in config.channels:
        raise ChannelConfigError("channel is not configured")
    if name not in {"email", "wecom", "whatsapp"}:
        raise ChannelConfigError("channel does not accept provider credentials")
    try:
        secret = provider.read_json_bytes(f"{name}_credential")
    except SecretProviderError:
        raise ChannelConfigError("channel credential provider request failed") from None
    if not isinstance(secret, SecretBytes):
        raise ChannelConfigError("channel credential provider request failed")
    value = _decode_json_object(secret.reveal())
    if name == "email":
        return _email(value)
    if name == "whatsapp":
        return _whatsapp(value)
    if name == "wecom":
        return _wecom(value)
    raise ChannelConfigError("channel does not accept provider credentials")


def _channel(value: Mapping[str, Any]) -> ChannelSettings:
    enabled = value.get("enabled")
    kill_switch = value.get("kill_switch")
    backfill = value.get("backfill_history")
    if not isinstance(enabled, bool) or not isinstance(kill_switch, bool):
        raise ChannelConfigError("channel enablement flags are invalid")
    if backfill is not False:
        raise ChannelConfigError("channel history backfill is forbidden")
    activation = _optional_datetime(value.get("activation_time"))
    if enabled and (kill_switch or activation is None):
        raise ChannelConfigError("enabled channel requires activation and an open kill switch")
    return ChannelSettings(
        enabled=enabled,
        kill_switch=kill_switch,
        activation_time=activation,
        backfill_history=False,
        credential_file=_absolute_path(value, "credential_file"),
    )


def _email(value: Mapping[str, Any]) -> EmailCredentialConfig:
    if not _matches_credential_fields(value, _EMAIL_FIELDS):
        raise ChannelConfigError("email credential must use the closed schema")
    team_ref, task = _routing(value)
    return EmailCredentialConfig(
        instance_id=_text(value, "instance_id", maximum=256),
        team_ref=team_ref,
        agent_task_type=task,
        account_user_ref=_account_user_ref(value.get("account_user_ref")),
        host=_text(value, "host", maximum=253),
        port=_integer(value, "port", minimum=1, maximum=65_535),
        mailbox=_text(value, "mailbox", maximum=256),
        folder=_text(value, "folder", maximum=256),
        username=_text(value, "username", maximum=4_096),
        password=_text(value, "password", maximum=4_096),
        poll_limit=_integer(value, "poll_limit", minimum=1, maximum=1_000),
        max_message_bytes=_integer(value, "max_message_bytes", minimum=1, maximum=100_000_000),
        max_attachment_bytes=_integer(
            value, "max_attachment_bytes", minimum=1, maximum=100_000_000
        ),
        max_attachments=_integer(value, "max_attachments", minimum=1, maximum=1_000),
        rescan_max_window_seconds=_integer(
            value,
            "rescan_max_window_seconds",
            minimum=1,
            maximum=90 * 86_400,
        ),
        rescan_max_uids=_integer(value, "rescan_max_uids", minimum=1, maximum=10_000),
        initial_checkpoint=_optional_text(value.get("initial_checkpoint"), maximum=4_096),
    )


def _whatsapp(value: Mapping[str, Any]) -> WhatsAppCredentialConfig:
    if not _matches_credential_fields(value, _WHATSAPP_FIELDS):
        raise ChannelConfigError("WhatsApp credential must use the closed schema")
    team_ref, task = _routing(value)
    path = _text(value, "path", maximum=255)
    if _WEBHOOK_PATH.fullmatch(path) is None:
        raise ChannelConfigError("WhatsApp credential path is invalid")
    return WhatsAppCredentialConfig(
        instance_id=_text(value, "instance_id", maximum=256),
        team_ref=team_ref,
        agent_task_type=task,
        account_user_ref=_account_user_ref(value.get("account_user_ref")),
        app_secret=_text(value, "app_secret", maximum=4_096),
        verify_token=_text(value, "verify_token", maximum=4_096),
        path=path,
        max_body_bytes=_integer(value, "max_body_bytes", minimum=1, maximum=16_777_216),
    )


def _wecom(value: Mapping[str, Any]) -> WeComCredentialConfig:
    if not _matches_credential_fields(value, _WECOM_FIELDS):
        raise ChannelConfigError("WeCom credential must use the closed schema")
    team_ref, task = _routing(value)
    return WeComCredentialConfig(
        instance_id=_text(value, "instance_id", maximum=256),
        team_ref=team_ref,
        agent_task_type=task,
        account_user_ref=_account_user_ref(value.get("account_user_ref")),
        corp_id=_text(value, "corp_id", maximum=256),
        secret=_text(value, "secret", maximum=4_096),
        private_key=_text(value, "private_key", maximum=16_384),
        initial_checkpoint=_optional_text(value.get("initial_checkpoint"), maximum=4_096),
    )


def _routing(value: Mapping[str, Any]) -> tuple[str | None, str | None]:
    team_ref = _optional_text(value.get("team_ref"), maximum=256)
    task = value.get("agent_task_type")
    if task is not None and task not in _TASK_TYPES:
        raise ChannelConfigError("credential routing task is invalid")
    if task is not None and team_ref is None:
        raise ChannelConfigError("credential task routing requires a team")
    return team_ref, task


def _matches_credential_fields(
    value: Mapping[str, Any],
    fields: frozenset[str],
) -> bool:
    provided = set(value)
    return provided <= fields and fields - {"account_user_ref"} <= provided


def _account_user_ref(value: object) -> str | None:
    account_user_ref = _optional_text(value, maximum=256)
    if account_user_ref is not None and any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in account_user_ref
    ):
        raise ChannelConfigError("credential account user reference is invalid")
    return account_user_ref


def _read_json_object(path: Path, *, maximum: int, private: bool) -> dict[str, Any]:
    try:
        details = path.lstat()
    except OSError as exc:
        raise ChannelConfigError("required configuration file is unavailable") from exc
    if (
        not stat.S_ISREG(details.st_mode)
        or path.is_symlink()
        or not 0 < details.st_size <= maximum
        or (private and stat.S_IMODE(details.st_mode) != 0o600)
    ):
        raise ChannelConfigError("configuration file is unsafe or unbounded")
    return _decode_json_object(path.read_bytes())


def _decode_json_object(payload: bytes) -> dict[str, Any]:
    try:
        decoded = json.loads(payload, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ChannelConfigError("configuration file is invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise ChannelConfigError("configuration root must be an object")
    return decoded


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _text(value: Mapping[str, Any], key: str, *, maximum: int) -> str:
    item = value.get(key)
    if (
        not isinstance(item, str)
        or not item
        or item != item.strip()
        or len(item.encode("utf-8")) > maximum
        or any(character in item for character in ("\x00", "\r", "\n"))
    ):
        raise ChannelConfigError("credential contains an invalid bounded string")
    return item


def _optional_text(value: object, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _text({"value": value}, "value", maximum=maximum)


def _integer(
    value: Mapping[str, Any],
    key: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    item = value.get(key)
    if type(item) is not int or not minimum <= item <= maximum:
        raise ChannelConfigError("credential integer is outside its allowed boundary")
    return item


def _absolute_path(value: Mapping[str, Any], key: str) -> Path:
    raw = _text(value, key, maximum=4_096)
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts or str(path) != raw:
        raise ChannelConfigError("configuration path must be absolute and normalized")
    return path


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise ChannelConfigError("channel activation time is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ChannelConfigError("channel activation time is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ChannelConfigError("channel activation time must be timezone-aware")
    return parsed


__all__ = [
    "ChannelConfig",
    "ChannelConfigError",
    "ChannelCredential",
    "ChannelCredentialSecretProvider",
    "ChannelSettings",
    "EmailCredentialConfig",
    "LegacyImapMailboxMigration",
    "WeComCredentialConfig",
    "WhatsAppCredentialConfig",
    "load_channel_config",
    "load_channel_credential",
    "load_channel_credential_from_provider",
    "require_active_channel",
    "translate_legacy_imap_mailbox",
]
