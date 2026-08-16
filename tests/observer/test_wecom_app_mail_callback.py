from __future__ import annotations

import base64
import hashlib
import re
import struct
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from observer.connectors.wecom_app_mail_callback import (
    WeComAppMailCallbackError,
    WeComAppMailCallbackVerifier,
)

NOW = datetime(2026, 8, 14, 8, tzinfo=UTC)
TOKEN = "SyntheticCallbackToken"
KEY_BYTES = bytes(range(32))
AES_KEY = base64.b64encode(KEY_BYTES).decode().rstrip("=")
CORP_ID = "synthetic-corp"
AGENT_ID = "1000001"
EVENT_XML = (
    "<xml><ToUserName><![CDATA[synthetic-app-mail]]></ToUserName>"
    "<FromUserName><![CDATA[sys]]></FromUserName>"
    f"<CreateTime><![CDATA[{int(NOW.timestamp())}]]></CreateTime>"
    "<MsgType><![CDATA[event]]></MsgType>"
    "<Event><![CDATA[app_email_change]]></Event>"
    "<ChangeType><![CDATA[receive_email]]></ChangeType>"
    "<Amount><![CDATA[2]]></Amount></xml>"
)


def _encrypt(plaintext: str, *, receiver: str = CORP_ID, malformed_padding: bool = False) -> str:
    content = plaintext.encode()
    framed = b"0123456789abcdef" + struct.pack(">I", len(content)) + content + receiver.encode()
    pad = 32 - (len(framed) % 32)
    padded = framed + bytes([pad]) * pad
    if malformed_padding:
        padded = padded[:-1] + b"\x00"
    encryptor = Cipher(algorithms.AES(KEY_BYTES), modes.CBC(KEY_BYTES[:16])).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode()


def _signature(encrypted: str, *, timestamp: str, nonce: str, token: str = TOKEN) -> str:
    joined = "".join(sorted((token, timestamp, nonce, encrypted)))
    return hashlib.sha1(joined.encode()).hexdigest()  # noqa: S324 - official WeCom contract


def _query(
    encrypted: str,
    *,
    when: datetime = NOW,
    nonce: str = "synthetic-nonce",
) -> dict[str, str]:
    timestamp = str(int(when.timestamp()))
    return {
        "msg_signature": _signature(encrypted, timestamp=timestamp, nonce=nonce),
        "timestamp": timestamp,
        "nonce": nonce,
    }


def _body(encrypted: str, *, corp_id: str = CORP_ID, agent_id: str = AGENT_ID) -> bytes:
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{corp_id}]]></ToUserName>"
        f"<AgentID><![CDATA[{agent_id}]]></AgentID>"
        f"<Encrypt><![CDATA[{encrypted}]]></Encrypt>"
        "</xml>"
    ).encode()


def _verifier() -> WeComAppMailCallbackVerifier:
    return WeComAppMailCallbackVerifier(
        callback_token=TOKEN,
        encoding_aes_key=AES_KEY,
        corp_id=CORP_ID,
        agent_id=AGENT_ID,
        timestamp_tolerance=timedelta(minutes=5),
    )


def test_url_challenge_uses_official_signature_and_returns_raw_plaintext() -> None:
    encrypted = _encrypt("synthetic-url-verification")
    query = {**_query(encrypted), "echostr": encrypted}

    assert _verifier().verify_challenge(query=query, now=NOW) == "synthetic-url-verification"


def test_encrypted_receive_event_is_a_content_minimized_count_hint() -> None:
    encrypted = _encrypt(EVENT_XML)
    verified = _verifier().verify_event(query=_query(encrypted), body=_body(encrypted), now=NOW)

    assert verified.count_hint == 2
    assert verified.callback_timestamp == NOW
    assert verified.signal_kind == "callback"
    assert verified.payload_digest.startswith("sha256:")
    assert verified.nonce_digest.startswith("sha256:")
    assert verified.replay_key_digest.startswith("sha256:")
    rendered = repr(verified)
    for forbidden in (EVENT_XML, encrypted, "synthetic-nonce", TOKEN, AES_KEY):
        assert forbidden not in rendered
    assert not hasattr(verified, "mail_id")
    assert not hasattr(verified, "cursor")
    assert not hasattr(verified, "delivery_id")


def test_pretty_printed_xml_whitespace_is_allowed_without_opening_the_field_set() -> None:
    pretty_event = re.sub(r"(</[^>]+>)(<[^/])", r"\1\n  \2", EVENT_XML).replace(
        "</xml>", "\n</xml>"
    )
    encrypted = _encrypt(pretty_event)
    pretty_body = re.sub(
        r"(</[^>]+>)(<[^/])",
        r"\1\n  \2",
        _body(encrypted).decode(),
    ).replace("</xml>", "\n</xml>")

    verified = _verifier().verify_event(
        query=_query(encrypted),
        body=pretty_body.encode(),
        now=NOW,
    )

    assert verified.count_hint == 2


def test_query_and_event_times_are_independently_bounded_not_required_equal() -> None:
    query_time = NOW + timedelta(seconds=30)
    encrypted = _encrypt(EVENT_XML)

    verified = _verifier().verify_event(
        query=_query(encrypted, when=query_time),
        body=_body(encrypted),
        now=query_time,
    )
    assert verified.callback_timestamp == query_time

    stale_event = EVENT_XML.replace(
        str(int(NOW.timestamp())),
        str(int((NOW - timedelta(minutes=6)).timestamp())),
    )
    stale_encrypted = _encrypt(stale_event)
    with pytest.raises(WeComAppMailCallbackError) as caught:
        _verifier().verify_event(
            query=_query(stale_encrypted),
            body=_body(stale_encrypted),
            now=NOW,
        )
    assert caught.value.code == "timestamp_invalid"


@pytest.mark.parametrize(
    ("query_change", "body_change", "expected_code"),
    [
        ({"msg_signature": "0" * 40}, {}, "signature_invalid"),
        (
            {"timestamp": str(int((NOW - timedelta(minutes=6)).timestamp()))},
            {},
            "timestamp_invalid",
        ),
        (
            {"timestamp": str(int((NOW + timedelta(minutes=6)).timestamp()))},
            {},
            "timestamp_invalid",
        ),
        ({}, {"corp_id": "wrong-corp"}, "receiver_invalid"),
        ({}, {"agent_id": "1000002"}, "agent_invalid"),
    ],
)
def test_callback_rejects_wrong_signature_time_and_receiver_bindings(
    query_change: dict[str, str],
    body_change: dict[str, str],
    expected_code: str,
) -> None:
    encrypted = _encrypt(EVENT_XML)
    query = _query(encrypted)
    query.update(query_change)
    if "timestamp" in query_change and "msg_signature" not in query_change:
        query["msg_signature"] = _signature(
            encrypted,
            timestamp=query["timestamp"],
            nonce=query["nonce"],
        )

    with pytest.raises(WeComAppMailCallbackError) as caught:
        _verifier().verify_event(query=query, body=_body(encrypted, **body_change), now=NOW)

    assert caught.value.code == expected_code


@pytest.mark.parametrize(
    ("event_xml", "expected_code"),
    [
        (EVENT_XML.replace("receive_email", "unknown_change"), "event_invalid"),
        (EVENT_XML.replace("</xml>", "<mail_id>invented</mail_id></xml>"), "xml_invalid"),
        (EVENT_XML.replace("<Amount><![CDATA[2]]></Amount>", ""), "xml_invalid"),
        (
            EVENT_XML.replace(
                "<Amount><![CDATA[2]]></Amount>",
                "<Amount><![CDATA[2]]></Amount><Amount><![CDATA[2]]></Amount>",
            ),
            "xml_invalid",
        ),
        ("<!DOCTYPE xml [<!ENTITY xxe SYSTEM 'file:///etc/passwd'>]>" + EVENT_XML, "xml_invalid"),
    ],
)
def test_callback_xml_parser_is_closed(event_xml: str, expected_code: str) -> None:
    encrypted = _encrypt(event_xml)

    with pytest.raises(WeComAppMailCallbackError) as caught:
        _verifier().verify_event(query=_query(encrypted), body=_body(encrypted), now=NOW)

    assert caught.value.code == expected_code


def test_callback_rejects_invalid_base64_padding_encoding_and_oversized_inputs() -> None:
    malformed = _encrypt(EVENT_XML, malformed_padding=True)
    cases = (
        (_query(malformed), _body(malformed), "encryption_invalid"),
        (
            {
                "msg_signature": "0" * 40,
                "timestamp": str(int(NOW.timestamp())),
                "nonce": "n" * 129,
            },
            b"<xml/>",
            "query_invalid",
        ),
        (_query("not-base64!"), _body("not-base64!"), "encryption_invalid"),
        (_query(malformed), b"x" * 65_537, "payload_too_large"),
        (_query(malformed), b"\xff\xfe", "xml_invalid"),
    )

    for query, body, expected in cases:
        with pytest.raises(WeComAppMailCallbackError) as caught:
            _verifier().verify_event(query=query, body=body, now=NOW)
        assert caught.value.code == expected


def test_callback_errors_and_verifier_repr_never_expose_secret_or_plaintext() -> None:
    encrypted = _encrypt(EVENT_XML)
    verifier = _verifier()
    with pytest.raises(WeComAppMailCallbackError) as caught:
        verifier.verify_event(
            query={**_query(encrypted), "msg_signature": "0" * 40},
            body=_body(encrypted),
            now=NOW,
        )

    rendered = repr(verifier) + repr(caught.value) + str(caught.value)
    for forbidden in (TOKEN, AES_KEY, EVENT_XML, encrypted):
        assert forbidden not in rendered
