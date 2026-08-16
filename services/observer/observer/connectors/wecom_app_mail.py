"""Credential-isolated WeCom application-mail inbound provider core."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesHeaderParser
from email.utils import parsedate_to_datetime
from typing import Protocol, cast

from ..models import RawDelivery
from .email_provider import EmailProviderError, EmailProviderPollResult

_TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
_LIST_URL = "https://qyapi.weixin.qq.com/cgi-bin/exmail/app/get_mail_list"
_READ_URL = "https://qyapi.weixin.qq.com/cgi-bin/exmail/app/read_mail"

_CORP_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_APP_ID = re.compile(r"^[0-9]{1,20}$")
_SECRET = re.compile(r"^[A-Za-z0-9._-]{1,512}$")
_MAILBOX = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9.-]{1,189}$")
_TOKEN = re.compile(r"^[A-Za-z0-9._-]{1,512}$")

_TOKEN_KEYS = frozenset({"errcode", "errmsg", "access_token", "expires_in"})
_LIST_KEYS = frozenset({"errcode", "errmsg", "next_cursor", "has_more", "mail_list"})
_MESSAGE_KEYS = frozenset({"errcode", "errmsg", "mail_data"})
_ERROR_KEYS = frozenset({"errcode", "errmsg"})
_CURSOR_KEYS = frozenset({"begin_time", "cursor", "end_time", "v"})
_TOKEN_REJECTION_CODES = frozenset({40014, 42001})
_MAX_EML_BYTES = 1024

_PAUSE_CODES = {
    45009: "wecom_app_mail.rate_limited_45009",
    48004: "wecom_app_mail.authorization_invalid_48004",
    48006: "wecom_app_mail.permission_reclaimed_48006",
    50003: "wecom_app_mail.application_disabled_50003",
    60031: "wecom_app_mail.application_prohibited_60031",
}


class _DuplicateJSONKey(ValueError):
    pass


class _InvalidJSONConstant(ValueError):
    pass


def _invalid_response() -> EmailProviderError:
    return EmailProviderError("wecom_app_mail.invalid_response", retryable=False)


def _require_aware_seconds(value: datetime, field_name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
        or value.microsecond != 0
    ):
        raise ValueError(f"invalid {field_name}")


def _json_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKey
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    del value
    raise _InvalidJSONConstant


@dataclass(frozen=True, slots=True, repr=False)
class WeComAppMailHTTPResponse:
    """Exact injected transport response with a body-redacting representation."""

    status_code: int
    body: bytes

    def __post_init__(self) -> None:
        if type(self.status_code) is not int or not 100 <= self.status_code <= 599:
            raise ValueError("invalid transport status")
        if not isinstance(self.body, bytes):
            raise TypeError("transport body must be bytes")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(status_code={self.status_code}, "
            f"body=<redacted bytes={len(self.body)}>)"
        )


class WeComAppMailTransport(Protocol):
    """The sole trust, environment, and network seam; no default implementation exists."""

    def request(
        self,
        *,
        method: str,
        url: str,
        query: Mapping[str, str],
        json_body: Mapping[str, object] | None,
        timeout_seconds: float,
    ) -> WeComAppMailHTTPResponse: ...


@dataclass(frozen=True, slots=True, repr=False)
class WeComAppMailConfig:
    """Closed application-scoped configuration; every identifying value is redacted."""

    corp_id: str
    app_id: str
    app_secret: str
    mailbox: str
    activation_watermark: datetime
    window_seconds: int = 3600
    overlap_seconds: int = 300
    token_early_expiry_seconds: int = 60
    request_timeout_seconds: float = 10.0
    transient_retries: int = 2
    max_response_bytes: int = 2_000_000
    max_pages: int = 100

    def __post_init__(self) -> None:
        if not isinstance(self.corp_id, str) or _CORP_ID.fullmatch(self.corp_id) is None:
            raise ValueError("invalid corp_id")
        if not isinstance(self.app_id, str) or _APP_ID.fullmatch(self.app_id) is None:
            raise ValueError("invalid app_id")
        if not isinstance(self.app_secret, str) or _SECRET.fullmatch(self.app_secret) is None:
            raise ValueError("invalid app_secret")
        if not isinstance(self.mailbox, str) or _MAILBOX.fullmatch(self.mailbox) is None:
            raise ValueError("invalid mailbox")
        _require_aware_seconds(self.activation_watermark, "activation_watermark")
        if type(self.window_seconds) is not int or not 1 <= self.window_seconds <= 86_400:
            raise ValueError("invalid window_seconds")
        if (
            type(self.overlap_seconds) is not int
            or not 0 <= self.overlap_seconds < self.window_seconds
        ):
            raise ValueError("invalid overlap_seconds")
        if (
            type(self.token_early_expiry_seconds) is not int
            or not 0 <= self.token_early_expiry_seconds <= 3600
        ):
            raise ValueError("invalid token_early_expiry_seconds")
        if (
            isinstance(self.request_timeout_seconds, bool)
            or not isinstance(self.request_timeout_seconds, (int, float))
            or not 0.1 <= float(self.request_timeout_seconds) <= 60.0
        ):
            raise ValueError("invalid request_timeout_seconds")
        if type(self.transient_retries) is not int or not 0 <= self.transient_retries <= 3:
            raise ValueError("invalid transient_retries")
        if (
            type(self.max_response_bytes) is not int
            or not 128 <= self.max_response_bytes <= 10_000_000
        ):
            raise ValueError("invalid max_response_bytes")
        if type(self.max_pages) is not int or not 1 <= self.max_pages <= 1000:
            raise ValueError("invalid max_pages")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(identity=<redacted>, credential=<redacted>, "
            f"window_seconds={self.window_seconds}, overlap_seconds={self.overlap_seconds}, "
            f"max_response_bytes={self.max_response_bytes}, max_pages={self.max_pages})"
        )


class WeComAppMailPause(EmailProviderError):
    """A typed mailbox-local pause for governance handling by a later runtime wrapper."""

    def __init__(self, code: str) -> None:
        super().__init__(code, retryable=False)


@dataclass(frozen=True, slots=True)
class _CursorState:
    begin_time: int
    end_time: int
    cursor: str

    def canonical(self) -> str:
        return json.dumps(
            {
                "begin_time": self.begin_time,
                "cursor": self.cursor,
                "end_time": self.end_time,
                "v": 1,
            },
            separators=(",", ":"),
            sort_keys=True,
        )


class WeComAppMailProvider:
    """Pull full application-mail EML while leaving checkpoint authority to Observer."""

    __slots__ = (
        "_cached_token",
        "_clock",
        "_config",
        "_token_usable_until",
        "_transport",
    )

    def __init__(
        self,
        *,
        config: WeComAppMailConfig,
        transport: WeComAppMailTransport,
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(config, WeComAppMailConfig):
            raise TypeError("invalid WeCom application-mail config")
        if not hasattr(transport, "request") or not callable(transport.request):
            raise TypeError("invalid WeCom application-mail transport")
        if not callable(clock):
            raise TypeError("invalid WeCom application-mail clock")
        self._config = config
        self._transport = transport
        self._clock = clock
        self._cached_token: str | None = None
        self._token_usable_until: datetime | None = None

    @property
    def provider_kind(self) -> str:
        return "wecom_app_mail"

    def poll(self, checkpoint: str | None, limit: int) -> EmailProviderPollResult:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        previous = self._parse_cursor(checkpoint)
        now = self._now()
        window = self._window(previous, now)
        mail_ids, candidate_page_cursor = self._list_mail_ids(window, limit)
        candidate = _CursorState(
            begin_time=window.begin_time,
            end_time=window.end_time,
            cursor=candidate_page_cursor,
        ).canonical()
        deliveries = self._read_deliveries(mail_ids, window)
        return EmailProviderPollResult(
            expected_cursor=checkpoint,
            candidate_cursor=candidate,
            deliveries=deliveries,
        )

    def _now(self) -> datetime:
        value = self._clock()
        _require_aware_seconds(value, "clock")
        return value.astimezone(UTC)

    def _parse_cursor(self, checkpoint: str | None) -> _CursorState | None:
        if checkpoint is None:
            return None
        if not isinstance(checkpoint, str) or not checkpoint or len(checkpoint) > 4096:
            raise EmailProviderError("wecom_app_mail.invalid_cursor", retryable=False)
        try:
            raw = json.loads(
                checkpoint,
                object_pairs_hook=_json_object_pairs,
                parse_constant=_reject_json_constant,
            )
            if not isinstance(raw, dict) or frozenset(raw) != _CURSOR_KEYS:
                raise ValueError
            begin_time = raw["begin_time"]
            end_time = raw["end_time"]
            cursor = raw["cursor"]
            version = raw["v"]
            if (
                type(begin_time) is not int
                or type(end_time) is not int
                or begin_time < 0
                or end_time < begin_time
                or not isinstance(cursor, str)
                or len(cursor) > 256
                or version != 1
                or type(version) is not int
            ):
                raise ValueError
            state = _CursorState(begin_time, end_time, cursor)
            if checkpoint != state.canonical():
                raise ValueError
            return state
        except KeyError, TypeError, ValueError, json.JSONDecodeError:
            raise EmailProviderError("wecom_app_mail.invalid_cursor", retryable=False) from None

    def _window(self, previous: _CursorState | None, now: datetime) -> _CursorState:
        now_epoch = int(now.timestamp())
        activation = int(self._config.activation_watermark.astimezone(UTC).timestamp())
        if now_epoch < activation:
            raise EmailProviderError("wecom_app_mail.activation_not_reached", retryable=False)
        if previous is None:
            begin_time = activation
            cursor = ""
        elif previous.cursor:
            return previous
        else:
            begin_time = max(activation, previous.end_time - self._config.overlap_seconds)
            cursor = ""
        end_time = min(now_epoch, begin_time + self._config.window_seconds)
        return _CursorState(begin_time=begin_time, end_time=end_time, cursor=cursor)

    def _list_mail_ids(self, window: _CursorState, limit: int) -> tuple[set[str], str]:
        mail_ids: set[str] = set()
        cursor = window.cursor
        page_cursors = {cursor} if cursor else set()
        for _page_number in range(self._config.max_pages):
            remaining = limit - len(mail_ids)
            body: dict[str, object] = {
                "begin_time": window.begin_time,
                "end_time": window.end_time,
                "limit": remaining,
            }
            if cursor:
                body["cursor"] = cursor
            payload = self._authorized_json(method="POST", url=_LIST_URL, json_body=body)
            entries, next_cursor, has_more = self._validate_list_page(payload, remaining)
            mail_ids.update(entries)
            if not has_more:
                if next_cursor:
                    raise _invalid_response()
                return mail_ids, ""
            if not next_cursor or next_cursor in page_cursors:
                raise _invalid_response()
            if len(mail_ids) >= limit:
                return mail_ids, next_cursor
            page_cursors.add(next_cursor)
            cursor = next_cursor
        raise _invalid_response()

    def _validate_list_page(
        self,
        payload: Mapping[str, object],
        requested_limit: int,
    ) -> tuple[tuple[str, ...], str, bool]:
        if frozenset(payload) != _LIST_KEYS:
            raise _invalid_response()
        self._require_ok(payload)
        next_cursor = payload["next_cursor"]
        has_more = payload["has_more"]
        raw_entries = payload["mail_list"]
        if (
            not isinstance(next_cursor, str)
            or len(next_cursor) > 256
            or type(has_more) is not int
            or has_more not in {0, 1}
            or not isinstance(raw_entries, list)
            or len(raw_entries) > requested_limit
        ):
            raise _invalid_response()
        entries: list[str] = []
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, dict) or frozenset(raw_entry) != {"mail_id"}:
                raise _invalid_response()
            mail_id = raw_entry["mail_id"]
            if (
                not isinstance(mail_id, str)
                or not mail_id
                or mail_id != mail_id.strip()
                or len(mail_id) > 512
            ):
                raise _invalid_response()
            entries.append(mail_id)
        return tuple(entries), next_cursor, has_more == 1

    def _read_deliveries(
        self,
        mail_ids: set[str],
        window: _CursorState,
    ) -> tuple[RawDelivery, ...]:
        deliveries: list[RawDelivery] = []
        activation = self._config.activation_watermark.astimezone(UTC)
        fallback_received_at = datetime.fromtimestamp(window.end_time, tz=UTC)
        for mail_id in sorted(mail_ids):
            payload = self._authorized_json(
                method="POST",
                url=_READ_URL,
                json_body={"mail_id": mail_id},
            )
            if frozenset(payload) != _MESSAGE_KEYS:
                raise _invalid_response()
            self._require_ok(payload)
            mail_data = payload["mail_data"]
            if not isinstance(mail_data, str) or not mail_data or "\x00" in mail_data:
                raise _invalid_response()
            try:
                exact_bytes = mail_data.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                raise _invalid_response() from None
            if len(exact_bytes) > _MAX_EML_BYTES:
                raise _invalid_response()
            header_date = self._message_date(exact_bytes)
            if header_date is not None and header_date < activation:
                continue
            deliveries.append(
                RawDelivery(
                    delivery_id=mail_id,
                    exact_bytes=exact_bytes,
                    media_type="message/rfc822",
                    received_at=header_date or fallback_received_at,
                )
            )
        return tuple(deliveries)

    @staticmethod
    def _message_date(exact_bytes: bytes) -> datetime | None:
        try:
            parsed = BytesHeaderParser(policy=policy.default).parsebytes(exact_bytes)
            raw_date = parsed.get("Date")
            if raw_date is None:
                return None
            value = parsedate_to_datetime(str(raw_date))
            if value.tzinfo is None or value.utcoffset() is None:
                return None
            return value.astimezone(UTC).replace(microsecond=0)
        except TypeError, ValueError, OverflowError:
            return None

    def _authorized_json(
        self,
        *,
        method: str,
        url: str,
        json_body: Mapping[str, object],
    ) -> Mapping[str, object]:
        for refresh_attempt in range(2):
            token = self._access_token()
            payload = self._request_json(
                method=method,
                url=url,
                query={"access_token": token},
                json_body=json_body,
            )
            errcode = self._errcode(payload)
            if errcode in _TOKEN_REJECTION_CODES:
                self._invalidate_token()
                if refresh_attempt == 0:
                    continue
                raise EmailProviderError(
                    f"wecom_app_mail.token_rejected_{errcode}", retryable=False
                )
            if errcode != 0:
                self._raise_provider_error(payload)
            return payload
        raise AssertionError("unreachable token refresh loop")

    def _access_token(self) -> str:
        now = self._now()
        if (
            self._cached_token is not None
            and self._token_usable_until is not None
            and now < self._token_usable_until
        ):
            return self._cached_token
        payload = self._request_json(
            method="GET",
            url=_TOKEN_URL,
            query={
                "corpid": self._config.corp_id,
                "corpsecret": self._config.app_secret,
            },
            json_body=None,
        )
        errcode = self._errcode(payload)
        if errcode != 0:
            self._raise_provider_error(payload)
        if frozenset(payload) != _TOKEN_KEYS:
            raise _invalid_response()
        self._require_ok(payload)
        access_token = payload["access_token"]
        expires_in = payload["expires_in"]
        if (
            not isinstance(access_token, str)
            or _TOKEN.fullmatch(access_token) is None
            or type(expires_in) is not int
            or not 1 <= expires_in <= 86_400
        ):
            raise _invalid_response()
        early = min(self._config.token_early_expiry_seconds, max(0, expires_in - 1))
        self._cached_token = access_token
        self._token_usable_until = now + timedelta(seconds=expires_in - early)
        return access_token

    def _invalidate_token(self) -> None:
        self._cached_token = None
        self._token_usable_until = None

    def _request_json(
        self,
        *,
        method: str,
        url: str,
        query: Mapping[str, str],
        json_body: Mapping[str, object] | None,
    ) -> Mapping[str, object]:
        response: WeComAppMailHTTPResponse | None = None
        for attempt in range(self._config.transient_retries + 1):
            try:
                response = self._transport.request(
                    method=method,
                    url=url,
                    query=query,
                    json_body=json_body,
                    timeout_seconds=float(self._config.request_timeout_seconds),
                )
            except TimeoutError:
                if attempt < self._config.transient_retries:
                    continue
                raise EmailProviderError("wecom_app_mail.transport_timeout") from None
            except Exception:
                raise EmailProviderError("wecom_app_mail.transport_failure") from None
            if not isinstance(response, WeComAppMailHTTPResponse):
                raise EmailProviderError("wecom_app_mail.transport_failure")
            if 500 <= response.status_code <= 599:
                if attempt < self._config.transient_retries:
                    continue
                raise EmailProviderError("wecom_app_mail.transport_5xx")
            break
        if response is None:
            raise AssertionError("unreachable transport loop")
        if response.status_code != 200 or len(response.body) > self._config.max_response_bytes:
            raise _invalid_response()
        try:
            decoded = json.loads(
                response.body.decode("utf-8", errors="strict"),
                object_pairs_hook=_json_object_pairs,
                parse_constant=_reject_json_constant,
            )
        except UnicodeDecodeError, ValueError, json.JSONDecodeError:
            raise _invalid_response() from None
        if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
            raise _invalid_response()
        return cast(Mapping[str, object], decoded)

    @staticmethod
    def _errcode(payload: Mapping[str, object]) -> int:
        errcode = payload.get("errcode")
        if type(errcode) is not int:
            raise _invalid_response()
        return errcode

    @staticmethod
    def _require_ok(payload: Mapping[str, object]) -> None:
        if payload.get("errcode") != 0 or payload.get("errmsg") != "ok":
            raise _invalid_response()

    @staticmethod
    def _raise_provider_error(payload: Mapping[str, object]) -> None:
        if frozenset(payload) != _ERROR_KEYS:
            raise _invalid_response()
        errcode = payload.get("errcode")
        errmsg = payload.get("errmsg")
        if type(errcode) is not int or not isinstance(errmsg, str) or not 1 <= len(errmsg) <= 1024:
            raise _invalid_response()
        if errcode in _PAUSE_CODES:
            raise WeComAppMailPause(_PAUSE_CODES[errcode])
        if errcode in _TOKEN_REJECTION_CODES:
            raise EmailProviderError(f"wecom_app_mail.token_rejected_{errcode}", retryable=False)
        raise _invalid_response()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(network=injected, credentials=<redacted>, "
            f"window_seconds={self._config.window_seconds}, "
            f"max_pages={self._config.max_pages})"
        )


__all__ = [
    "WeComAppMailConfig",
    "WeComAppMailHTTPResponse",
    "WeComAppMailPause",
    "WeComAppMailProvider",
    "WeComAppMailTransport",
]
