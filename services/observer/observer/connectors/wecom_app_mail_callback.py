"""Closed verification for encrypted WeCom application-mail callback signals."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import struct
import xml.etree.ElementTree as ElementTree
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_SIGNATURE = re.compile(r"^[a-f0-9]{40}$")
_TIMESTAMP = re.compile(r"^[0-9]{1,10}$")
_TOKEN = re.compile(r"^[A-Za-z0-9]{1,32}$")
_AES_KEY = re.compile(r"^[A-Za-z0-9]{43}$")
_BOUND_VALUE = re.compile(r"^[^\x00\r\n]{1,256}$")
_ENVELOPE_FIELDS = frozenset({"ToUserName", "AgentID", "Encrypt"})
_EVENT_FIELDS = frozenset(
    {
        "ToUserName",
        "FromUserName",
        "CreateTime",
        "MsgType",
        "Event",
        "ChangeType",
        "Amount",
    }
)


class WeComAppMailCallbackError(ValueError):
    """Stable public failure that never retains provider input or plaintext."""

    __slots__ = ("code", "status_code")

    def __init__(self, code: str, *, status_code: int = 422) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, status_code={self.status_code})"


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedWeComAppMailSignal:
    signal_kind: str
    count_hint: int
    callback_timestamp: datetime
    payload_digest: str
    nonce_digest: str
    replay_key_digest: str

    def __repr__(self) -> str:
        return (
            "VerifiedWeComAppMailSignal(signal_kind='callback', "
            f"count_hint={self.count_hint}, callback_timestamp="
            f"{self.callback_timestamp!r}, digests=<redacted>)"
        )


class WeComAppMailCallbackVerifier:
    """Verify the official SHA-1/AES-256-CBC callback envelope without I/O."""

    __slots__ = (
        "_agent_id",
        "_callback_token",
        "_corp_id",
        "_key",
        "_max_body_bytes",
        "_max_query_bytes",
        "_timestamp_tolerance",
    )

    def __init__(
        self,
        *,
        callback_token: str,
        encoding_aes_key: str,
        corp_id: str,
        agent_id: str,
        timestamp_tolerance: timedelta = timedelta(minutes=5),
        max_body_bytes: int = 65_536,
        max_query_bytes: int = 1_024,
    ) -> None:
        if not isinstance(callback_token, str) or _TOKEN.fullmatch(callback_token) is None:
            raise ValueError("invalid callback token")
        if not isinstance(encoding_aes_key, str) or _AES_KEY.fullmatch(encoding_aes_key) is None:
            raise ValueError("invalid callback AES key")
        try:
            key = base64.b64decode(encoding_aes_key + "=", validate=True)
        except binascii.Error as exc:
            raise ValueError("invalid callback AES key") from exc
        if len(key) != 32:
            raise ValueError("invalid callback AES key")
        if any(
            not isinstance(value, str)
            or _BOUND_VALUE.fullmatch(value) is None
            or len(value.encode("utf-8")) > 256
            for value in (corp_id, agent_id)
        ):
            raise ValueError("invalid callback receiver binding")
        if (
            not isinstance(timestamp_tolerance, timedelta)
            or not timedelta(seconds=1) <= timestamp_tolerance <= timedelta(minutes=15)
            or isinstance(max_body_bytes, bool)
            or not isinstance(max_body_bytes, int)
            or not 1 <= max_body_bytes <= 65_536
            or isinstance(max_query_bytes, bool)
            or not isinstance(max_query_bytes, int)
            or not 1 <= max_query_bytes <= 4_096
        ):
            raise ValueError("invalid callback boundary")
        self._callback_token = callback_token
        self._key = key
        self._corp_id = corp_id
        self._agent_id = agent_id
        self._timestamp_tolerance = timestamp_tolerance
        self._max_body_bytes = max_body_bytes
        self._max_query_bytes = max_query_bytes

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(callback_token=<redacted>, "
            "encoding_aes_key=<redacted>, receiver_bindings=<redacted>)"
        )

    def verify_challenge(self, *, query: Mapping[str, str], now: datetime) -> str:
        values, callback_timestamp = self._validate_query(
            query,
            expected=frozenset({"msg_signature", "timestamp", "nonce", "echostr"}),
            now=now,
        )
        del callback_timestamp
        encrypted = values["echostr"]
        self._verify_signature(values, encrypted)
        plaintext, receiver = self._decrypt(encrypted)
        if not hmac.compare_digest(receiver, self._corp_id):
            raise WeComAppMailCallbackError("receiver_invalid")
        try:
            challenge = plaintext.decode("utf-8")
        except UnicodeDecodeError:
            raise WeComAppMailCallbackError("encoding_invalid") from None
        if not 1 <= len(challenge.encode("utf-8")) <= 256 or any(
            character in challenge for character in ("\r", "\n", "\ufeff", '"', "\x00")
        ):
            raise WeComAppMailCallbackError("challenge_invalid")
        return challenge

    def verify_event(
        self,
        *,
        query: Mapping[str, str],
        body: bytes,
        now: datetime,
    ) -> VerifiedWeComAppMailSignal:
        values, callback_timestamp = self._validate_query(
            query,
            expected=frozenset({"msg_signature", "timestamp", "nonce"}),
            now=now,
        )
        if not isinstance(body, bytes):
            raise WeComAppMailCallbackError("xml_invalid")
        if len(body) > self._max_body_bytes:
            raise WeComAppMailCallbackError("payload_too_large", status_code=413)
        envelope = _closed_xml(body, expected=_ENVELOPE_FIELDS)
        if not hmac.compare_digest(envelope["ToUserName"], self._corp_id):
            raise WeComAppMailCallbackError("receiver_invalid")
        if not hmac.compare_digest(envelope["AgentID"], self._agent_id):
            raise WeComAppMailCallbackError("agent_invalid")
        encrypted = envelope["Encrypt"]
        self._verify_signature(values, encrypted)
        plaintext, receiver = self._decrypt(encrypted)
        if not hmac.compare_digest(receiver, self._corp_id):
            raise WeComAppMailCallbackError("receiver_invalid")
        event = _closed_xml(plaintext, expected=_EVENT_FIELDS)
        try:
            event_timestamp = int(event["CreateTime"])
            amount = int(event["Amount"])
        except ValueError:
            raise WeComAppMailCallbackError("event_invalid") from None
        if (
            event["FromUserName"] != "sys"
            or event["MsgType"] != "event"
            or event["Event"] != "app_email_change"
            or event["ChangeType"] != "receive_email"
            or event_timestamp != int(callback_timestamp.timestamp())
            or not 0 <= amount <= 4_294_967_295
        ):
            raise WeComAppMailCallbackError("event_invalid")
        payload_digest = _digest(body)
        nonce_digest = _digest(values["nonce"].encode())
        replay_key_digest = _digest((values["timestamp"] + "\0" + values["nonce"]).encode())
        return VerifiedWeComAppMailSignal(
            signal_kind="callback",
            count_hint=amount,
            callback_timestamp=callback_timestamp,
            payload_digest=payload_digest,
            nonce_digest=nonce_digest,
            replay_key_digest=replay_key_digest,
        )

    def _validate_query(
        self,
        query: Mapping[str, str],
        *,
        expected: frozenset[str],
        now: datetime,
    ) -> tuple[dict[str, str], datetime]:
        if not isinstance(query, Mapping) or set(query) != expected:
            raise WeComAppMailCallbackError("query_invalid")
        values = dict(query)
        if any(not isinstance(value, str) for value in values.values()):
            raise WeComAppMailCallbackError("query_invalid")
        encoded_size = sum(
            len(key.encode()) + len(value.encode()) + 2 for key, value in values.items()
        )
        if (
            encoded_size > self._max_query_bytes
            or _SIGNATURE.fullmatch(values["msg_signature"]) is None
            or _TIMESTAMP.fullmatch(values["timestamp"]) is None
            or not 1 <= len(values["nonce"].encode()) <= 128
            or any(character in values["nonce"] for character in "\x00\r\n")
        ):
            raise WeComAppMailCallbackError("query_invalid")
        try:
            _require_aware(now)
            callback_timestamp = datetime.fromtimestamp(int(values["timestamp"]), tz=UTC)
        except OverflowError, OSError, ValueError:
            raise WeComAppMailCallbackError("timestamp_invalid") from None
        if abs(now.astimezone(UTC) - callback_timestamp) > self._timestamp_tolerance:
            raise WeComAppMailCallbackError("timestamp_invalid")
        return values, callback_timestamp

    def _verify_signature(self, query: Mapping[str, str], encrypted: str) -> None:
        joined = "".join(
            sorted(
                (
                    self._callback_token,
                    query["timestamp"],
                    query["nonce"],
                    encrypted,
                )
            )
        )
        expected = hashlib.sha1(joined.encode()).hexdigest()  # noqa: S324 - official contract
        if not hmac.compare_digest(expected, query["msg_signature"]):
            raise WeComAppMailCallbackError("signature_invalid", status_code=401)

    def _decrypt(self, encrypted: str) -> tuple[bytes, str]:
        if (
            not isinstance(encrypted, str)
            or not encrypted
            or len(encrypted) > 65_536
            or any(character.isspace() for character in encrypted)
        ):
            raise WeComAppMailCallbackError("encryption_invalid")
        try:
            ciphertext = base64.b64decode(encrypted, validate=True)
            if not ciphertext or len(ciphertext) % 16:
                raise ValueError
            decryptor = Cipher(algorithms.AES(self._key), modes.CBC(self._key[:16])).decryptor()
            padded = decryptor.update(ciphertext) + decryptor.finalize()
            padding = padded[-1]
            if not 1 <= padding <= 32 or not hmac.compare_digest(
                padded[-padding:], bytes([padding]) * padding
            ):
                raise ValueError
            framed = padded[:-padding]
            if len(framed) < 21:
                raise ValueError
            content_length = struct.unpack(">I", framed[16:20])[0]
            content_end = 20 + content_length
            if content_length < 1 or content_end >= len(framed):
                raise ValueError
            content = framed[20:content_end]
            receiver_bytes = framed[content_end:]
            receiver = receiver_bytes.decode("utf-8")
            if _BOUND_VALUE.fullmatch(receiver) is None:
                raise ValueError
        except binascii.Error, UnicodeDecodeError, ValueError, IndexError:
            raise WeComAppMailCallbackError("encryption_invalid") from None
        return content, receiver


def _closed_xml(value: bytes, *, expected: frozenset[str]) -> dict[str, str]:
    if (
        not isinstance(value, bytes)
        or not value
        or len(value) > 65_536
        or b"<!DOCTYPE" in value.upper()
        or b"<!ENTITY" in value.upper()
    ):
        raise WeComAppMailCallbackError("xml_invalid")
    try:
        root = ElementTree.fromstring(value)
    except ElementTree.ParseError, UnicodeDecodeError:
        raise WeComAppMailCallbackError("xml_invalid") from None
    if root.tag != "xml" or root.attrib or root.text not in (None, ""):
        raise WeComAppMailCallbackError("xml_invalid")
    fields: dict[str, str] = {}
    for child in root:
        if (
            child.tag not in expected
            or child.tag in fields
            or child.attrib
            or len(child)
            or child.tail not in (None, "")
            or child.text is None
            or not 1 <= len(child.text.encode("utf-8")) <= 65_536
            or "\x00" in child.text
        ):
            raise WeComAppMailCallbackError("xml_invalid")
        fields[child.tag] = child.text
    if set(fields) != expected:
        raise WeComAppMailCallbackError("xml_invalid")
    return fields


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _require_aware(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("callback clock must be timezone-aware")


__all__ = [
    "VerifiedWeComAppMailSignal",
    "WeComAppMailCallbackError",
    "WeComAppMailCallbackVerifier",
]
