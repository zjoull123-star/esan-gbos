from __future__ import annotations

import imaplib
import json
import ssl
from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn

import pytest

from services.local_pilot_runtime import pollers as pollers_module
from services.local_pilot_runtime.pollers import (
    EmailCredentials,
    compose_email_poller,
    compose_postgres_polling_state,
    compose_wecom_poller,
    main,
    run_poll_daemon,
)
from services.observer.observer.connectors.email_imap import EmailImapConfig
from services.observer.observer.connectors.wecom_archive import (
    EncryptedEnvelope,
    SdkFetchPage,
    WeComArchiveConfig,
)
from services.observer.observer.models import ConnectorKey, RawDelivery, TenantScope

ACTIVATION = datetime(2026, 8, 8, 9, tzinfo=UTC)
NOW = datetime(2026, 8, 8, 12, tzinfo=UTC)
SCOPE = TenantScope("alpha.example", "observation_processing")


def _raw_message(uid: int) -> bytes:
    return (
        b"From: private@example.invalid\r\n"
        + f"Message-ID: <message-{uid}@example.invalid>\r\n".encode()
        + b"Content-Type: text/plain\r\n\r\nprivate body"
    )


def _fetch_response(
    uid: int,
    raw: bytes,
    *,
    internal_date: str,
) -> tuple[str, list[tuple[bytes, bytes] | bytes]]:
    metadata = f'1 (UID {uid} INTERNALDATE "{internal_date}" BODY[] {{{len(raw)}}}'.encode()
    return "OK", [(metadata, raw), b")"]


class FakeImapClient:
    def __init__(
        self,
        *,
        search_uids: tuple[int, ...] = (1, 2),
        internal_dates: dict[int, str] | None = None,
    ) -> None:
        self.commands: list[tuple[object, ...]] = []
        self.search_uids = search_uids
        self.internal_dates = internal_dates or {
            1: "08-Aug-2026 08:00:00 +0000",
            2: "08-Aug-2026 10:00:00 +0000",
        }

    def login(self, username: str, password: str) -> tuple[str, list[bytes]]:
        self.commands.append(("LOGIN", username, password))
        return "OK", [b"authenticated"]

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.commands.append(("SELECT", mailbox, readonly))
        return "OK", [b"2"]

    def response(self, code: str) -> tuple[str, list[bytes]]:
        self.commands.append(("RESPONSE", code))
        return "UIDVALIDITY", [b"42"]

    def uid(self, command: str, *args: object) -> tuple[object, object]:
        self.commands.append(("UID", command, *args))
        if command == "SEARCH":
            return "OK", [" ".join(map(str, self.search_uids)).encode()]
        if command == "FETCH":
            uid = int(str(args[0]))
            return _fetch_response(
                uid,
                _raw_message(uid),
                internal_date=self.internal_dates[uid],
            )
        raise AssertionError(f"unexpected command: {command}")

    def fetch(self, *_args: object) -> NoReturn:
        raise AssertionError("sequence fetch is forbidden")

    def logout(self) -> tuple[str, list[bytes]]:
        self.commands.append(("LOGOUT",))
        return "BYE", [b"done"]


class RecordingTlsFactory:
    def __init__(self, client: FakeImapClient) -> None:
        self.client = client
        self.calls: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int) -> FakeImapClient:
        self.calls.append((host, port))
        return self.client


class FakePollingState:
    def __init__(
        self,
        *,
        cursor: str | None,
        fail_on_accept: int | None = None,
        expected_now: datetime = NOW,
    ) -> None:
        self.cursor = cursor
        self.version = 3
        self.fail_on_accept = fail_on_accept
        self.expected_now = expected_now
        self.accepted: list[RawDelivery] = []
        self.advanced: list[tuple[int, str | None]] = []
        self.health: list[tuple[str, str | None]] = []
        self.leases: list[str] = []

    def acquire(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        owner: str,
        now: datetime,
        lease_seconds: int,
    ) -> None:
        assert scope == SCOPE and now == self.expected_now and lease_seconds == 60
        self.leases.append(f"acquire:{key.connector}:{owner}")

    def release(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        owner: str,
        now: datetime,
    ) -> None:
        assert scope == SCOPE and now == self.expected_now
        self.leases.append(f"release:{key.connector}:{owner}")

    def load_checkpoint(
        self,
        scope: TenantScope,
        key: ConnectorKey,
    ) -> tuple[str | None, int, str]:
        assert scope == SCOPE
        return self.cursor, self.version, "healthy"

    def accept_delivery(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        delivery: RawDelivery,
    ) -> None:
        assert scope == SCOPE
        call_number = len(self.accepted) + 1
        if self.fail_on_accept == call_number:
            raise RuntimeError("durable store unavailable")
        self.accepted.append(delivery)

    def advance_checkpoint(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        expected_version: int,
        cursor: str | None,
        now: datetime,
    ) -> None:
        assert scope == SCOPE and now == self.expected_now
        self.advanced.append((expected_version, cursor))

    def update_health(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        status: str,
        error_code: str | None,
        now: datetime,
    ) -> None:
        assert scope == SCOPE and now == self.expected_now
        self.health.append((status, error_code))


def _email_config() -> EmailImapConfig:
    return EmailImapConfig(
        host="imap.example.invalid",
        port=993,
        mailbox="pilot-primary",
        folder="INBOX",
        enabled_at=ACTIVATION,
        poll_limit=10,
        max_message_bytes=1_000_000,
        max_attachment_bytes=100_000,
        max_attachments=10,
        rescan_max_window=timedelta(days=7),
        rescan_max_uids=100,
    )


def test_default_imap_factory_requires_modern_verified_tls_and_bounded_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    client = object()

    def fake_imap_client(
        host: str,
        port: int,
        *,
        ssl_context: ssl.SSLContext,
        timeout: float,
    ) -> object:
        captured.update(
            host=host,
            port=port,
            ssl_context=ssl_context,
            timeout=timeout,
        )
        return client

    monkeypatch.setattr(imaplib, "IMAP4_SSL", fake_imap_client)

    result = pollers_module._stdlib_imap_factory("imap.example.invalid", 993)

    assert result is client
    assert captured["host"] == "imap.example.invalid"
    assert captured["port"] == 993
    context = captured["ssl_context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
    timeout = captured["timeout"]
    assert isinstance(timeout, int | float)
    assert 0 < timeout <= 30


def test_email_poller_uses_injected_tls_factory_and_never_backfills_before_activation() -> None:
    client = FakeImapClient()
    factory = RecordingTlsFactory(client)
    state = FakePollingState(cursor=None)
    credentials = EmailCredentials(
        username="private-user@example.invalid",
        password="private-password",
    )
    runner = compose_email_poller(
        state=state,
        scope=SCOPE,
        key=ConnectorKey("email", "pilot-primary"),
        config=_email_config(),
        tls_client_factory=factory,
        credentials=credentials,
        clock=lambda: NOW,
        worker_id="email-poller",
        limit=10,
    )

    result = runner.run_once()

    assert result.status == "ok"
    assert result.accepted_count == 1
    assert state.accepted[0].received_at >= ACTIVATION
    assert state.advanced == [
        (
            3,
            '{"mailbox":"pilot-primary","uid":2,"uidvalidity":42,"version":1}',
        )
    ]
    assert factory.calls == [("imap.example.invalid", 993)]
    assert ("LOGIN", credentials.username, credentials.password) in client.commands
    assert ("SELECT", "INBOX", True) in client.commands
    assert ("UID", "FETCH", "1", "(UID INTERNALDATE BODY.PEEK[])") in client.commands
    rendered = repr((runner, credentials))
    assert credentials.username not in rendered
    assert credentials.password not in rendered


def test_email_poller_advances_past_an_all_pre_activation_batch_without_accepting_it() -> None:
    client = FakeImapClient(
        internal_dates={
            1: "08-Aug-2026 07:00:00 +0000",
            2: "08-Aug-2026 08:00:00 +0000",
        }
    )
    state = FakePollingState(cursor=None)
    runner = compose_email_poller(
        state=state,
        scope=SCOPE,
        key=ConnectorKey("email", "pilot-primary"),
        config=_email_config(),
        tls_client_factory=RecordingTlsFactory(client),
        credentials=EmailCredentials(
            username="private-user@example.invalid",
            password="private-password",
        ),
        clock=lambda: NOW,
        worker_id="email-poller",
        limit=10,
    )

    result = runner.run_once()

    assert result.status == "ok"
    assert result.accepted_count == 0
    assert result.checkpoint_advanced is True
    assert state.accepted == []
    assert state.advanced == [
        (
            3,
            '{"mailbox":"pilot-primary","uid":2,"uidvalidity":42,"version":1}',
        )
    ]


class FakeOfficialWeComSdk:
    def __init__(self, envelopes: tuple[EncryptedEnvelope, ...]) -> None:
        self.envelopes = envelopes
        self.fetch_calls: list[tuple[int, int]] = []

    def fetch_chat_data(self, *, seq: int, limit: int) -> SdkFetchPage:
        self.fetch_calls.append((seq, limit))
        return SdkFetchPage.ok(self.envelopes)

    def decrypt_random_key(self, *, encrypt_random_key: bytes) -> bytes:
        raise AssertionError("poll composition must not decrypt")

    def decrypt_chat_data(
        self,
        *,
        decrypted_random_key: bytes,
        encrypt_chat_msg: bytes,
    ) -> bytes:
        raise AssertionError("poll composition must not decrypt")

    def download_media(self, *, sdk_file_id: str, cursor: bytes) -> NoReturn:
        raise AssertionError("poll composition must not download media")


def _envelope(seq: int) -> EncryptedEnvelope:
    return EncryptedEnvelope(
        seq=seq,
        exact_bytes=f"encrypted-envelope-{seq}".encode(),
        encrypt_random_key=f"random-key-{seq}".encode(),
        encrypt_chat_msg=f"cipher-{seq}".encode(),
    )


def test_wecom_poller_uses_only_injected_sdk_and_preserves_checkpoint_on_partial_failure() -> None:
    sdk = FakeOfficialWeComSdk((_envelope(11), _envelope(12)))
    state = FakePollingState(cursor="10", fail_on_accept=2)
    runner = compose_wecom_poller(
        state=state,
        scope=SCOPE,
        key=ConnectorKey("wecom", "archive-primary"),
        config=WeComArchiveConfig(instance_id="archive-primary"),
        sdk=sdk,
        activation_time=ACTIVATION,
        clock=lambda: NOW,
        worker_id="wecom-poller",
        limit=10,
    )

    result = runner.run_once()

    assert result.status == "retry"
    assert result.safe_error_code == "durable_accept_failed"
    assert [delivery.delivery_id for delivery in state.accepted] == [
        "wecom-archive-archive-primary-seq-11"
    ]
    assert state.advanced == []
    assert sdk.fetch_calls == [(10, 10)]
    assert "sdk=<redacted>" in repr(runner)


def test_wecom_poller_rejects_an_unverified_sdk_object_at_composition() -> None:
    with pytest.raises(TypeError, match="official SDK"):
        compose_wecom_poller(
            state=FakePollingState(cursor="10"),
            scope=SCOPE,
            key=ConnectorKey("wecom", "archive-primary"),
            config=WeComArchiveConfig(instance_id="archive-primary"),
            sdk=object(),  # type: ignore[arg-type]
            activation_time=ACTIVATION,
            clock=lambda: NOW,
            worker_id="wecom-poller",
            limit=10,
        )


@pytest.mark.parametrize(
    ("cursor", "clock", "error_code"),
    [
        (None, lambda: NOW, "activation_checkpoint_required"),
        ("10", lambda: ACTIVATION - timedelta(seconds=1), "activation_not_reached"),
    ],
)
def test_wecom_poller_fails_closed_without_an_active_non_backfill_checkpoint(
    cursor: str | None,
    clock: Callable[[], datetime],
    error_code: str,
) -> None:
    sdk = FakeOfficialWeComSdk((_envelope(11),))
    state = FakePollingState(cursor=cursor, expected_now=clock())
    runner = compose_wecom_poller(
        state=state,
        scope=SCOPE,
        key=ConnectorKey("wecom", "archive-primary"),
        config=WeComArchiveConfig(instance_id="archive-primary"),
        sdk=sdk,
        activation_time=ACTIVATION,
        clock=clock,
        worker_id="wecom-poller",
        limit=10,
    )

    result = runner.run_once()

    assert result.status == "paused"
    assert result.safe_error_code == error_code
    assert sdk.fetch_calls == []
    assert state.accepted == []
    assert state.advanced == []


class FakeInbox:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def accept(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        delivery: RawDelivery,
        *,
        correlation_id: str,
        max_attempts: int = 3,
    ) -> None:
        self.calls.append((scope, key, delivery, correlation_id, max_attempts))


def test_postgres_state_composition_routes_delivery_to_durable_inbox_without_credentials() -> None:
    inbox = FakeInbox()
    state = compose_postgres_polling_state(
        connection=object(),
        storage=object(),
        inbox=inbox,
    )
    delivery = RawDelivery(
        delivery_id="email-delivery-001",
        exact_bytes=b"private body",
        media_type="message/rfc822",
        received_at=NOW,
    )

    state.accept_delivery(
        SCOPE,
        ConnectorKey("email", "pilot-primary"),
        delivery,
    )

    assert inbox.calls[0][:3] == (
        SCOPE,
        ConnectorKey("email", "pilot-primary"),
        delivery,
    )
    assert isinstance(inbox.calls[0][3], str)
    assert "connection=<redacted>" in repr(state)
    assert main() == 78


class _StopAfterWait:
    def __init__(self) -> None:
        self.stopped = False

    def is_set(self) -> bool:
        return self.stopped

    def wait(self, seconds: float) -> bool:
        assert seconds == 60
        self.stopped = True
        return True


class _RetryRunner:
    def __init__(self) -> None:
        self.calls = 0

    def run_once(self) -> object:
        self.calls += 1
        return object()


def test_poller_daemon_honors_stop_after_retry_iteration() -> None:
    runner = _RetryRunner()
    stop = _StopAfterWait()

    run_poll_daemon(runner, stop_event=stop)  # type: ignore[arg-type]

    assert runner.calls == 1


def _private_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


def _poller_files(tmp_path: Path, channel: str) -> tuple[Path, Path, Path]:
    password_file = tmp_path / "postgres-password"
    password_file.write_text("not-a-real-password", encoding="utf-8")
    password_file.chmod(0o600)
    manifest = {
        "schema_version": "1.0",
        "mode": "local_pilot",
        "site_id": "alpha.example",
        "production_go": False,
        "local_pilot_go": True,
        "local_pilot_status": "ready",
        "deepseek": {"enabled": False},
        "channels": {
            name: {
                "enabled": name == channel,
                "activation_time": ("2026-08-08T09:00:00Z" if name == channel else None),
                "backfill_history": False,
                **({"credential_ref": None} if name != "media" else {"local_only": True}),
            }
            for name in ("email", "wecom", "whatsapp", "media")
        },
    }
    runtime = {
        "schema_version": "1.0",
        "site_id": "alpha.example",
        "postgres": {
            "host": "postgres",
            "port": 5432,
            "database": "gbos",
            "user": "gbos_observer_app",
            "password_file": str(password_file),
            "connect_timeout_seconds": 2,
        },
        "auth": {
            "agent_api_bearer_file": str(password_file),
            "context_api_bearer_file": str(password_file),
            "context_client_bearer_file": str(password_file),
            "context_auth_ref": "local",
        },
        "context_endpoint": {"base_url": "http://context-api:8001", "unix_socket": None},
        "listen": {"host": "127.0.0.1", "agent_api_port": 8002, "context_api_port": 8001},
        "components": {
            name: {
                "enabled": True,
                "kill_switch": False,
                "provider_mode": "disabled",
                "synthetic_e2e": False,
            }
            for name in ("agent_api", "context_api", "agent_worker", "model_worker")
        },
        "worker": {
            "worker_id": "poller-worker",
            "idle_delay_seconds": 1,
            "heartbeat_interval_seconds": 5,
        },
    }
    credential_path = tmp_path / f"{channel}.json"
    if channel == "email":
        credential = {
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
            "poll_limit": 10,
            "max_message_bytes": 1_000_000,
            "max_attachment_bytes": 100_000,
            "max_attachments": 5,
            "rescan_max_window_seconds": 86_400,
            "rescan_max_uids": 100,
            "initial_checkpoint": None,
        }
    else:
        credential = {
            "instance_id": "wecom-primary",
            "team_ref": None,
            "agent_task_type": None,
            "account_user_ref": "USER-WECOM-OWNER",
            "corp_id": "not-a-real-corp",
            "secret": "not-a-real-secret",
            "private_key": "not-a-real-private-key",
            "initial_checkpoint": "100",
        }
    _private_json(credential_path, credential)
    connectors = {
        "schema_version": "1.0",
        "site_id": "alpha.example",
        "external_send": False,
        "evidence_cas_root": str(tmp_path / "cas"),
        "channels": {
            name: {
                "enabled": name == channel,
                "kill_switch": name != channel,
                "activation_time": ("2026-08-08T09:00:00Z" if name == channel else None),
                "backfill_history": False,
                "credential_file": str(credential_path if name == channel else tmp_path / name),
            }
            for name in ("email", "wecom", "whatsapp", "media")
        },
    }
    return (
        _private_json(tmp_path / "manifest.json", manifest),
        _private_json(tmp_path / "runtime.json", runtime),
        _private_json(tmp_path / "connectors.json", connectors),
    )


def test_wecom_cli_without_official_sdk_factory_returns_78_before_database(
    tmp_path: Path,
) -> None:
    manifest, runtime, connectors = _poller_files(tmp_path, "wecom")
    database_calls: list[object] = []

    result = main(
        ["wecom"],
        manifest_path=manifest,
        runtime_config_path=runtime,
        connectors_path=connectors,
        environ={
            "GBOS_LOCAL_RUNTIME_ENABLED": "true",
            "GBOS_CONNECTOR_KILL_SWITCH": "false",
        },
        connector=lambda **kwargs: database_calls.append(kwargs),
        clock=lambda: NOW,
    )

    assert result == 78
    assert database_calls == []


class _EntryConnection:
    class Cursor:
        def __enter__(self) -> _EntryConnection.Cursor:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(self, *args: object) -> None:
            return None

        def fetchone(self) -> tuple[None, int, str]:
            return None, 0, "healthy"

    def transaction(self) -> nullcontext[None]:
        return nullcontext()

    def cursor(self) -> _EntryConnection.Cursor:
        return self.Cursor()

    def close(self) -> None:
        return None


class _EntryStorage:
    def __init__(self) -> None:
        self.account_user_refs: list[object] = []

    def register_connector_instance(self, *args: object, **kwargs: object) -> None:
        self.account_user_refs.append(kwargs.get("account_user_ref"))

    def compare_and_swap_checkpoint(self, *args: object, **kwargs: object) -> None:
        return None


@pytest.mark.parametrize(
    ("channel", "expected_owner"),
    [("email", "owner@example.invalid"), ("wecom", "USER-WECOM-OWNER")],
)
def test_poller_registration_passes_account_owner_without_inference(
    tmp_path: Path,
    channel: str,
    expected_owner: str,
) -> None:
    manifest, runtime, connectors = _poller_files(tmp_path, channel)
    storage = _EntryStorage()
    captured: list[object] = []

    result = main(
        [channel],
        manifest_path=manifest,
        runtime_config_path=runtime,
        connectors_path=connectors,
        environ={
            "GBOS_LOCAL_RUNTIME_ENABLED": "true",
            "GBOS_CONNECTOR_KILL_SWITCH": "false",
        },
        connector=lambda **kwargs: _EntryConnection(),
        storage_factory=lambda connection: storage,  # type: ignore[arg-type]
        tls_client_factory=(
            RecordingTlsFactory(FakeImapClient(search_uids=())) if channel == "email" else None
        ),
        wecom_sdk_factory=(
            (lambda credential: FakeOfficialWeComSdk(())) if channel == "wecom" else None
        ),
        daemon_runner=lambda runner, **kwargs: captured.append(runner),
        clock=lambda: NOW,
    )

    assert result == 0
    assert storage.account_user_refs == [expected_owner]
    assert expected_owner != "private@example.invalid"
    assert len(captured) == 1
