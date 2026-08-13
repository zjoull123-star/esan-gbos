from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from services.local_pilot_runtime.email_command_publication_worker import (
    CommandPublicationRelayWorker,
    FrappeCommandPublicationClient,
    GatewayCommandIngestClient,
    PublicationRelayStatus,
    main,
)
from services.local_pilot_runtime.secret_provider import SecretText
from tests.email_gateway.fakes.provider import closed_command

NOW = datetime(2026, 8, 13, 13, 5, tzinfo=UTC)


class _Secrets:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.calls: list[str] = []

    def read_text(self, name: str) -> SecretText | None:
        self.calls.append(name)
        value = self.values.get(name)
        return None if value is None else SecretText(value)


class _Transport:
    def __init__(self, *responses: tuple[int, dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def post(self, **kwargs: object) -> tuple[int, dict[str, Any]]:
        self.calls.append(kwargs)
        return self.responses.pop(0)


def _frappe(message: dict[str, Any]) -> dict[str, Any]:
    return {"message": message}


def _claim() -> dict[str, Any]:
    command = closed_command()
    return {
        "publication": {
            "publication_ref": "PUB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "attempt": 1,
            "generation": 1,
            "fence_token": "FNC-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "lease_expires_at": "2026-08-13T13:05:30Z",
            "command": command,
            "payload_digest": "sha256:" + command["payload_sha256"],
        }
    }


def _lease() -> dict[str, Any]:
    claim = _claim()["publication"]
    return {
        key: claim[key]
        for key in (
            "publication_ref",
            "attempt",
            "generation",
            "fence_token",
            "lease_expires_at",
        )
    }


def _acknowledgement() -> dict[str, Any]:
    command = closed_command()
    return {
        "publication_ref": "PUB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "command_receipt_ref": "ECR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "send_outbox_ref": "SOB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "payload_digest": "sha256:" + command["payload_sha256"],
        "status": "acknowledged",
    }


def test_relay_uses_one_frappe_credential_and_separate_gateway_bearer_then_acks() -> None:
    command = closed_command()
    transport = _Transport(
        (200, _frappe(_claim())),
        (200, _frappe({"lease": _lease()})),
        (
            200,
            {
                "command_receipt_ref": "ECR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
                "send_outbox_ref": "SOB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
                "payload_digest": command["payload_sha256"],
            },
        ),
        (
            200,
            _frappe({"acknowledgement": _acknowledgement()}),
        ),
    )
    frappe = FrappeCommandPublicationClient(
        transport=transport,
        api_key="frappe-publication-key",
        api_secret="frappe-publication-secret",
    )
    gateway = GatewayCommandIngestClient(
        transport=transport,
        bearer_token="gateway-command-ingest-secret",
    )
    worker = CommandPublicationRelayWorker(
        frappe=frappe,
        gateway=gateway,
        site_id="gbos.localhost",
        worker_id="email-command-relay-01",
        clock=lambda: NOW,
        lease_seconds=30,
    )

    result = worker.run_once()

    assert result.status == PublicationRelayStatus.DELIVERED
    assert [str(call["url"]).rsplit(".", 1)[-1] for call in transport.calls[:2]] == [
        "claim",
        "heartbeat",
    ]
    gateway_call = transport.calls[2]
    assert gateway_call["url"] == (
        "http://email-gateway-api:8004/internal/v1/email-commands/accept"
    )
    assert gateway_call["headers"]["Authorization"] == (  # type: ignore[index]
        "Bearer gateway-command-ingest-secret"
    )
    assert "frappe-publication-secret" not in repr(gateway_call)
    assert transport.calls[3]["url"] == (
        "http://frappe-backend:8000/api/method/"
        "esan_gbos.api.internal.email_command_publication.acknowledge"
    )


def test_gateway_failure_releases_frappe_claim_with_fixed_safe_code_and_never_acks() -> None:
    transport = _Transport(
        (200, _frappe(_claim())),
        (200, _frappe({"lease": _lease()})),
        (503, {"error": {"code": "unavailable"}}),
        (
            200,
            _frappe(
                {
                    "release": {
                        "publication_ref": "PUB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
                        "status": "retry",
                        "safe_code": "gateway_unavailable",
                    }
                }
            ),
        ),
    )
    worker = CommandPublicationRelayWorker(
        frappe=FrappeCommandPublicationClient(
            transport=transport,
            api_key="frappe-publication-key",
            api_secret="frappe-publication-secret",
        ),
        gateway=GatewayCommandIngestClient(
            transport=transport,
            bearer_token="gateway-command-ingest-secret",
        ),
        site_id="gbos.localhost",
        worker_id="email-command-relay-01",
        clock=lambda: NOW,
        lease_seconds=30,
    )

    assert worker.run_once().status == PublicationRelayStatus.RETRY
    assert transport.calls[-1]["payload"]["payload"]["safe_code"] == (  # type: ignore[index]
        "gateway_unavailable"
    )
    assert all("acknowledge" not in str(call["url"]) for call in transport.calls)


def test_frappe_client_unwraps_only_the_exact_success_envelope() -> None:
    transport = _Transport((200, _frappe({"publication": None})))
    client = FrappeCommandPublicationClient(
        transport=transport,
        api_key="frappe-publication-key",
        api_secret="frappe-publication-secret",
    )

    assert client.post(
        "claim",
        {
            "site_id": "gbos.localhost",
            "processing_purpose": "email_command_publication",
            "request_id": "request-1",
        },
    ) == (200, {"publication": None})
    assert transport.calls[0]["payload"] == {
        "payload": {
            "site_id": "gbos.localhost",
            "processing_purpose": "email_command_publication",
            "request_id": "request-1",
        }
    }


@pytest.mark.parametrize(
    "status, body",
    [
        (200, {"publication": None}),
        (200, {"message": {"publication": None}, "extra": True}),
        (200, {"message": {"publication": None, "extra": True}}),
        (200, {"message": {"error": {"code": "raw-user@example.invalid"}}}),
        (503, {"message": {"error": {"code": "internal_error"}}}),
        (200, {"message": []}),
    ],
)
def test_frappe_client_rejects_malformed_error_or_extra_envelopes_without_leaks(
    status: int,
    body: dict[str, Any],
) -> None:
    client = FrappeCommandPublicationClient(
        transport=_Transport((status, body)),
        api_key="frappe-publication-key",
        api_secret="frappe-publication-secret",
    )

    with pytest.raises(RuntimeError, match="publication response rejected") as caught:
        client.post(
            "claim",
            {
                "site_id": "gbos.localhost",
                "processing_purpose": "email_command_publication",
                "request_id": "request-1",
            },
        )

    assert "raw-user@example.invalid" not in repr(caught.value)
    assert "frappe-publication-secret" not in repr(caught.value)


def _runtime_files(tmp_path: Path, *, enabled: bool = True) -> tuple[Path, Path]:
    manifest_path = tmp_path / "manifest.json"
    config_path = tmp_path / "runtime.json"
    manifest_path.write_text(
        '{"schema_version":"1.0","mode":"local_pilot","site_id":"gbos.localhost",'
        '"production_go":false,"local_pilot_go":true,"local_pilot_status":"ready",'
        '"deepseek":{},"email_gateway":{'
        '"command_publication_kill_switch":false,"external_send":false}}'
    )
    config_path.write_text(
        "{" + f'"schema_version":"1.0","site_id":"gbos.localhost","enabled":{str(enabled).lower()},'
        '"kill_switch":false,"external_send":false,'
        '"endpoints":{"frappe":"http://frappe-backend:8000",'
        '"gateway":"http://email-gateway-api:8004"},'
        '"auth":{"frappe_api_key_file":"/run/secrets/frappe_email_command_publication_api_key",'
        '"frappe_api_secret_file":"/run/secrets/frappe_email_command_publication_api_secret",'
        '"gateway_bearer_file":"/run/secrets/email_gateway_command_ingest_bearer"},'
        '"worker":{"worker_id":"local-pilot-email-command-publication-worker",'
        '"lease_seconds":30,"idle_delay_seconds":1.0}}'
    )
    return manifest_path, config_path


def _enabled_environment() -> dict[str, str]:
    return {
        "GBOS_LOCAL_RUNTIME_ENABLED": "true",
        "GBOS_EMAIL_COMMAND_PUBLICATION_KILL_SWITCH": "false",
        "GBOS_EXTERNAL_SEND_ENABLED": "false",
    }


def _valid_secrets() -> _Secrets:
    return _Secrets(
        {
            "frappe_email_command_publication_api_key": "frappe-publication-key",
            "frappe_email_command_publication_api_secret": "frappe-publication-secret",
            "email_gateway_command_ingest_bearer": "gateway-command-ingest-secret",
        }
    )


def test_main_preflights_closed_config_and_secrets_then_constructs_and_runs_relay(
    tmp_path: Path,
) -> None:
    manifest_path, config_path = _runtime_files(tmp_path)
    secrets = _valid_secrets()
    transport = _Transport((200, _frappe({"publication": None})))
    factory_calls: list[str] = []

    def transport_factory() -> _Transport:
        assert secrets.calls == [
            "frappe_email_command_publication_api_key",
            "frappe_email_command_publication_api_secret",
            "email_gateway_command_ingest_bearer",
        ]
        factory_calls.append("http")
        return transport

    result = main(
        manifest_path=manifest_path,
        config_path=config_path,
        environ=_enabled_environment(),
        transport_factory=transport_factory,
        secret_provider=secrets,
        clock=lambda: NOW,
    )

    assert result == 0
    assert factory_calls == ["http"]
    assert len(transport.calls) == 1
    assert transport.calls[0]["url"].endswith("email_command_publication.claim")  # type: ignore[union-attr]


def test_disabled_config_returns_78_before_any_secret_or_http_factory(tmp_path: Path) -> None:
    manifest_path, config_path = _runtime_files(tmp_path, enabled=False)
    secrets = _Secrets({})
    calls: list[str] = []

    result = main(
        manifest_path=manifest_path,
        config_path=config_path,
        environ=_enabled_environment(),
        transport_factory=lambda: calls.append("http"),  # type: ignore[arg-type,return-value]
        secret_provider=secrets,
    )

    assert result == 78
    assert secrets.calls == []
    assert calls == []


def test_config_rejects_arbitrary_url_before_secret_or_http_factory(tmp_path: Path) -> None:
    manifest_path, config_path = _runtime_files(tmp_path)
    config_path.write_text(config_path.read_text().replace("frappe-backend:8000", "evil.invalid"))
    secrets = _valid_secrets()
    calls: list[str] = []

    result = main(
        manifest_path=manifest_path,
        config_path=config_path,
        environ=_enabled_environment(),
        transport_factory=lambda: calls.append("http"),  # type: ignore[arg-type,return-value]
        secret_provider=secrets,
    )

    assert result == 78
    assert secrets.calls == []
    assert calls == []


def test_all_three_fixed_secrets_must_preflight_before_http_factory(tmp_path: Path) -> None:
    manifest_path, config_path = _runtime_files(tmp_path)
    secrets = _valid_secrets()
    secrets.values.pop("email_gateway_command_ingest_bearer")
    calls: list[str] = []

    result = main(
        manifest_path=manifest_path,
        config_path=config_path,
        environ=_enabled_environment(),
        transport_factory=lambda: calls.append("http"),  # type: ignore[arg-type,return-value]
        secret_provider=secrets,
    )

    assert result == 78
    assert secrets.calls == [
        "frappe_email_command_publication_api_key",
        "frappe_email_command_publication_api_secret",
        "email_gateway_command_ingest_bearer",
    ]
    assert calls == []


def test_main_defaults_closed_before_http_factory(tmp_path: Path) -> None:
    calls: list[str] = []
    assert (
        main(
            manifest_path=tmp_path / "missing.json",
            environ={},
            transport_factory=lambda: calls.append("http"),  # type: ignore[arg-type,return-value]
        )
        == 78
    )
    assert calls == []


def test_main_rejects_plaintext_secret_environment_before_http_without_raising(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    assert (
        main(
            manifest_path=tmp_path / "missing.json",
            environ={"FRAPPE_API_SECRET": "must-not-leak"},
            transport_factory=lambda: calls.append("http"),  # type: ignore[arg-type,return-value]
        )
        == 78
    )
    assert calls == []


def test_module_entrypoint_invokes_fail_closed_main() -> None:
    source = Path("services/local_pilot_runtime/email_command_publication_worker.py").read_text()

    assert 'if __name__ == "__main__":' in source
    assert "raise SystemExit(main())" in source
