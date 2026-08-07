from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from services.local_pilot_runtime.channel_config import (
    ChannelConfigError,
    EmailCredentialConfig,
    WhatsAppCredentialConfig,
    load_channel_config,
    load_channel_credential,
    require_active_channel,
)

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
    rendered = repr(credential)
    assert secret["username"] not in rendered
    assert secret["password"] not in rendered
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
        "app_secret": "not-a-real-app-secret",
        "verify_token": "not-a-real-verify-token",
        "path": "/webhooks/whatsapp",
        "max_body_bytes": 1_048_576,
    }
    _private_json(tmp_path / "whatsapp.json", secret)

    credential = load_channel_credential(config, "whatsapp")

    assert isinstance(credential, WhatsAppCredentialConfig)
    assert credential.path == "/webhooks/whatsapp"
    rendered = repr(credential)
    assert secret["app_secret"] not in rendered
    assert secret["verify_token"] not in rendered
    assert "app_secret=<redacted>" in rendered
