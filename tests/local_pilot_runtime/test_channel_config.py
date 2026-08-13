from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from services.local_pilot_runtime.channel_config import (
    ChannelConfigError,
    EmailCredentialConfig,
    WeComCredentialConfig,
    WhatsAppCredentialConfig,
    load_channel_config,
    load_channel_credential,
    load_channel_credential_from_provider,
    require_active_channel,
    translate_legacy_imap_mailbox,
)
from services.local_pilot_runtime.secret_provider import SecretBytes, SecretProviderError

NOW = datetime(2026, 8, 8, 9, tzinfo=UTC)


def _private_json(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    return path


def _manifest(*, site_id: str = "alpha.example") -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "mode": "local_pilot",
        "site_id": site_id,
        "production_go": False,
        "local_pilot_go": True,
        "local_pilot_status": "ready",
        "channels": {
            name: {
                "enabled": name in {"email", "whatsapp"},
                "activation_time": (
                    "2026-08-08T09:00:00Z" if name in {"email", "whatsapp"} else None
                ),
                "backfill_history": False,
                **({"credential_ref": None} if name != "media" else {"local_only": True}),
            }
            for name in ("email", "wecom", "whatsapp", "media")
        },
    }


def _connectors(tmp_path: Path, *, site_id: str = "alpha.example") -> Path:
    channels = {}
    for name in ("email", "wecom", "whatsapp", "media"):
        channels[name] = {
            "enabled": name in {"email", "whatsapp"},
            "kill_switch": name not in {"email", "whatsapp"},
            "activation_time": ("2026-08-08T09:00:00Z" if name in {"email", "whatsapp"} else None),
            "backfill_history": False,
            "credential_file": str(tmp_path / f"{name}.json"),
        }
    return _private_json(
        tmp_path / "connectors.json",
        {
            "schema_version": "1.0",
            "site_id": site_id,
            "external_send": False,
            "evidence_cas_root": str(tmp_path / "cas"),
            "channels": channels,
        },
    )


def test_closed_config_binds_site_manifest_and_requires_activation_without_backfill(
    tmp_path: Path,
) -> None:
    config = load_channel_config(
        _connectors(tmp_path),
        expected_site_id="alpha.example",
        manifest=_manifest(),
    )

    active = require_active_channel(config, "email", now=NOW)

    assert active.activation_time == NOW
    assert config.external_send is False
    assert config.evidence_cas_root == tmp_path / "cas"
    assert "credential_file=<redacted>" in repr(active)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"unknown": True}),
        lambda value: value.update({"external_send": True}),
        lambda value: value.update({"site_id": "other.example"}),
        lambda value: value["channels"]["email"].update({"backfill_history": True}),
        lambda value: value["channels"]["email"].update({"activation_time": None}),
        lambda value: value["channels"]["email"].update({"kill_switch": True}),
    ],
)
def test_config_rejects_closed_schema_site_send_backfill_and_activation_failures(
    tmp_path: Path,
    mutation: object,
) -> None:
    path = _connectors(tmp_path)
    value = json.loads(path.read_text(encoding="utf-8"))
    mutation(value)  # type: ignore[operator]
    _private_json(path, value)

    with pytest.raises(ChannelConfigError):
        config = load_channel_config(
            path,
            expected_site_id="alpha.example",
            manifest=_manifest(),
        )
        require_active_channel(config, "email", now=NOW)


def test_email_credential_is_exact_private_bounded_and_redacted(tmp_path: Path) -> None:
    config = load_channel_config(
        _connectors(tmp_path),
        expected_site_id="alpha.example",
        manifest=_manifest(),
    )
    secret = {
        "instance_id": "email-primary",
        "team_ref": "team:sales",
        "agent_task_type": "sales",
        "account_user_ref": "owner@example.invalid",
        "host": "imap.example.invalid",
        "port": 993,
        "mailbox": "pilot-primary",
        "folder": "INBOX",
        "username": "private@example.invalid",
        "password": "not-a-real-password",
        "poll_limit": 25,
        "max_message_bytes": 1_000_000,
        "max_attachment_bytes": 100_000,
        "max_attachments": 5,
        "rescan_max_window_seconds": 86_400,
        "rescan_max_uids": 100,
        "initial_checkpoint": None,
    }
    _private_json(tmp_path / "email.json", secret)

    credential = load_channel_credential(config, "email")

    assert isinstance(credential, EmailCredentialConfig)
    assert credential.folder == "INBOX"
    assert credential.account_user_ref == "owner@example.invalid"
    rendered = repr(credential)
    assert secret["username"] not in rendered
    assert secret["password"] not in rendered
    assert secret["account_user_ref"] not in rendered
    assert "username=<redacted>" in rendered

    secret["extra"] = "rejected"
    _private_json(tmp_path / "email.json", secret)
    with pytest.raises(ChannelConfigError):
        load_channel_credential(config, "email")


@pytest.mark.parametrize("case", ["mode", "symlink", "oversize"])
def test_credential_file_rejects_unsafe_mode_symlink_and_oversize(
    tmp_path: Path,
    case: str,
) -> None:
    config = load_channel_config(
        _connectors(tmp_path),
        expected_site_id="alpha.example",
        manifest=_manifest(),
    )
    target = _private_json(tmp_path / "real.json", {"placeholder": "x"})
    credential_path = tmp_path / "email.json"
    if case == "mode":
        credential_path.write_bytes(target.read_bytes())
        credential_path.chmod(0o644)
    elif case == "symlink":
        credential_path.symlink_to(target)
    else:
        credential_path.write_bytes(b"{" + b"x" * 70_000)
        credential_path.chmod(0o600)

    with pytest.raises(ChannelConfigError) as caught:
        load_channel_credential(config, "email")

    assert "placeholder" not in repr(caught.value)


def test_whatsapp_credential_schema_is_exact_and_redacted(tmp_path: Path) -> None:
    config = load_channel_config(
        _connectors(tmp_path),
        expected_site_id="alpha.example",
        manifest=_manifest(),
    )
    secret = {
        "instance_id": "wa-primary",
        "team_ref": None,
        "agent_task_type": None,
        "account_user_ref": "USER-WHATSAPP-OWNER",
        "app_secret": "not-a-real-app-secret",
        "verify_token": "not-a-real-verify-token",
        "path": "/webhooks/whatsapp",
        "max_body_bytes": 1_048_576,
    }
    _private_json(tmp_path / "whatsapp.json", secret)

    credential = load_channel_credential(config, "whatsapp")

    assert isinstance(credential, WhatsAppCredentialConfig)
    assert credential.path == "/webhooks/whatsapp"
    assert credential.account_user_ref == "USER-WHATSAPP-OWNER"
    rendered = repr(credential)
    assert secret["app_secret"] not in rendered
    assert secret["verify_token"] not in rendered
    assert secret["account_user_ref"] not in rendered
    assert "app_secret=<redacted>" in rendered


def test_wecom_credential_accepts_explicit_null_account_owner_and_redacts_it(
    tmp_path: Path,
) -> None:
    config = load_channel_config(
        _connectors(tmp_path),
        expected_site_id="alpha.example",
        manifest=_manifest(),
    )
    secret = {
        "instance_id": "wecom-primary",
        "team_ref": "team:sales",
        "agent_task_type": "sales",
        "account_user_ref": None,
        "corp_id": "not-a-real-corp",
        "secret": "not-a-real-secret",
        "private_key": "not-a-real-key",
        "initial_checkpoint": "100",
    }
    _private_json(tmp_path / "wecom.json", secret)

    credential = load_channel_credential(config, "wecom")

    assert isinstance(credential, WeComCredentialConfig)
    assert credential.account_user_ref is None
    assert "account_user_ref=<redacted>" in repr(credential)


@pytest.mark.parametrize(
    "account_user_ref",
    ["", " owner@example.invalid", "owner example.invalid", "owner\texample.invalid", "x" * 257],
)
def test_account_user_ref_rejects_empty_whitespace_controls_and_oversize(
    tmp_path: Path,
    account_user_ref: str,
) -> None:
    config = load_channel_config(
        _connectors(tmp_path),
        expected_site_id="alpha.example",
        manifest=_manifest(),
    )
    _private_json(
        tmp_path / "whatsapp.json",
        {
            "instance_id": "wa-primary",
            "team_ref": None,
            "agent_task_type": None,
            "account_user_ref": account_user_ref,
            "app_secret": "not-a-real-app-secret",
            "verify_token": "not-a-real-verify-token",
            "path": "/webhooks/whatsapp",
            "max_body_bytes": 1_048_576,
        },
    )

    with pytest.raises(ChannelConfigError):
        load_channel_credential(config, "whatsapp")


@pytest.mark.parametrize(
    ("name", "payload", "expected_type"),
    [
        (
            "email",
            {
                "instance_id": "email-primary",
                "team_ref": "team:sales",
                "agent_task_type": "sales",
                "account_user_ref": "owner@example.invalid",
                "host": "imap.example.invalid",
                "port": 993,
                "mailbox": "pilot-primary",
                "folder": "INBOX",
                "username": "provider-user@example.invalid",
                "password": "PROVIDER-EMAIL-SECRET",
                "poll_limit": 25,
                "max_message_bytes": 1_000_000,
                "max_attachment_bytes": 100_000,
                "max_attachments": 5,
                "rescan_max_window_seconds": 86_400,
                "rescan_max_uids": 100,
                "initial_checkpoint": None,
            },
            EmailCredentialConfig,
        ),
        (
            "wecom",
            {
                "instance_id": "wecom-primary",
                "team_ref": "team:sales",
                "agent_task_type": "sales",
                "account_user_ref": None,
                "corp_id": "provider-corp",
                "secret": "PROVIDER-WECOM-SECRET",
                "private_key": "PROVIDER-WECOM-PRIVATE-KEY",
                "initial_checkpoint": "100",
            },
            WeComCredentialConfig,
        ),
        (
            "whatsapp",
            {
                "instance_id": "wa-primary",
                "team_ref": None,
                "agent_task_type": None,
                "account_user_ref": "USER-WHATSAPP-OWNER",
                "app_secret": "PROVIDER-WHATSAPP-SECRET",
                "verify_token": "PROVIDER-WHATSAPP-TOKEN",
                "path": "/webhooks/whatsapp",
                "max_body_bytes": 1_048_576,
            },
            WhatsAppCredentialConfig,
        ),
    ],
)
def test_channel_credentials_consume_secret_bytes_from_exact_logical_names(
    tmp_path: Path,
    name: str,
    payload: dict[str, object],
    expected_type: type[object],
) -> None:
    config = load_channel_config(
        _connectors(tmp_path),
        expected_site_id="alpha.example",
        manifest=_manifest(),
    )
    requested: list[str] = []

    class Provider:
        def read_json_bytes(self, logical_name: str) -> SecretBytes:
            requested.append(logical_name)
            return SecretBytes(json.dumps(payload).encode())

    credential = load_channel_credential_from_provider(config, name, Provider())

    assert isinstance(credential, expected_type)
    assert requested == [f"{name}_credential"]
    rendered = repr(credential)
    assert not any(
        value in rendered
        for value in payload.values()
        if isinstance(value, str) and value.startswith("PROVIDER-")
    )


def test_channel_provider_json_keeps_duplicate_rejection_and_hides_provider_errors(
    tmp_path: Path,
) -> None:
    config = load_channel_config(
        _connectors(tmp_path),
        expected_site_id="alpha.example",
        manifest=_manifest(),
    )

    class DuplicateProvider:
        def read_json_bytes(self, logical_name: str) -> SecretBytes:
            assert logical_name == "email_credential"
            return SecretBytes(b'{"instance_id":"first","instance_id":"SECRET-DUPLICATE"}')

    with pytest.raises(ChannelConfigError) as duplicate:
        load_channel_credential_from_provider(config, "email", DuplicateProvider())
    assert "SECRET-DUPLICATE" not in repr(duplicate.value)

    class FailingProvider:
        def read_json_bytes(self, logical_name: str) -> SecretBytes:
            assert logical_name == "email_credential"
            raise SecretProviderError("SECRET-PROVIDER-DETAIL")

    with pytest.raises(
        ChannelConfigError,
        match="channel credential provider request failed",
    ) as failed:
        load_channel_credential_from_provider(config, "email", FailingProvider())
    assert "SECRET-PROVIDER-DETAIL" not in repr(failed.value)


def test_legacy_imap_translation_is_disabled_selective_archive_without_secret_read(
    tmp_path: Path,
) -> None:
    path = _connectors(tmp_path)
    value = json.loads(path.read_text())
    value["channels"]["email"].update(
        {"enabled": False, "kill_switch": True, "activation_time": None}
    )
    manifest = _manifest()
    manifest["channels"]["email"].update({"enabled": False, "activation_time": None})
    config = load_channel_config(
        _private_json(path, value),
        expected_site_id="alpha.example",
        manifest=manifest,
    )

    translated = translate_legacy_imap_mailbox(
        config,
        mailbox_ref="MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        cutover_publication_revision=9,
        activation_watermark="uidvalidity:42;uid:100",
        business_mode="selective_archive",
    )

    assert translated.enabled is False
    assert translated.backfill_history is False
    assert translated.provider_kind == "imap_smtp"
    assert translated.cutover_publication_revision == 9
