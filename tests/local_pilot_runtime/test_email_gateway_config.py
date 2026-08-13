from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from services.local_pilot_runtime.email_gateway_config import (
    EMAIL_GATEWAY_API_URL,
    OBSERVER_CONFIG_API_URL,
    EmailGatewayConfigError,
    load_email_gateway_config,
)


def _private(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _value(tmp_path: Path) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "site_id": "alpha.example",
        "external_send": False,
        "postgres": {
            "host": "postgres",
            "port": 5432,
            "database": "gbos_local_pilot",
            "user": "gbos_email_gateway_app",
            "password_file": "/run/secrets/postgres_email_gateway_password",
            "connect_timeout_seconds": 5,
        },
        "endpoints": {
            "email_gateway_api": EMAIL_GATEWAY_API_URL,
            "observer_config_api": OBSERVER_CONFIG_API_URL,
        },
        "auth": {
            "email_gateway_data_key_file": "/run/secrets/email_gateway_data_key",
            "email_publication_bearer_file": "/run/secrets/email_publication_bearer",
            "email_publication_auth_ref": "observer-email-publication-v1",
            "email_gateway_bff_bearer_file": "/run/secrets/email_gateway_bff_bearer",
            "email_gateway_bff_auth_ref": "email-gateway-bff-v1",
            "mailbox_projection_bearer_file": "/run/secrets/mailbox_projection_bearer",
            "mailbox_projection_auth_ref": "gateway-mailbox-projection-v1",
            "observer_email_draft_material_bearer_file": (
                "/run/secrets/observer_email_draft_material_bearer"
            ),
            "observer_email_draft_material_auth_ref": ("observer-email-draft-material-v1"),
            "frappe_email_gateway_authority_api_key_file": (
                "/run/secrets/frappe_email_gateway_authority_api_key"
            ),
            "frappe_email_gateway_authority_api_secret_file": (
                "/run/secrets/frappe_email_gateway_authority_api_secret"
            ),
            "frappe_email_gateway_authority_auth_ref": "email-gateway-authority-v1",
        },
        "listen": {"host": "0.0.0.0", "port": 8004},
        "components": {
            name: {"enabled": False, "kill_switch": True}
            for name in (
                "email_gateway_api",
                "email_gateway_worker",
                "email_publication_worker",
                "mailbox_config_projection_worker",
            )
        },
        "worker": {
            "worker_id": "email-gateway-local-1",
            "idle_delay_seconds": 1.0,
            "heartbeat_interval_seconds": 5.0,
        },
        "mailboxes": [],
    }


def test_closed_config_defaults_off_and_uses_exact_internal_urls(tmp_path: Path) -> None:
    config = load_email_gateway_config(_private(tmp_path / "gateway.json", _value(tmp_path)))

    assert config.external_send is False
    assert config.endpoints.email_gateway_api == "http://email-gateway-api:8004"
    assert config.endpoints.observer_config_api == "http://observer-api:8003"
    assert config.auth.email_gateway_bff_bearer_file == Path(
        "/run/secrets/email_gateway_bff_bearer"
    )
    assert config.auth.email_gateway_bff_auth_ref == "email-gateway-bff-v1"
    assert config.auth.observer_email_draft_material_bearer_file == Path(
        "/run/secrets/observer_email_draft_material_bearer"
    )
    assert config.auth.observer_email_draft_material_auth_ref == (
        "observer-email-draft-material-v1"
    )
    assert config.auth.frappe_email_gateway_authority_api_key_file == Path(
        "/run/secrets/frappe_email_gateway_authority_api_key"
    )
    assert config.auth.frappe_email_gateway_authority_api_secret_file == Path(
        "/run/secrets/frappe_email_gateway_authority_api_secret"
    )
    assert config.auth.frappe_email_gateway_authority_auth_ref == ("email-gateway-authority-v1")
    assert all(
        not component.enabled and component.kill_switch for component in config.components.values()
    )
    assert config.mailboxes == ()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update({"unknown": True}),
        lambda value: value.update({"external_send": True}),
        lambda value: value["postgres"].update({"user": "gbos_observer_app"}),
        lambda value: value["postgres"].update({"password_file": "/tmp/inline"}),
        lambda value: value["endpoints"].update({"email_gateway_api": "https://example.invalid"}),
        lambda value: value["auth"].update({"email_publication_auth_ref": "wrong"}),
        lambda value: value["auth"].update({"email_gateway_bff_auth_ref": "wrong"}),
        lambda value: value["auth"].update({"email_gateway_bff_bearer_file": "/tmp/inline"}),
        lambda value: value["auth"].update({"observer_email_draft_material_auth_ref": "wrong"}),
        lambda value: value["auth"].update(
            {"frappe_email_gateway_authority_api_key_file": "/tmp/inline"}
        ),
        lambda value: value["auth"].update(
            {"frappe_email_gateway_authority_auth_ref": "email-command-publication-v1"}
        ),
        lambda value: value["components"]["email_gateway_api"].update({"kill_switch": False}),
    ],
)
def test_config_rejects_role_secret_url_auth_and_switch_drift(
    tmp_path: Path, mutate: object
) -> None:
    value = _value(tmp_path)
    mutate(value)  # type: ignore[operator]

    with pytest.raises(EmailGatewayConfigError):
        load_email_gateway_config(_private(tmp_path / "gateway.json", value))


def test_legacy_imap_translation_is_disabled_revisioned_and_never_backfills(
    tmp_path: Path,
) -> None:
    value = _value(tmp_path)
    value["mailboxes"] = [
        {
            "mailbox_ref": "MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "provider_kind": "imap_smtp",
            "business_mode": "selective_archive",
            "enabled": False,
            "cutover_publication_revision": 7,
            "activation_watermark": "uidvalidity:42;uid:100",
            "legacy_migration": True,
            "backfill_history": False,
        }
    ]

    config = load_email_gateway_config(_private(tmp_path / "gateway.json", value))

    mailbox = config.mailboxes[0]
    assert mailbox.provider_kind == "imap_smtp"
    assert mailbox.business_mode == "selective_archive"
    assert mailbox.enabled is False
    assert mailbox.cutover_publication_revision == 7
    assert mailbox.backfill_history is False
