from __future__ import annotations

import base64
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import NoReturn

import pytest
from observer.connectors.email_imap import (
    EmailImapConfig,
    EmailImapConnector,
    ImapCheckpoint,
)

ENABLED_AT = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)
NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
PASSWORD = "secret-password-that-must-not-leak"
USERNAME = "pilot@example.invalid"
FetchData = list[tuple[bytes, bytes] | bytes]


def _raw_message(
    *,
    message_id: str = "<shared@example.invalid>",
    date: str = "Tue, 01 Jan 2000 00:00:00 +0000",
    body: bytes = b"hello",
) -> bytes:
    return (
        b"From: sender@example.invalid\r\n"
        b"To: pilot@example.invalid\r\n"
        + f"Message-ID: {message_id}\r\n".encode()
        + f"Date: {date}\r\n".encode()
        + b"Content-Type: text/plain; charset=utf-8\r\n"
        + b"\r\n"
        + body
    )


def _multipart_message() -> bytes:
    return (
        b"From: sender@example.invalid\r\n"
        b"To: pilot@example.invalid\r\n"
        b"Message-ID: <attachments@example.invalid>\r\n"
        b"Date: Tue, 01 Jan 2000 00:00:00 +0000\r\n"
        b"Content-Type: multipart/mixed; boundary=BOUNDARY\r\n"
        b"\r\n"
        b"--BOUNDARY\r\n"
        b"Content-Type: text/plain\r\n"
        b"\r\n"
        b"body\r\n"
        b"--BOUNDARY\r\n"
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Disposition: attachment; filename=data.bin\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n"
        b"AP+AYmluYXJ5\r\n"
        b"--BOUNDARY\r\n"
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Disposition: attachment; filename=second.bin\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n"
        b"AQI=\r\n"
        b"--BOUNDARY--\r\n"
    )


def _multipart_with_broken_second_attachment() -> bytes:
    return (
        b"Message-ID: <broken-attachment@example.invalid>\r\n"
        b"Content-Type: multipart/mixed; boundary=BOUNDARY\r\n"
        b"\r\n"
        b"--BOUNDARY\r\n"
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Disposition: attachment; filename=valid.bin\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n"
        b"AQI=\r\n"
        b"--BOUNDARY\r\n"
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Disposition: attachment; filename=broken.bin\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n"
        b"!!!!\r\n"
        b"--BOUNDARY--\r\n"
    )


def _multipart_with_repr_sentinels() -> tuple[bytes, tuple[str, ...]]:
    message_id = "message-id-repr-sentinel"
    filename = "filename-repr-sentinel.bin"
    raw_body = "raw-body-repr-sentinel"
    attachment = b"attachment-bytes-repr-sentinel"
    raw = (
        f"Message-ID: <{message_id}>\r\n".encode()
        + b"Content-Type: multipart/mixed; boundary=BOUNDARY\r\n"
        + b"\r\n"
        + b"--BOUNDARY\r\n"
        + b"Content-Type: text/plain\r\n"
        + b"\r\n"
        + raw_body.encode()
        + b"\r\n"
        + b"--BOUNDARY\r\n"
        + b"Content-Type: application/octet-stream\r\n"
        + f"Content-Disposition: attachment; filename={filename}\r\n".encode()
        + b"Content-Transfer-Encoding: base64\r\n"
        + b"\r\n"
        + base64.b64encode(attachment)
        + b"\r\n"
        + b"--BOUNDARY--\r\n"
    )
    return raw, (message_id, filename, raw_body, attachment.decode())


def _fetch_response(
    uid: int,
    raw: bytes,
    *,
    internal_date: str = "07-Aug-2026 10:00:00 +0000",
) -> tuple[str, FetchData]:
    metadata = f'1 (UID {uid} INTERNALDATE "{internal_date}" BODY[] {{{len(raw)}}}'.encode()
    return "OK", [(metadata, raw), b")"]


class FakeImapClient:
    def __init__(
        self,
        *,
        uidvalidity: bytes = b"42",
        search_uids: bytes = b"7",
        messages: dict[int, bytes] | None = None,
        fetch_override: Callable[[int], tuple[object, object]] | None = None,
        login_error: Exception | None = None,
    ) -> None:
        self.uidvalidity = uidvalidity
        self.search_uids = search_uids
        self.messages = messages or {7: _raw_message()}
        self.fetch_override = fetch_override
        self.login_error = login_error
        self.commands: list[tuple[object, ...]] = []

    def login(self, username: str, password: str) -> tuple[str, list[bytes]]:
        self.commands.append(("LOGIN", username, password))
        if self.login_error is not None:
            raise self.login_error
        return "OK", [b"authenticated"]

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.commands.append(("SELECT", mailbox, readonly))
        return "OK", [b"2"]

    def response(self, code: str) -> tuple[str, list[bytes]]:
        self.commands.append(("RESPONSE", code))
        return "UIDVALIDITY", [self.uidvalidity]

    def uid(self, command: str, *args: object) -> tuple[object, object]:
        self.commands.append(("UID", command, *args))
        if command == "SEARCH":
            return "OK", [self.search_uids]
        if command == "FETCH":
            uid = int(str(args[0]))
            if self.fetch_override is not None:
                return self.fetch_override(uid)
            return _fetch_response(uid, self.messages[uid])
        raise AssertionError(f"unexpected UID command: {command}")

    def fetch(self, *_args: object) -> NoReturn:
        raise AssertionError("sequence FETCH is forbidden")

    def logout(self) -> tuple[str, list[bytes]]:
        self.commands.append(("LOGOUT",))
        return "BYE", [b"logout"]


class RecordingTlsFactory:
    def __init__(self, client: FakeImapClient) -> None:
        self.client = client
        self.calls: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int) -> FakeImapClient:
        self.calls.append((host, port))
        return self.client


def _config(**changes: object) -> EmailImapConfig:
    values: dict[str, object] = {
        "host": "imap.example.invalid",
        "port": 993,
        "mailbox": "pilot-primary",
        "folder": "INBOX",
        "enabled_at": ENABLED_AT,
        "poll_limit": 25,
        "max_message_bytes": 1_000_000,
        "max_attachment_bytes": 100_000,
        "max_attachments": 10,
        "rescan_max_window": timedelta(days=7),
        "rescan_max_uids": 100,
    }
    values.update(changes)
    return EmailImapConfig(**values)


def _connector(
    client: FakeImapClient,
    *,
    config: EmailImapConfig | None = None,
) -> tuple[EmailImapConnector, RecordingTlsFactory]:
    factory = RecordingTlsFactory(client)
    connector = EmailImapConnector(
        connector_instance_id="email-primary",
        config=config or _config(),
        tls_client_factory=factory,
        clock=lambda: NOW,
    )
    return connector, factory


@pytest.mark.parametrize(
    "changes",
    (
        {"host": ""},
        {"port": 0},
        {"port": 65_536},
        {"mailbox": ""},
        {"folder": ""},
        {"enabled_at": "2026-08-07T09:30:00Z"},
        {"enabled_at": ENABLED_AT.replace(tzinfo=None)},
        {"poll_limit": 0},
        {"poll_limit": 1001},
        {"max_message_bytes": 0},
        {"max_attachment_bytes": 0},
        {"max_attachments": 0},
        {"rescan_max_window": timedelta(0)},
        {"rescan_max_window": timedelta(days=91)},
        {"rescan_max_uids": 0},
        {"rescan_max_uids": 10_001},
    ),
)
def test_config_rejects_unsafe_or_unbounded_values(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _config(**changes)


def test_checkpoint_round_trips_strictly_and_advances_monotonically() -> None:
    checkpoint = ImapCheckpoint(mailbox="pilot-primary", uidvalidity=42, uid=7)

    serialized = checkpoint.serialize()

    assert serialized == '{"mailbox":"pilot-primary","uid":7,"uidvalidity":42,"version":1}'
    assert ImapCheckpoint.parse(serialized) == checkpoint
    assert checkpoint.advance(9).uid == 9
    assert checkpoint.advance(7) is checkpoint
    with pytest.raises(ValueError, match="backwards"):
        checkpoint.advance(6)
    with pytest.raises(ValueError, match="checkpoint"):
        ImapCheckpoint.parse('{"mailbox":"pilot-primary","uid":7,"uidvalidity":42}')
    with pytest.raises(ValueError, match="checkpoint"):
        ImapCheckpoint.parse(
            '{"extra":0,"mailbox":"pilot-primary","uid":7,"uidvalidity":42,"version":1}'
        )
    with pytest.raises(ValueError, match="checkpoint"):
        ImapCheckpoint.parse(
            '{"mailbox":"pilot-primary","uid":7,"uid":8,"uidvalidity":42,"version":1}'
        )
    with pytest.raises(ValueError, match="checkpoint"):
        ImapCheckpoint.parse('{"mailbox":"pilot-primary","uid":true,"uidvalidity":42,"version":1}')


@pytest.mark.parametrize(
    ("uidvalidity", "uid"),
    (
        (0, 0),
        (2**32, 0),
        (1, -1),
        (1, 2**32),
    ),
)
def test_checkpoint_enforces_rfc_32_bit_uid_boundaries(uidvalidity: int, uid: int) -> None:
    with pytest.raises(ValueError):
        ImapCheckpoint("pilot-primary", uidvalidity, uid)


def test_poll_uses_tls_factory_readonly_select_uid_fetch_and_body_peek_only() -> None:
    client = FakeImapClient()
    connector, factory = _connector(client)

    result = connector.poll(None, username=USERNAME, password=PASSWORD, limit=1)

    assert result.status == "ok"
    assert factory.calls == [("imap.example.invalid", 993)]
    assert ("SELECT", "INBOX", True) in client.commands
    assert ("UID", "FETCH", "7", "(UID INTERNALDATE BODY.PEEK[])") in client.commands
    flattened_commands = " ".join(
        str(part).upper() for command in client.commands for part in command[:2]
    )
    for forbidden in ("STORE", "COPY", "MOVE", "EXPUNGE", "DELETE", "APPEND", "CLOSE"):
        assert forbidden not in flattened_commands


def test_login_values_are_call_only_and_never_leak_through_repr_or_errors() -> None:
    client = FakeImapClient(login_error=RuntimeError(f"provider echoed {PASSWORD}"))
    connector, _ = _connector(client)

    result = connector.poll(None, username=USERNAME, password=PASSWORD)
    rendered = repr((connector, result))

    assert result.status == "retry"
    assert result.error_code == "authentication_failed"
    assert PASSWORD not in rendered
    assert USERNAME not in rendered
    assert not hasattr(connector, "_password")
    assert not hasattr(connector, "_username")


def test_duplicate_search_uids_and_uid_gaps_preserve_uid_identity() -> None:
    shared_message_id = "<duplicate-header@example.invalid>"
    client = FakeImapClient(
        search_uids=b"7 7 10",
        messages={
            7: _raw_message(message_id=shared_message_id, body=b"first"),
            10: _raw_message(message_id=shared_message_id, body=b"second"),
        },
    )
    connector, _ = _connector(client)

    result = connector.poll(None, username=USERNAME, password=PASSWORD)

    assert result.status == "ok"
    assert [message.uid for message in result.messages] == [7, 10]
    assert len({message.provider_event_id for message in result.messages}) == 2
    assert [message.message_id for message in result.messages] == [
        shared_message_id,
        shared_message_id,
    ]
    fetches = [command for command in client.commands if command[:2] == ("UID", "FETCH")]
    assert len(fetches) == 2


def test_provider_identity_includes_connector_instance_mailbox_uidvalidity_and_uid() -> None:
    client = FakeImapClient()
    connector, _ = _connector(client)
    other_connector = EmailImapConnector(
        connector_instance_id="email-secondary",
        config=_config(),
        tls_client_factory=RecordingTlsFactory(FakeImapClient()),
        clock=lambda: NOW,
    )

    first = connector.poll(None, username=USERNAME, password=PASSWORD)
    repeat = connector.poll(None, username=USERNAME, password=PASSWORD)
    other = other_connector.poll(None, username=USERNAME, password=PASSWORD)

    assert first.messages[0].provider_event_id == repeat.messages[0].provider_event_id
    assert first.messages[0].provider_event_id != other.messages[0].provider_event_id


def test_old_date_header_does_not_drop_a_new_uid_arriving_after_enablement() -> None:
    raw = _raw_message(date="Tue, 01 Jan 2000 00:00:00 +0000")
    client = FakeImapClient(messages={7: raw})
    connector, _ = _connector(client)

    result = connector.poll(None, username=USERNAME, password=PASSWORD)

    assert result.status == "ok"
    assert len(result.messages) == 1
    assert result.messages[0].raw_delivery.exact_bytes is raw
    assert result.messages[0].raw_delivery.received_at == datetime(2026, 8, 7, 10, 0, tzinfo=UTC)


def test_initial_poll_filters_by_internaldate_not_date_header_and_advances_past_old_uid() -> None:
    client = FakeImapClient(
        search_uids=b"6 7",
        messages={
            6: _raw_message(body=b"before enabled"),
            7: _raw_message(body=b"after enabled"),
        },
        fetch_override=lambda uid: _fetch_response(
            uid,
            {
                6: _raw_message(body=b"before enabled"),
                7: _raw_message(body=b"after enabled"),
            }[uid],
            internal_date=(
                "07-Aug-2026 09:00:00 +0000" if uid == 6 else "07-Aug-2026 10:00:00 +0000"
            ),
        ),
    )
    connector, _ = _connector(client)

    result = connector.poll(None, username=USERNAME, password=PASSWORD)

    assert [message.uid for message in result.messages] == [7]
    assert ImapCheckpoint.parse(result.checkpoint_candidate or "").uid == 7
    assert ("UID", "SEARCH", None, "SINCE", "07-Aug-2026") in client.commands


def test_exact_message_bytes_and_binary_attachment_candidates_are_preserved() -> None:
    raw = _multipart_message()
    client = FakeImapClient(messages={7: raw})
    connector, _ = _connector(client)

    result = connector.poll(None, username=USERNAME, password=PASSWORD)

    assert result.status == "ok"
    message = result.messages[0]
    assert message.raw_delivery.media_type == "message/rfc822"
    assert message.raw_delivery.exact_bytes is raw
    assert [candidate.filename for candidate in message.evidence_candidates] == [
        "data.bin",
        "second.bin",
    ]
    assert message.attachment_status == "ready"
    assert message.attachment_error_code is None
    assert message.checkpoint_candidate == result.checkpoint_candidate
    assert message.evidence_candidates[0].content == b"\x00\xff\x80binary"
    assert isinstance(message.evidence_candidates[0].content, bytes)


def test_success_result_repr_redacts_exact_message_and_attachment_bytes() -> None:
    raw, sentinels = _multipart_with_repr_sentinels()
    client = FakeImapClient(messages={7: raw})
    connector, _ = _connector(client)

    result = connector.poll(None, username=USERNAME, password=PASSWORD)

    assert result.messages[0].raw_delivery.exact_bytes == raw
    rendered = repr(
        (
            result.messages[0].evidence_candidates[0],
            result.messages[0],
            result,
        )
    )
    assert all(sentinel not in rendered for sentinel in sentinels)


def test_attachment_limit_quarantines_candidates_but_preserves_raw_delivery() -> None:
    previous = ImapCheckpoint("pilot-primary", 42, 6).serialize()
    raw = _multipart_message()
    client = FakeImapClient(messages={7: raw})
    connector, _ = _connector(client, config=_config(max_attachments=1))

    result = connector.poll(previous, username=USERNAME, password=PASSWORD)

    assert result.status == "ok"
    assert result.error_code is None
    assert len(result.messages) == 1
    message = result.messages[0]
    assert message.raw_delivery.exact_bytes is raw
    assert message.attachment_status == "quarantined"
    assert message.attachment_error_code == "attachment_limit_exceeded"
    assert message.evidence_candidates == ()
    assert message.checkpoint_candidate == result.checkpoint_candidate
    assert message.checkpoint_candidate != previous


def test_oversized_attachment_quarantines_candidates_but_preserves_raw_delivery() -> None:
    previous = ImapCheckpoint("pilot-primary", 42, 6).serialize()
    raw = _multipart_message()
    client = FakeImapClient(messages={7: raw})
    connector, _ = _connector(client, config=_config(max_attachment_bytes=1))

    result = connector.poll(previous, username=USERNAME, password=PASSWORD)

    assert result.status == "ok"
    message = result.messages[0]
    assert message.raw_delivery.exact_bytes is raw
    assert message.attachment_status == "quarantined"
    assert message.attachment_error_code == "attachment_too_large"
    assert message.evidence_candidates == ()
    assert message.checkpoint_candidate == result.checkpoint_candidate


def test_broken_second_attachment_quarantines_all_candidates_but_preserves_raw() -> None:
    previous = ImapCheckpoint("pilot-primary", 42, 6).serialize()
    raw = _multipart_with_broken_second_attachment()
    client = FakeImapClient(messages={7: raw})
    connector, _ = _connector(client)

    result = connector.poll(previous, username=USERNAME, password=PASSWORD)

    assert result.status == "ok"
    assert len(result.messages) == 1
    message = result.messages[0]
    assert message.raw_delivery.exact_bytes is raw
    assert message.attachment_status == "quarantined"
    assert message.attachment_error_code == "attachment_decode_failed"
    assert message.evidence_candidates == ()
    assert ImapCheckpoint.parse(message.checkpoint_candidate).uid == 7
    assert message.checkpoint_candidate == result.checkpoint_candidate
    assert "durably accepted" in (type(message).__doc__ or "")
    assert "MUST NOT be committed" in (type(result).__doc__ or "")
    assert not hasattr(message, "checkpoint")
    assert not hasattr(result, "next_checkpoint")


def test_uidvalidity_change_pauses_with_bounded_rescan_plan_and_keeps_checkpoint() -> None:
    previous = ImapCheckpoint("pilot-primary", 41, 123).serialize()
    client = FakeImapClient(uidvalidity=b"42")
    connector, _ = _connector(
        client,
        config=_config(
            enabled_at=NOW - timedelta(days=30),
            rescan_max_window=timedelta(days=7),
            rescan_max_uids=80,
        ),
    )

    result = connector.poll(previous, username=USERNAME, password=PASSWORD)

    assert result.status == "paused"
    assert result.error_code == "uidvalidity_changed"
    assert result.checkpoint_candidate == previous
    assert result.messages == ()
    assert result.rescan_plan is not None
    assert result.rescan_plan.previous_uidvalidity == 41
    assert result.rescan_plan.observed_uidvalidity == 42
    assert result.rescan_plan.lower_bound == NOW - timedelta(days=7)
    assert result.rescan_plan.lower_bound >= connector.config.enabled_at
    assert result.rescan_plan.max_uids == 80
    assert not any(command[:2] == ("UID", "SEARCH") for command in client.commands)


@pytest.mark.parametrize(
    ("fetch_result", "error_code"),
    (
        (
            ("OK", [(b'1 (UID 7 INTERNALDATE "07-Aug-2026 10:00:00 +0000")', b"partial")]),
            "invalid_fetch_response",
        ),
        (("NO", [b"temporary failure"]), "fetch_failed"),
    ),
)
def test_invalid_or_partial_fetch_does_not_advance_checkpoint(
    fetch_result: tuple[object, object],
    error_code: str,
) -> None:
    previous = ImapCheckpoint("pilot-primary", 42, 6).serialize()
    client = FakeImapClient(fetch_override=lambda _uid: fetch_result)
    connector, _ = _connector(client)

    result = connector.poll(previous, username=USERNAME, password=PASSWORD)

    assert result.status == "retry"
    assert result.error_code == error_code
    assert result.messages == ()
    assert result.checkpoint_candidate == previous


def test_disconnect_during_second_fetch_discards_partial_batch_and_keeps_checkpoint() -> None:
    previous = ImapCheckpoint("pilot-primary", 42, 6).serialize()

    def fetch(uid: int) -> tuple[str, FetchData]:
        if uid == 8:
            raise OSError("connection reset with no safe resume point")
        return _fetch_response(uid, _raw_message(body=b"first"))

    client = FakeImapClient(search_uids=b"7 8", fetch_override=fetch)
    connector, _ = _connector(client)

    result = connector.poll(previous, username=USERNAME, password=PASSWORD)

    assert result.status == "retry"
    assert result.error_code == "fetch_failed"
    assert result.messages == ()
    assert result.checkpoint_candidate == previous
