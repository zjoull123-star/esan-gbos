from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Literal, Never, Protocol

from ..local_pilot_ingestion import DeliveryQuarantine
from ..models import ConnectorItem, RawDelivery

_CHALLENGE = re.compile(r"^[0-9]{1,512}$")
_SIGNATURE = re.compile(r"^sha256=([0-9A-Fa-f]{64})$")
_IDENTIFIER = re.compile(r"^[^\x00-\x20\x7f]{1,512}$")
_SHA256 = re.compile(r"^[0-9A-Fa-f]{64}$")

_ROOT_KEYS = frozenset({"object", "entry"})
_ENTRY_KEYS = frozenset({"id", "changes"})
_CHANGE_KEYS = frozenset({"field", "value"})
_VALUE_KEYS = frozenset(
    {
        "messaging_product",
        "metadata",
        "contacts",
        "messages",
        "statuses",
        "errors",
    }
)
_METADATA_KEYS = frozenset({"display_phone_number", "phone_number_id"})
_CONTACT_KEYS = frozenset({"profile", "wa_id"})
_PROFILE_KEYS = frozenset({"name"})
_STATUS_KEYS = frozenset(
    {
        "id",
        "status",
        "timestamp",
        "recipient_id",
        "conversation",
        "pricing",
        "errors",
        "biz_opaque_callback_data",
    }
)
_MESSAGE_COMMON_KEYS = frozenset(
    {
        "from",
        "id",
        "timestamp",
        "type",
        "context",
        "identity",
        "referral",
        "errors",
    }
)
_MEDIA_TYPES = frozenset({"image", "audio", "video", "document", "sticker"})
_MESSAGE_TYPES = frozenset({"text"}) | _MEDIA_TYPES
_MEDIA_KEYS = frozenset(
    {
        "id",
        "mime_type",
        "sha256",
        "caption",
        "filename",
        "voice",
        "animated",
    }
)


class WhatsAppCloudRequestError(ValueError):
    """A safe, public-facing webhook rejection."""

    def __init__(self, *, status_code: int, reason_code: str) -> None:
        super().__init__(reason_code)
        self.status_code = status_code
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class WebhookResponse:
    status_code: int
    content_type: str
    body: bytes


class ReplayGuard(Protocol):
    """Caller-owned durable nonce/age boundary."""

    def claim(self, delivery_id: str, received_at: datetime) -> str: ...


DurableAccept = Callable[[RawDelivery], str]


class DurableDeliveryConflict(RuntimeError):
    """A delivery identifier was already bound to different exact bytes."""


class WhatsAppCloudQuarantineError(DeliveryQuarantine):
    """An authenticated webhook has an unsafe or unsupported envelope."""


class _DuplicateJSONKey(ValueError):
    pass


class _InvalidJSONConstant(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MediaDownloadTask:
    """A credential-free description for a separate read-only media worker."""

    provider_event_id: str
    media_id: str
    media_type: str
    sha256: str | None
    retry_key: str


@dataclass(frozen=True, slots=True)
class WhatsAppRuntimeMetadata:
    """Provider runtime signals which must never be interpreted as business facts."""

    statuses: tuple[Mapping[str, object], ...]
    provider_errors: tuple[object, ...]
    provider_metadata: tuple[Mapping[str, object], ...]


@dataclass(frozen=True, slots=True)
class DecodedWhatsAppCloudDelivery:
    items: tuple[ConnectorItem, ...]
    runtime_metadata: WhatsAppRuntimeMetadata
    media_download_tasks: tuple[MediaDownloadTask, ...]
    raw_contacts: tuple[Mapping[str, object], ...]


@dataclass(frozen=True, slots=True)
class WebhookReceiveResult:
    """Immediate provider ACK outcome after a successful durable handoff."""

    status_code: int
    disposition: Literal["accepted", "duplicate"]
    work_created: bool


def verify_webhook_challenge(
    *,
    mode: str,
    supplied_token: str,
    challenge: str,
    expected_token: str,
) -> WebhookResponse:
    """Verify Meta's GET handshake without normalizing its challenge."""

    valid_types = all(
        isinstance(value, str) for value in (mode, supplied_token, challenge, expected_token)
    )
    token_matches = valid_types and hmac.compare_digest(
        supplied_token.encode("utf-8"),
        expected_token.encode("utf-8"),
    )
    if (
        mode != "subscribe"
        or not token_matches
        or not _CHALLENGE.fullmatch(challenge)
        or not expected_token
    ):
        raise WhatsAppCloudRequestError(
            status_code=403,
            reason_code="verification_failed",
        )
    return WebhookResponse(
        status_code=200,
        content_type="text/plain; charset=utf-8",
        body=challenge.encode("ascii"),
    )


class WhatsAppCloudDeliveryAuthenticator:
    """Authenticate the exact callback bytes before they enter durable storage."""

    __slots__ = ("_app_secret", "_max_body_bytes")

    def __init__(
        self,
        *,
        app_secret: str,
        max_body_bytes: int = 1_048_576,
    ) -> None:
        if (
            not isinstance(app_secret, str)
            or not app_secret
            or len(app_secret.encode("utf-8")) > 4096
        ):
            raise ValueError("invalid app secret configuration")
        if not isinstance(max_body_bytes, int) or not 1 <= max_body_bytes <= 16_777_216:
            raise ValueError("invalid body size boundary")
        self._app_secret = app_secret.encode("utf-8")
        self._max_body_bytes = max_body_bytes

    def authenticate(
        self,
        *,
        exact_body: bytes,
        signature_header: str | None,
        delivery_id: str,
        received_at: datetime,
    ) -> RawDelivery:
        if not isinstance(exact_body, bytes):
            raise WhatsAppCloudRequestError(status_code=400, reason_code="invalid_request")
        if len(exact_body) > self._max_body_bytes:
            raise WhatsAppCloudRequestError(
                status_code=413,
                reason_code="payload_too_large",
            )
        if (
            not isinstance(signature_header, str)
            or (match := _SIGNATURE.fullmatch(signature_header)) is None
        ):
            raise WhatsAppCloudRequestError(
                status_code=401,
                reason_code="authentication_failed",
            )
        expected = hmac.new(self._app_secret, exact_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(match.group(1).lower(), expected):
            raise WhatsAppCloudRequestError(
                status_code=401,
                reason_code="authentication_failed",
            )
        if (
            not isinstance(delivery_id, str)
            or not _IDENTIFIER.fullmatch(delivery_id)
            or not isinstance(received_at, datetime)
            or received_at.tzinfo is None
            or received_at.utcoffset() is None
        ):
            raise WhatsAppCloudRequestError(status_code=400, reason_code="invalid_request")

        return RawDelivery(
            delivery_id=delivery_id,
            exact_bytes=exact_body,
            media_type="application/json",
            received_at=received_at,
        )


class WhatsAppCloudDurableReceiver:
    """Order the authenticated handoff as durable accept, then replay accounting."""

    __slots__ = ("_authenticator", "_durable_accept", "_replay_guard")

    def __init__(
        self,
        *,
        authenticator: WhatsAppCloudDeliveryAuthenticator,
        durable_accept: DurableAccept,
        replay_guard: ReplayGuard,
    ) -> None:
        if not isinstance(authenticator, WhatsAppCloudDeliveryAuthenticator):
            raise TypeError("invalid authenticator")
        if not callable(durable_accept):
            raise TypeError("durable_accept must be callable")
        if not callable(getattr(replay_guard, "claim", None)):
            raise TypeError("replay_guard must provide claim")
        self._authenticator = authenticator
        self._durable_accept = durable_accept
        self._replay_guard = replay_guard

    def receive(
        self,
        *,
        exact_body: bytes,
        signature_header: str | None,
        delivery_id: str,
        received_at: datetime,
    ) -> WebhookReceiveResult:
        delivery = self._authenticator.authenticate(
            exact_body=exact_body,
            signature_header=signature_header,
            delivery_id=delivery_id,
            received_at=received_at,
        )
        try:
            disposition = self._durable_accept(delivery)
        except DurableDeliveryConflict:
            raise WhatsAppCloudRequestError(
                status_code=409,
                reason_code="delivery_conflict",
            ) from None
        except Exception:
            raise WhatsAppCloudRequestError(
                status_code=503,
                reason_code="durable_accept_failed",
            ) from None
        if disposition not in {"accepted", "duplicate"}:
            raise WhatsAppCloudRequestError(
                status_code=503,
                reason_code="durable_accept_failed",
            )

        try:
            replay_decision = self._replay_guard.claim(
                delivery.delivery_id,
                delivery.received_at,
            )
        except Exception:
            raise WhatsAppCloudRequestError(
                status_code=503,
                reason_code="replay_check_failed",
            ) from None
        if replay_decision not in {"accepted", "replay", "expired"}:
            raise WhatsAppCloudRequestError(
                status_code=503,
                reason_code="replay_check_failed",
            )

        if disposition == "duplicate":
            return WebhookReceiveResult(
                status_code=200,
                disposition="duplicate",
                work_created=False,
            )
        if replay_decision == "replay":
            raise WhatsAppCloudRequestError(status_code=409, reason_code="replay_rejected")
        if replay_decision == "expired":
            raise WhatsAppCloudRequestError(status_code=408, reason_code="delivery_expired")
        return WebhookReceiveResult(
            status_code=200,
            disposition="accepted",
            work_created=True,
        )


class WhatsAppCloudWebhookDecoder:
    """Decode an authenticated, durably stored Meta webhook without side effects."""

    __slots__ = (
        "_max_body_bytes",
        "_max_changes",
        "_max_depth",
        "_max_entries",
        "_max_messages",
        "_max_nodes",
        "_max_records",
        "_max_string_bytes",
    )

    def __init__(
        self,
        *,
        max_body_bytes: int = 1_048_576,
        max_entries: int = 100,
        max_changes: int = 100,
        max_messages: int = 100,
        max_records: int = 100,
        max_depth: int = 20,
        max_nodes: int = 20_000,
        max_string_bytes: int = 65_536,
    ) -> None:
        boundaries = (
            max_body_bytes,
            max_entries,
            max_changes,
            max_messages,
            max_records,
            max_depth,
            max_nodes,
            max_string_bytes,
        )
        if any(not isinstance(value, int) or value < 1 for value in boundaries):
            raise ValueError("decoder boundaries must be positive integers")
        self._max_body_bytes = max_body_bytes
        self._max_entries = max_entries
        self._max_changes = max_changes
        self._max_messages = max_messages
        self._max_records = max_records
        self._max_depth = max_depth
        self._max_nodes = max_nodes
        self._max_string_bytes = max_string_bytes

    def decode(self, exact_bytes: bytes) -> tuple[ConnectorItem, ...]:
        return self.decode_delivery(exact_bytes).items

    def decode_delivery(self, exact_bytes: bytes) -> DecodedWhatsAppCloudDelivery:
        root = self._parse(exact_bytes)
        self._require_keys(root, _ROOT_KEYS, "invalid_root")
        if root.get("object") != "whatsapp_business_account":
            self._quarantine("unsupported_object")
        entries = self._require_list(root.get("entry"), "invalid_entries", self._max_entries)

        items: list[ConnectorItem] = []
        media_tasks: list[MediaDownloadTask] = []
        statuses: list[Mapping[str, object]] = []
        provider_errors: list[object] = []
        provider_metadata: list[Mapping[str, object]] = []
        raw_contacts: list[Mapping[str, object]] = []
        seen_wamids: set[str] = set()
        message_count = 0

        for entry_value in entries:
            entry = self._require_mapping(entry_value, "invalid_entry")
            self._require_keys(entry, _ENTRY_KEYS, "invalid_entry")
            self._require_identifier(entry.get("id"), "invalid_entry")
            changes = self._require_list(
                entry.get("changes"),
                "invalid_changes",
                self._max_changes,
            )
            for change_value in changes:
                change = self._require_mapping(change_value, "invalid_change")
                self._require_keys(change, _CHANGE_KEYS, "invalid_change")
                if change.get("field") != "messages":
                    self._quarantine("unsupported_change")
                value = self._require_mapping(change.get("value"), "invalid_value")
                self._require_keys(value, _VALUE_KEYS, "invalid_value")
                if value.get("messaging_product") != "whatsapp":
                    self._quarantine("invalid_messaging_product")

                metadata = self._validate_metadata(value.get("metadata"))
                provider_metadata.append(metadata)
                contacts = self._validate_contacts(value.get("contacts", []))
                raw_contacts.extend(contacts)
                change_statuses = self._validate_statuses(value.get("statuses", []))
                statuses.extend(change_statuses)
                errors = self._require_list(
                    value.get("errors", []),
                    "invalid_provider_errors",
                    self._max_records,
                )
                provider_errors.extend(_freeze(error) for error in errors)
                messages = self._require_list(
                    value.get("messages", []),
                    "invalid_messages",
                    self._max_messages,
                )
                message_count += len(messages)
                if message_count > self._max_messages:
                    self._quarantine("too_many_messages")

                frozen_contacts = tuple(contacts)
                for message_value in messages:
                    message = self._validate_message(message_value)
                    wamid = self._require_identifier(
                        message.get("id"),
                        "invalid_message_id",
                    )
                    if wamid in seen_wamids:
                        continue
                    seen_wamids.add(wamid)
                    occurred_at = self._parse_timestamp(message.get("timestamp"))
                    media_task = self._media_task(message, wamid)
                    if media_task is not None:
                        media_tasks.append(media_task)
                    payload: dict[str, object] = {
                        "kind": "whatsapp_message",
                        "message": _freeze(message),
                        "raw_contacts": frozen_contacts,
                        "runtime_metadata": metadata,
                    }
                    if media_task is not None:
                        payload["media_download_task"] = media_task
                    items.append(
                        ConnectorItem(
                            provider_event_id=wamid,
                            occurred_at=occurred_at,
                            source_cursor=wamid,
                            payload=payload,
                        )
                    )

        return DecodedWhatsAppCloudDelivery(
            items=tuple(items),
            runtime_metadata=WhatsAppRuntimeMetadata(
                statuses=tuple(statuses),
                provider_errors=tuple(provider_errors),
                provider_metadata=tuple(provider_metadata),
            ),
            media_download_tasks=tuple(media_tasks),
            raw_contacts=tuple(raw_contacts),
        )

    def _parse(self, exact_bytes: bytes) -> Mapping[str, object]:
        if not isinstance(exact_bytes, bytes):
            self._quarantine("invalid_body")
        if len(exact_bytes) > self._max_body_bytes:
            self._quarantine("payload_too_large")
        try:
            text = exact_bytes.decode("utf-8", errors="strict")
            value = json.loads(
                text,
                object_pairs_hook=_strict_json_object,
                parse_constant=_reject_json_constant,
            )
        except _DuplicateJSONKey:
            self._quarantine("duplicate_json_key")
        except UnicodeDecodeError, json.JSONDecodeError, _InvalidJSONConstant:
            self._quarantine("invalid_json")
        self._validate_tree(value)
        return self._require_mapping(value, "invalid_root")

    def _validate_tree(self, root: object) -> None:
        stack: list[tuple[object, int]] = [(root, 1)]
        nodes = 0
        while stack:
            value, depth = stack.pop()
            nodes += 1
            if nodes > self._max_nodes:
                self._quarantine("payload_too_complex")
            if depth > self._max_depth:
                self._quarantine("payload_too_deep")
            if isinstance(value, str):
                if len(value.encode("utf-8")) > self._max_string_bytes:
                    self._quarantine("string_too_large")
            elif isinstance(value, Mapping):
                for key, child in value.items():
                    if not isinstance(key, str):
                        self._quarantine("invalid_object_key")
                    stack.append((child, depth + 1))
            elif isinstance(value, list):
                stack.extend((child, depth + 1) for child in value)
            elif isinstance(value, float) and not math.isfinite(value):
                self._quarantine("invalid_json_number")
            elif value is not None and not isinstance(value, (bool, int, float)):
                self._quarantine("invalid_json_value")

    def _validate_metadata(self, value: object) -> Mapping[str, object]:
        metadata = self._require_mapping(value, "invalid_metadata")
        self._require_keys(metadata, _METADATA_KEYS, "invalid_metadata")
        phone_number_id = metadata.get("phone_number_id")
        self._require_identifier(phone_number_id, "invalid_metadata")
        display = metadata.get("display_phone_number")
        if display is not None:
            self._require_identifier(display, "invalid_metadata")
        return _freeze_mapping(metadata)

    def _validate_contacts(self, value: object) -> tuple[Mapping[str, object], ...]:
        contacts = self._require_list(value, "invalid_contacts", self._max_records)
        validated: list[Mapping[str, object]] = []
        for contact_value in contacts:
            contact = self._require_mapping(contact_value, "invalid_contact")
            self._require_keys(contact, _CONTACT_KEYS, "invalid_contact")
            self._require_identifier(contact.get("wa_id"), "invalid_contact")
            profile = self._require_mapping(contact.get("profile"), "invalid_contact")
            self._require_keys(profile, _PROFILE_KEYS, "invalid_contact")
            self._require_text(profile.get("name"), "invalid_contact")
            validated.append(_freeze_mapping(contact))
        return tuple(validated)

    def _validate_statuses(self, value: object) -> tuple[Mapping[str, object], ...]:
        status_values = self._require_list(value, "invalid_statuses", self._max_records)
        statuses: list[Mapping[str, object]] = []
        for status_value in status_values:
            status = self._require_mapping(status_value, "invalid_status")
            self._require_keys(status, _STATUS_KEYS, "invalid_status")
            self._require_identifier(status.get("id"), "invalid_status")
            self._require_identifier(status.get("status"), "invalid_status")
            self._parse_timestamp(status.get("timestamp"))
            recipient = status.get("recipient_id")
            if recipient is not None:
                self._require_identifier(recipient, "invalid_status")
            statuses.append(_freeze_mapping(status))
        return tuple(statuses)

    def _validate_message(self, value: object) -> Mapping[str, object]:
        message = self._require_mapping(value, "invalid_message")
        message_type = message.get("type")
        if not isinstance(message_type, str) or message_type not in _MESSAGE_TYPES:
            self._quarantine("unsupported_message_type")
        self._require_keys(
            message,
            _MESSAGE_COMMON_KEYS | {message_type},
            "invalid_message",
        )
        self._require_identifier(message.get("id"), "invalid_message_id")
        sender = message.get("from")
        if sender is not None:
            self._require_identifier(sender, "invalid_message")
        self._parse_timestamp(message.get("timestamp"))
        content = self._require_mapping(message.get(message_type), "invalid_message_content")
        if message_type == "text":
            self._require_keys(content, frozenset({"body"}), "invalid_text_message")
            body = content.get("body")
            if not isinstance(body, str):
                self._quarantine("invalid_text_message")
        elif message_type in _MEDIA_TYPES:
            self._require_keys(content, _MEDIA_KEYS, "invalid_media_message")
            self._require_identifier(content.get("id"), "invalid_media_message")
            self._require_identifier(content.get("mime_type"), "invalid_media_message")
            digest = content.get("sha256")
            if digest is not None and (
                not isinstance(digest, str) or not _SHA256.fullmatch(digest)
            ):
                self._quarantine("invalid_media_message")
        return message

    def _media_task(
        self,
        message: Mapping[str, object],
        wamid: str,
    ) -> MediaDownloadTask | None:
        message_type = message["type"]
        if message_type not in _MEDIA_TYPES:
            return None
        media = self._require_mapping(message[message_type], "invalid_media_message")
        media_id = self._require_identifier(media.get("id"), "invalid_media_message")
        media_type = self._require_identifier(media.get("mime_type"), "invalid_media_message")
        digest = media.get("sha256")
        return MediaDownloadTask(
            provider_event_id=wamid,
            media_id=media_id,
            media_type=media_type,
            sha256=digest if isinstance(digest, str) else None,
            retry_key=f"whatsapp-media:{media_id}",
        )

    @staticmethod
    def _parse_timestamp(value: object) -> datetime:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,10}", value):
            WhatsAppCloudWebhookDecoder._quarantine("invalid_timestamp")
        try:
            return datetime.fromtimestamp(int(value), tz=UTC)
        except OverflowError, OSError, ValueError:
            WhatsAppCloudWebhookDecoder._quarantine("invalid_timestamp")

    @staticmethod
    def _require_mapping(value: object, reason: str) -> Mapping[str, object]:
        if not isinstance(value, Mapping):
            WhatsAppCloudWebhookDecoder._quarantine(reason)
        return value

    @staticmethod
    def _require_list(value: object, reason: str, maximum: int) -> list[object]:
        if not isinstance(value, list):
            WhatsAppCloudWebhookDecoder._quarantine(reason)
        if len(value) > maximum:
            if reason == "invalid_messages":
                WhatsAppCloudWebhookDecoder._quarantine("too_many_messages")
            WhatsAppCloudWebhookDecoder._quarantine(reason)
        return value

    @staticmethod
    def _require_keys(
        value: Mapping[str, object],
        allowed: frozenset[str],
        reason: str,
    ) -> None:
        if not set(value).issubset(allowed):
            WhatsAppCloudWebhookDecoder._quarantine(reason)

    @staticmethod
    def _require_identifier(value: object, reason: str) -> str:
        if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
            WhatsAppCloudWebhookDecoder._quarantine(reason)
        return value

    @staticmethod
    def _require_text(value: object, reason: str) -> str:
        if not isinstance(value, str) or not value or "\x00" in value:
            WhatsAppCloudWebhookDecoder._quarantine(reason)
        return value

    @staticmethod
    def _quarantine(reason: str) -> Never:
        raise WhatsAppCloudQuarantineError(reason)


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType({key: _freeze(child) for key, child in value.items()})


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, list):
        return tuple(_freeze(child) for child in value)
    return value


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, child in pairs:
        if key in value:
            raise _DuplicateJSONKey
        value[key] = child
    return value


def _reject_json_constant(_value: str) -> Never:
    raise _InvalidJSONConstant
