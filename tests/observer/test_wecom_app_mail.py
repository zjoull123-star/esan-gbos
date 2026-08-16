from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest
from observer.connectors.email_provider import EmailProviderError
from observer.connectors.wecom_app_mail import (
    WeComAppMailConfig,
    WeComAppMailHTTPResponse,
    WeComAppMailPause,
    WeComAppMailProvider,
)

NOW = datetime(2026, 8, 14, 6, tzinfo=UTC)


class _NoCallsTransport:
    def request(self, **kwargs: object) -> WeComAppMailHTTPResponse:
        raise AssertionError(f"unexpected transport call: {sorted(kwargs)}")


class _ScriptedTransport:
    def __init__(self, *steps: WeComAppMailHTTPResponse | BaseException) -> None:
        self.steps = list(steps)
        self.calls: list[dict[str, object]] = []

    def request(
        self,
        *,
        method: str,
        url: str,
        query: Mapping[str, str],
        json_body: Mapping[str, object] | None,
        timeout_seconds: float,
    ) -> WeComAppMailHTTPResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "query": dict(query),
                "json_body": None if json_body is None else dict(json_body),
                "timeout_seconds": timeout_seconds,
            }
        )
        if not self.steps:
            raise AssertionError("script exhausted")
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


class _MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _response(payload: object, *, status: int = 200) -> WeComAppMailHTTPResponse:
    return WeComAppMailHTTPResponse(
        status_code=status,
        body=json.dumps(payload, separators=(",", ":")).encode(),
    )


def _token(value: str = "synthetic_token_0001", *, expires_in: int = 7200) -> object:
    return {
        "errcode": 0,
        "errmsg": "ok",
        "access_token": value,
        "expires_in": expires_in,
    }


def _page(
    *mail_ids: str,
    next_cursor: str = "",
    has_more: int = 0,
) -> object:
    return {
        "errcode": 0,
        "errmsg": "ok",
        "next_cursor": next_cursor,
        "has_more": has_more,
        "mail_list": [{"mail_id": value} for value in mail_ids],
    }


def _message(
    mail_id: str,
    *,
    date: str = "Fri, 14 Aug 2026 06:00:00 +0000",
    body: str = "Synthetic body.",
) -> dict[str, object]:
    return {
        "errcode": 0,
        "errmsg": "ok",
        "mail_data": (
            f"Date: {date}\r\n"
            "From: sender@example.invalid\r\n"
            "To: synthetic-mailbox@example.invalid\r\n"
            f"Subject: {mail_id}\r\n"
            f"Message-ID: <{mail_id}@example.invalid>\r\n"
            "\r\n"
            f"{body}"
        ),
    }


def _config(**changes: Any) -> WeComAppMailConfig:
    values: dict[str, Any] = {
        "corp_id": "synthetic-corp-id",
        "app_id": "1000001",
        "app_secret": "synthetic-app-secret",
        "mailbox": "synthetic-mailbox@example.invalid",
        "activation_watermark": NOW,
    }
    values.update(changes)
    return WeComAppMailConfig(**values)


def _provider(
    transport: _ScriptedTransport,
    *,
    config: WeComAppMailConfig | None = None,
    clock: _MutableClock | None = None,
) -> WeComAppMailProvider:
    return WeComAppMailProvider(
        config=config or _config(),
        transport=transport,
        clock=clock or _MutableClock(NOW.replace(hour=7)),
    )


def _urls(transport: _ScriptedTransport) -> list[str]:
    return [str(call["url"]) for call in transport.calls]


def test_provider_is_explicitly_injected_and_zero_default_network() -> None:
    config = WeComAppMailConfig(
        corp_id="synthetic-corp-id",
        app_id="1000001",
        app_secret="synthetic-app-secret",
        mailbox="synthetic-mailbox@example.invalid",
        activation_watermark=NOW,
    )

    provider = WeComAppMailProvider(
        config=config,
        transport=_NoCallsTransport(),
        clock=lambda: NOW,
    )

    assert provider.provider_kind == "wecom_app_mail"
    assert "network=injected" in repr(provider)


def test_poll_uses_fixed_official_requests_and_returns_exact_raw_delivery() -> None:
    transport = _ScriptedTransport(
        _response(_token()),
        _response(_page("mail-001")),
        _response(_message("mail-001")),
    )
    provider = _provider(transport)

    result = provider.poll(None, 10)

    assert result.expected_cursor is None
    assert [value.delivery_id for value in result.deliveries] == ["mail-001"]
    assert result.deliveries[0].media_type == "message/rfc822"
    assert result.deliveries[0].exact_bytes == str(_message("mail-001")["mail_data"]).encode()
    assert result.deliveries[0].received_at == NOW
    assert _urls(transport) == [
        "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
        "https://qyapi.weixin.qq.com/cgi-bin/exmail/app/get_mail_list",
        "https://qyapi.weixin.qq.com/cgi-bin/exmail/app/read_mail",
    ]
    assert transport.calls[0]["method"] == "GET"
    assert transport.calls[0]["query"] == {
        "corpid": "synthetic-corp-id",
        "corpsecret": "synthetic-app-secret",
    }
    assert transport.calls[0]["json_body"] is None
    assert transport.calls[1]["query"] == {"access_token": "synthetic_token_0001"}
    assert transport.calls[1]["json_body"] == {
        "begin_time": int(NOW.timestamp()),
        "end_time": int(NOW.replace(hour=7).timestamp()),
        "limit": 10,
    }
    assert transport.calls[2]["json_body"] == {"mail_id": "mail-001"}
    assert json.loads(result.candidate_cursor or "") == {
        "begin_time": int(NOW.timestamp()),
        "cursor": "",
        "end_time": int(NOW.replace(hour=7).timestamp()),
        "v": 1,
    }
    assert result.candidate_cursor == json.dumps(
        json.loads(result.candidate_cursor), separators=(",", ":"), sort_keys=True
    )


def test_token_is_application_cached_and_refreshed_at_early_expiry() -> None:
    clock = _MutableClock(NOW.replace(hour=7))
    transport = _ScriptedTransport(
        _response(_token("token-one", expires_in=120)),
        _response(_page()),
        _response(_page()),
        _response(_token("token-two", expires_in=120)),
        _response(_page()),
    )
    provider = _provider(transport, clock=clock)

    first = provider.poll(None, 10)
    clock.now = clock.now.replace(second=59)
    second = provider.poll(first.candidate_cursor, 10)
    clock.now = clock.now.replace(minute=1, second=1)
    provider.poll(second.candidate_cursor, 10)

    assert _urls(transport).count("https://qyapi.weixin.qq.com/cgi-bin/gettoken") == 2
    list_tokens = [
        call["query"] for call in transport.calls if str(call["url"]).endswith("/get_mail_list")
    ]
    assert list_tokens == [
        {"access_token": "token-one"},
        {"access_token": "token-one"},
        {"access_token": "token-two"},
    ]


def test_pagination_deduplicates_and_sorts_out_of_order_ids_before_one_read_each() -> None:
    transport = _ScriptedTransport(
        _response(_token()),
        _response(_page("mail-b", "mail-a", next_cursor="page-2", has_more=1)),
        _response(_page("mail-a", "mail-c")),
        _response(_message("mail-a")),
        _response(_message("mail-b")),
        _response(_message("mail-c")),
    )

    result = _provider(transport).poll(None, 10)

    assert [value.delivery_id for value in result.deliveries] == [
        "mail-a",
        "mail-b",
        "mail-c",
    ]
    list_bodies = [
        call["json_body"] for call in transport.calls if str(call["url"]).endswith("/get_mail_list")
    ]
    assert list_bodies == [
        {
            "begin_time": int(NOW.timestamp()),
            "end_time": int(NOW.replace(hour=7).timestamp()),
            "limit": 10,
        },
        {
            "begin_time": int(NOW.timestamp()),
            "end_time": int(NOW.replace(hour=7).timestamp()),
            "cursor": "page-2",
            "limit": 8,
        },
    ]
    read_ids = [
        call["json_body"] for call in transport.calls if str(call["url"]).endswith("/read_mail")
    ]
    assert read_ids == [{"mail_id": "mail-a"}, {"mail_id": "mail-b"}, {"mail_id": "mail-c"}]


def test_empty_page_returns_terminal_window_cursor_without_delivery() -> None:
    transport = _ScriptedTransport(_response(_token()), _response(_page()))

    result = _provider(transport).poll(None, 1000)

    assert result.deliveries == ()
    assert result.candidate_cursor is not None
    assert json.loads(result.candidate_cursor)["cursor"] == ""


def test_page_cursor_replay_preserves_exact_window_and_has_no_internal_advance() -> None:
    checkpoint = json.dumps(
        {
            "begin_time": int(NOW.timestamp()),
            "cursor": "page-2",
            "end_time": int(NOW.replace(hour=7).timestamp()),
            "v": 1,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    transport = _ScriptedTransport(
        _response(_token()),
        _response(_page("mail-002")),
        _response(_message("mail-002")),
        _response(_page("mail-002")),
        _response(_message("mail-002")),
    )
    provider = _provider(transport)

    first = provider.poll(checkpoint, 10)
    replay = provider.poll(checkpoint, 10)

    assert first == replay
    assert first.expected_cursor == checkpoint
    assert [
        call["json_body"] for call in transport.calls if str(call["url"]).endswith("/get_mail_list")
    ] == [
        {
            "begin_time": int(NOW.timestamp()),
            "end_time": int(NOW.replace(hour=7).timestamp()),
            "cursor": "page-2",
            "limit": 10,
        },
        {
            "begin_time": int(NOW.timestamp()),
            "end_time": int(NOW.replace(hour=7).timestamp()),
            "cursor": "page-2",
            "limit": 10,
        },
    ]
    assert not hasattr(provider, "checkpoint")
    assert not hasattr(provider, "advance_checkpoint")


def test_terminal_cursor_opens_bounded_overlap_window_not_before_activation() -> None:
    terminal = json.dumps(
        {
            "begin_time": int(NOW.timestamp()),
            "cursor": "",
            "end_time": int(NOW.replace(hour=7).timestamp()),
            "v": 1,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    transport = _ScriptedTransport(_response(_token()), _response(_page()))
    clock = _MutableClock(NOW.replace(hour=7, minute=10))

    _provider(transport, clock=clock).poll(terminal, 10)

    assert transport.calls[1]["json_body"] == {
        "begin_time": int(NOW.replace(hour=6, minute=55).timestamp()),
        "end_time": int(clock.now.timestamp()),
        "limit": 10,
    }


def test_initial_catch_up_starts_at_activation_and_advances_in_bounded_windows() -> None:
    activation = NOW.replace(hour=3)
    clock = _MutableClock(NOW.replace(hour=7))
    transport = _ScriptedTransport(
        _response(_token()),
        _response(_page()),
        _response(_page()),
    )
    provider = _provider(transport, config=_config(activation_watermark=activation), clock=clock)

    first = provider.poll(None, 10)
    provider.poll(first.candidate_cursor, 10)

    list_bodies = [
        call["json_body"] for call in transport.calls if str(call["url"]).endswith("/get_mail_list")
    ]
    assert list_bodies == [
        {
            "begin_time": int(activation.timestamp()),
            "end_time": int(activation.replace(hour=4).timestamp()),
            "limit": 10,
        },
        {
            "begin_time": int(activation.replace(hour=3, minute=55).timestamp()),
            "end_time": int(activation.replace(hour=4, minute=55).timestamp()),
            "limit": 10,
        },
    ]


def test_pre_activation_date_is_rejected_and_exact_boundary_is_accepted() -> None:
    transport = _ScriptedTransport(
        _response(_token()),
        _response(_page("mail-before", "mail-exact")),
        _response(_message("mail-before", date="Fri, 14 Aug 2026 05:59:59 +0000")),
        _response(_message("mail-exact", date="Fri, 14 Aug 2026 06:00:00 +0000")),
    )

    result = _provider(transport).poll(None, 10)

    assert [value.delivery_id for value in result.deliveries] == ["mail-exact"]
    assert result.deliveries[0].received_at == NOW


def test_minimally_checked_eml_still_reaches_common_downstream_quarantine_path() -> None:
    malformed = "Date: invalid\r\n\r\nnot a valid MIME message"
    transport = _ScriptedTransport(
        _response(_token()),
        _response(_page("mail-malformed")),
        _response({"errcode": 0, "errmsg": "ok", "mail_data": malformed}),
    )

    delivery = _provider(transport).poll(None, 10).deliveries[0]

    assert delivery.exact_bytes == malformed.encode("utf-8")
    assert delivery.media_type == "message/rfc822"


@pytest.mark.parametrize("errcode", [40014, 42001])
def test_invalid_token_is_invalidated_and_refreshed_exactly_once(errcode: int) -> None:
    transport = _ScriptedTransport(
        _response(_token("token-one")),
        _response({"errcode": errcode, "errmsg": "synthetic token rejected"}),
        _response(_token("token-two")),
        _response(_page()),
    )

    _provider(transport).poll(None, 10)

    assert _urls(transport).count("https://qyapi.weixin.qq.com/cgi-bin/gettoken") == 2
    assert transport.calls[1]["query"] == {"access_token": "token-one"}
    assert transport.calls[3]["query"] == {"access_token": "token-two"}


def test_second_invalid_token_fails_closed_without_a_third_refresh() -> None:
    transport = _ScriptedTransport(
        _response(_token("token-one")),
        _response({"errcode": 40014, "errmsg": "raw first rejection"}),
        _response(_token("token-two")),
        _response({"errcode": 42001, "errmsg": "raw second rejection"}),
    )

    with pytest.raises(EmailProviderError) as captured:
        _provider(transport).poll(None, 10)

    assert captured.value.code == "wecom_app_mail.token_rejected_42001"
    assert len(transport.calls) == 4
    assert "raw" not in repr(captured.value)


@pytest.mark.parametrize(
    ("errcode", "safe_code"),
    [
        (45009, "wecom_app_mail.rate_limited_45009"),
        (48004, "wecom_app_mail.authorization_invalid_48004"),
        (48006, "wecom_app_mail.permission_reclaimed_48006"),
        (50003, "wecom_app_mail.application_disabled_50003"),
        (60031, "wecom_app_mail.application_prohibited_60031"),
    ],
)
def test_governed_provider_errors_return_typed_pause_without_retry_metadata(
    errcode: int,
    safe_code: str,
) -> None:
    transport = _ScriptedTransport(
        _response(_token()),
        _response({"errcode": errcode, "errmsg": "raw provider message"}),
    )

    with pytest.raises(WeComAppMailPause) as captured:
        _provider(transport).poll(None, 10)

    assert captured.value.code == safe_code
    assert captured.value.retryable is False
    assert len(transport.calls) == 2
    assert not hasattr(captured.value, "retry_after")
    assert not hasattr(captured.value, "delay")
    assert "429" not in repr(captured.value)
    assert "raw provider" not in repr(captured.value)


def test_transient_5xx_retries_are_bounded_and_do_not_change_request() -> None:
    transport = _ScriptedTransport(
        _response(_token()),
        _response({"unsafe": "body"}, status=503),
        _response(_page()),
    )

    result = _provider(transport, config=_config(transient_retries=1)).poll(None, 10)

    assert result.deliveries == ()
    assert transport.calls[1] == transport.calls[2]


def test_timeout_is_bounded_safe_and_replay_starts_from_caller_checkpoint() -> None:
    transport = _ScriptedTransport(
        _response(_token()),
        TimeoutError("synthetic-token leaked in timeout"),
        TimeoutError("synthetic-token leaked again"),
        _response(_page("mail-replayed")),
        _response(_message("mail-replayed")),
    )
    provider = _provider(transport, config=_config(transient_retries=1))

    with pytest.raises(EmailProviderError) as captured:
        provider.poll(None, 10)
    recovered = provider.poll(None, 10)

    assert captured.value.code == "wecom_app_mail.transport_timeout"
    assert "synthetic-token" not in repr(captured.value)
    assert recovered.expected_cursor is None
    assert [value.delivery_id for value in recovered.deliveries] == ["mail-replayed"]


def test_unexpected_transport_crash_is_redacted_and_not_automatically_retried() -> None:
    transport = _ScriptedTransport(
        RuntimeError("synthetic-app-secret raw payload"),
    )

    with pytest.raises(EmailProviderError) as captured:
        _provider(transport).poll(None, 10)

    assert captured.value.code == "wecom_app_mail.transport_failure"
    assert len(transport.calls) == 1
    assert "synthetic-app-secret" not in repr(captured.value)


@pytest.mark.parametrize(
    "bad_payload",
    [
        {
            "errcode": 0,
            "errmsg": "ok",
            "next_cursor": "",
            "has_more": 0,
            "mail_list": [],
            "extra": 1,
        },
        {"errcode": 0, "errmsg": "ok", "next_cursor": "", "has_more": 2, "mail_list": []},
        {"errcode": 49999, "errmsg": "unknown provider error"},
        {"errcode": 45009, "errmsg": "rate", "retry_after": 60},
    ],
)
def test_unknown_json_and_extra_fields_fail_closed_without_payload_leak(
    bad_payload: object,
) -> None:
    transport = _ScriptedTransport(_response(_token()), _response(bad_payload))

    with pytest.raises(EmailProviderError) as captured:
        _provider(transport).poll(None, 10)

    assert captured.value.code == "wecom_app_mail.invalid_response"
    assert "unknown provider" not in repr(captured.value)
    assert "retry_after" not in repr(captured.value)


def test_duplicate_json_keys_and_oversized_response_fail_safely() -> None:
    duplicate = WeComAppMailHTTPResponse(
        status_code=200,
        body=b'{"errcode":0,"errcode":0,"errmsg":"ok"}',
    )
    oversized = WeComAppMailHTTPResponse(status_code=200, body=b"x" * 513)

    for response in (duplicate, oversized):
        transport = _ScriptedTransport(response)
        with pytest.raises(EmailProviderError) as captured:
            _provider(transport, config=_config(max_response_bytes=512)).poll(None, 10)
        assert captured.value.code == "wecom_app_mail.invalid_response"


def test_oversized_eml_fails_before_raw_delivery_and_never_leaks_content() -> None:
    raw = "Date: Fri, 14 Aug 2026 06:00:00 +0000\r\n\r\n" + "SECRET-EML" * 110
    transport = _ScriptedTransport(
        _response(_token()),
        _response(_page("mail-large")),
        _response({"errcode": 0, "errmsg": "ok", "mail_data": raw}),
    )

    with pytest.raises(EmailProviderError) as captured:
        _provider(transport).poll(None, 10)

    assert captured.value.code == "wecom_app_mail.invalid_response"
    assert "SECRET-EML" not in repr(captured.value)


@pytest.mark.parametrize("limit", [0, 1001, True])
def test_poll_limit_is_strictly_bounded(limit: int) -> None:
    provider = WeComAppMailProvider(
        config=_config(),
        transport=_NoCallsTransport(),
        clock=lambda: NOW,
    )
    with pytest.raises(ValueError, match="limit"):
        provider.poll(None, limit)


def test_provider_rejects_page_larger_than_requested_limit() -> None:
    transport = _ScriptedTransport(
        _response(_token()),
        _response(_page("mail-1", "mail-2")),
    )
    with pytest.raises(EmailProviderError, match="wecom_app_mail.invalid_response"):
        _provider(transport).poll(None, 1)


@pytest.mark.parametrize(
    "checkpoint",
    [
        "not-json",
        '{"begin_time":1,"cursor":"","end_time":2,"v":1,"extra":true}',
        '{"begin_time":2,"cursor":"","end_time":1,"v":1}',
        '{"begin_time":1,"cursor":false,"end_time":2,"v":1}',
        '{"begin_time":1,"cursor":"x","end_time":2,"v":2}',
    ],
)
def test_cursor_is_closed_canonical_and_invalid_input_makes_no_transport_call(
    checkpoint: str,
) -> None:
    provider = WeComAppMailProvider(
        config=_config(),
        transport=_NoCallsTransport(),
        clock=lambda: NOW,
    )
    with pytest.raises(EmailProviderError, match="wecom_app_mail.invalid_cursor"):
        provider.poll(checkpoint, 10)


def test_config_and_provider_repr_and_validation_redact_all_sensitive_values() -> None:
    config = _config()
    provider = WeComAppMailProvider(
        config=config,
        transport=_NoCallsTransport(),
        clock=lambda: NOW,
    )
    combined = repr(config) + repr(provider)
    for secret in (
        config.corp_id,
        config.app_id,
        config.app_secret,
        config.mailbox,
    ):
        assert secret not in combined

    invalid_values: list[dict[str, object]] = [
        {"corp_id": " bad-corp"},
        {"app_id": "not-digits"},
        {"app_secret": ""},
        {"mailbox": "not-an-address"},
        {"activation_watermark": NOW.replace(tzinfo=None)},
        {"overlap_seconds": 3601},
        {"transient_retries": 4},
        {"max_response_bytes": 10_000_001},
    ]
    for changes in invalid_values:
        with pytest.raises((TypeError, ValueError)) as captured:
            _config(**changes)
        unsafe_value = str(next(iter(changes.values())))
        if unsafe_value:
            assert unsafe_value not in str(captured.value)


def test_http_response_repr_redacts_raw_body() -> None:
    response = WeComAppMailHTTPResponse(status_code=200, body=b"raw provider JSON secret")
    assert "raw provider" not in repr(response)
    assert "bytes=24" in repr(response)
