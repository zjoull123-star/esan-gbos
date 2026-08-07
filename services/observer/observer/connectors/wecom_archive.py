"""Fail-closed boundary for the official WeCom message-archive SDK.

This module deliberately contains no SDK loader, HTTP client, credential model,
checkpoint store, or container launcher.  An official-SDK-compatible object is
injected by the composition root.  Callers durably accept ``deliveries`` before
advancing to ``next_checkpoint``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from ..models import ConnectorItem, RawDelivery

_MAX_PROVIDER_BATCH_SIZE = 1000
_MEDIA_MESSAGE_TYPES = frozenset({"image", "voice", "video", "emotion", "file"})


class ArchiveDisposition(StrEnum):
    """Required caller action for a non-deliverable archive stage."""

    RETRY = "retry"
    PAUSE = "pause"
    QUARANTINE = "quarantine"
    PRESERVE_ONLY = "preserve_only"


class SdkFetchStatus(StrEnum):
    """Closed, safe statuses exposed by the injected official SDK boundary."""

    OK = "ok"
    PERMISSION_DENIED = "permission_denied"
    ARCHIVE_NOT_AUTHORIZED = "archive_not_authorized"
    MEMBER_ARCHIVE_DISABLED = "member_archive_disabled"


_FETCH_STATUS_ERRORS: Mapping[SdkFetchStatus, str] = {
    SdkFetchStatus.PERMISSION_DENIED: "wecom_archive.permission_denied",
    SdkFetchStatus.ARCHIVE_NOT_AUTHORIZED: "wecom_archive.archive_not_authorized",
    SdkFetchStatus.MEMBER_ARCHIVE_DISABLED: "wecom_archive.member_archive_disabled",
}


class WeComArchiveError(RuntimeError):
    """A safe stage failure which never includes SDK output or encrypted content."""

    __slots__ = ("code", "disposition")

    def __init__(self, code: str, disposition: ArchiveDisposition) -> None:
        if not code.startswith("wecom_archive."):
            raise ValueError("invalid WeCom archive error code")
        self.code = code
        self.disposition = disposition
        super().__init__(code)

    def __str__(self) -> str:
        return self.code

    def __repr__(self) -> str:
        return f"WeComArchiveError(code={self.code!r}, disposition={self.disposition.value!r})"


def _require_bytes(value: bytes, field_name: str) -> None:
    if not isinstance(value, bytes):
        raise TypeError(f"{field_name} must be bytes")


def _redacted_bytes(value: bytes) -> str:
    return f"<redacted bytes={len(value)}>"


@dataclass(frozen=True, slots=True, repr=False)
class EncryptedEnvelope:
    """Exact provider envelope plus the opaque inputs required by official decryption."""

    seq: int
    exact_bytes: bytes
    encrypt_random_key: bytes
    encrypt_chat_msg: bytes

    def __post_init__(self) -> None:
        if isinstance(self.seq, bool) or self.seq <= 0:
            raise ValueError("seq must be a positive integer")
        _require_bytes(self.exact_bytes, "exact_bytes")
        _require_bytes(self.encrypt_random_key, "encrypt_random_key")
        _require_bytes(self.encrypt_chat_msg, "encrypt_chat_msg")

    def __repr__(self) -> str:
        return (
            f"EncryptedEnvelope(seq={self.seq}, "
            f"exact_bytes={_redacted_bytes(self.exact_bytes)}, "
            "encrypt_random_key=<redacted>, encrypt_chat_msg=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class SdkFetchPage:
    """Value returned by an official-SDK-compatible wrapper."""

    status: SdkFetchStatus
    envelopes: tuple[EncryptedEnvelope, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.status, SdkFetchStatus):
            raise TypeError("status must be SdkFetchStatus")
        if not isinstance(self.envelopes, tuple) or not all(
            isinstance(envelope, EncryptedEnvelope) for envelope in self.envelopes
        ):
            raise TypeError("envelopes must contain EncryptedEnvelope values")
        if self.status is not SdkFetchStatus.OK and self.envelopes:
            raise ValueError("non-ok SDK page cannot include envelopes")

    @classmethod
    def ok(cls, envelopes: Sequence[EncryptedEnvelope]) -> SdkFetchPage:
        return cls(status=SdkFetchStatus.OK, envelopes=tuple(envelopes))


@dataclass(frozen=True, slots=True, repr=False)
class DecryptedMessage:
    """Exact decrypted JSON bytes; persistence remains a caller-owned concern."""

    seq: int
    exact_bytes: bytes

    def __post_init__(self) -> None:
        if isinstance(self.seq, bool) or self.seq <= 0:
            raise ValueError("seq must be a positive integer")
        _require_bytes(self.exact_bytes, "exact_bytes")

    def __repr__(self) -> str:
        return f"DecryptedMessage(seq={self.seq}, exact_bytes={_redacted_bytes(self.exact_bytes)})"


@dataclass(frozen=True, slots=True, repr=False)
class MediaDownloadRequest:
    """Read-only descriptor for one official SDK media-chunk request."""

    sdk_file_id: str
    cursor: bytes = b""
    read_only: bool = True

    def __post_init__(self) -> None:
        if (
            not isinstance(self.sdk_file_id, str)
            or not self.sdk_file_id
            or len(self.sdk_file_id) > 4096
        ):
            raise ValueError("invalid sdk_file_id")
        _require_bytes(self.cursor, "cursor")
        if self.read_only is not True:
            raise ValueError("media requests must be read-only")

    def __repr__(self) -> str:
        return (
            "MediaDownloadRequest(sdk_file_id=<redacted>, "
            f"cursor={_redacted_bytes(self.cursor)}, read_only=True)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class MediaDownloadChunk:
    """Opaque media bytes; callers must never coerce them to text."""

    content: bytes
    next_cursor: bytes
    complete: bool
    media_type: str = "application/octet-stream"

    def __post_init__(self) -> None:
        _require_bytes(self.content, "content")
        _require_bytes(self.next_cursor, "next_cursor")
        if not isinstance(self.complete, bool):
            raise TypeError("complete must be bool")
        if (
            not isinstance(self.media_type, str)
            or not self.media_type
            or len(self.media_type) > 255
        ):
            raise ValueError("invalid media_type")

    def __repr__(self) -> str:
        return (
            f"MediaDownloadChunk(content={_redacted_bytes(self.content)}, "
            f"next_cursor={_redacted_bytes(self.next_cursor)}, "
            f"complete={self.complete!r}, media_type={self.media_type!r})"
        )


class OfficialWeComArchiveSDK(Protocol):
    """Small seam implemented only by a configured official SDK wrapper."""

    def fetch_chat_data(self, *, seq: int, limit: int) -> SdkFetchPage: ...

    def decrypt_random_key(self, *, encrypt_random_key: bytes) -> bytes: ...

    def decrypt_chat_data(
        self,
        *,
        decrypted_random_key: bytes,
        encrypt_chat_msg: bytes,
    ) -> bytes: ...

    def download_media(self, *, sdk_file_id: str, cursor: bytes) -> MediaDownloadChunk: ...


@dataclass(frozen=True, slots=True)
class WeComArchiveConfig:
    """Non-secret adapter configuration; credentials belong outside this boundary."""

    instance_id: str
    max_batch_size: int = _MAX_PROVIDER_BATCH_SIZE

    def __post_init__(self) -> None:
        if (
            not isinstance(self.instance_id, str)
            or not self.instance_id
            or self.instance_id != self.instance_id.strip()
            or len(self.instance_id) > 256
        ):
            raise ValueError("invalid instance_id")
        if (
            isinstance(self.max_batch_size, bool)
            or not 1 <= self.max_batch_size <= _MAX_PROVIDER_BATCH_SIZE
        ):
            raise ValueError("max_batch_size must be a positive bounded integer")


@dataclass(frozen=True, slots=True, repr=False)
class EncryptedBatch:
    """Fetch result whose checkpoint is only a candidate for caller-side commit."""

    expected_checkpoint: str | None
    next_checkpoint: str | None
    envelopes: tuple[EncryptedEnvelope, ...]
    deliveries: tuple[RawDelivery, ...]

    def __post_init__(self) -> None:
        if len(self.envelopes) != len(self.deliveries):
            raise ValueError("every encrypted envelope requires one raw delivery")

    def __repr__(self) -> str:
        return (
            "EncryptedBatch("
            f"expected_checkpoint={self.expected_checkpoint!r}, "
            f"next_checkpoint={self.next_checkpoint!r}, "
            f"envelopes=<redacted count={len(self.envelopes)}>, "
            f"deliveries=<redacted count={len(self.deliveries)}>)"
        )


@dataclass(frozen=True, slots=True)
class ArchiveControlState:
    """Non-business control state preserved for policy handling."""

    seq: int
    provider_event_id: str
    disposition: ArchiveDisposition
    reason_code: str
    content_ref: str


@dataclass(frozen=True, slots=True)
class NormalizedArchiveBatch:
    """Delivery-ready items plus controls that must not enter the business fact stream."""

    items: tuple[ConnectorItem, ...]
    controls: tuple[ArchiveControlState, ...]
    duplicate_msgids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Amd64IsolationPlan:
    """Declarative fallback only; it cannot launch or download anything."""

    platform: str = "linux/amd64"
    fixed_digest_required: bool = True
    isolated: bool = True
    execution: str = "plan_only"


@dataclass(frozen=True, slots=True)
class SdkPreflight:
    status: str
    selected_runtime: str | None
    error_code: str | None
    container_plan: Amd64IsolationPlan | None


def preflight_official_sdk(
    *,
    system: str,
    machine: str,
    official_linux_arm64_available: bool,
) -> SdkPreflight:
    """Select an available official Linux ARM64 library or emit a blocked plan."""

    normalized_system = system.casefold()
    normalized_machine = machine.casefold()
    is_linux_arm64 = normalized_system == "linux" and normalized_machine in {
        "arm64",
        "aarch64",
    }
    if is_linux_arm64 and official_linux_arm64_available:
        return SdkPreflight(
            status="ready",
            selected_runtime="official-linux-arm64",
            error_code=None,
            container_plan=None,
        )
    return SdkPreflight(
        status="blocked",
        selected_runtime=None,
        error_code="wecom_archive.sdk_architecture_unavailable",
        container_plan=Amd64IsolationPlan(),
    )


class WeComArchiveAdapter:
    """Stateless stage coordinator around an injected official SDK boundary."""

    __slots__ = ("_clock", "_config", "_sdk")

    def __init__(
        self,
        *,
        config: WeComArchiveConfig,
        sdk: OfficialWeComArchiveSDK,
        clock: Callable[[], datetime],
    ) -> None:
        self._config = config
        self._sdk = sdk
        self._clock = clock

    def __repr__(self) -> str:
        return f"WeComArchiveAdapter(instance_id={self._config.instance_id!r}, sdk=<redacted>)"

    def fetch(self, checkpoint: str | None, limit: int) -> EncryptedBatch:
        """Fetch a strictly bounded page without persisting or advancing state."""

        requested_seq = _parse_checkpoint(checkpoint)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= self._config.max_batch_size
        ):
            raise ValueError("limit must be a positive bounded integer")
        try:
            page = self._sdk.fetch_chat_data(seq=requested_seq, limit=limit)
        except Exception:
            raise WeComArchiveError(
                "wecom_archive.fetch_failed",
                ArchiveDisposition.RETRY,
            ) from None
        if not isinstance(page, SdkFetchPage):
            raise WeComArchiveError(
                "wecom_archive.invalid_sdk_result",
                ArchiveDisposition.QUARANTINE,
            )
        if page.status is not SdkFetchStatus.OK:
            raise WeComArchiveError(
                _FETCH_STATUS_ERRORS[page.status],
                ArchiveDisposition.PAUSE,
            )
        if len(page.envelopes) > limit:
            raise WeComArchiveError(
                "wecom_archive.sdk_page_overflow",
                ArchiveDisposition.QUARANTINE,
            )

        envelopes = tuple(sorted(page.envelopes, key=lambda envelope: envelope.seq))
        _validate_sequences(envelopes, requested_seq)
        received_at = self._clock()
        deliveries = tuple(
            RawDelivery(
                delivery_id=(f"wecom-archive-{self._config.instance_id}-seq-{envelope.seq}"),
                exact_bytes=envelope.exact_bytes,
                media_type="application/vnd.wecom.archive.encrypted+json",
                received_at=received_at,
            )
            for envelope in envelopes
        )
        next_checkpoint = str(envelopes[-1].seq) if envelopes else checkpoint
        return EncryptedBatch(
            expected_checkpoint=checkpoint,
            next_checkpoint=next_checkpoint,
            envelopes=envelopes,
            deliveries=deliveries,
        )

    def decrypt(self, envelope: EncryptedEnvelope) -> DecryptedMessage:
        """Decrypt one envelope; a failure can be retried without re-fetching."""

        try:
            decrypted_random_key = self._sdk.decrypt_random_key(
                encrypt_random_key=envelope.encrypt_random_key,
            )
            if not isinstance(decrypted_random_key, bytes):
                raise TypeError("invalid decrypted random key")
            exact_bytes = self._sdk.decrypt_chat_data(
                decrypted_random_key=decrypted_random_key,
                encrypt_chat_msg=envelope.encrypt_chat_msg,
            )
        except Exception:
            raise WeComArchiveError(
                "wecom_archive.decrypt_failed",
                ArchiveDisposition.RETRY,
            ) from None
        if not isinstance(exact_bytes, bytes):
            raise WeComArchiveError(
                "wecom_archive.decrypt_failed",
                ArchiveDisposition.RETRY,
            )
        return DecryptedMessage(seq=envelope.seq, exact_bytes=exact_bytes)

    def normalize_batch(
        self,
        messages: tuple[DecryptedMessage, ...],
        *,
        content_refs: tuple[str, ...],
        seen_msgids: frozenset[str] = frozenset(),
    ) -> NormalizedArchiveBatch:
        """Normalize decrypted metadata without embedding exact content in payloads."""

        if not isinstance(messages, tuple) or not all(
            isinstance(message, DecryptedMessage) for message in messages
        ):
            raise TypeError("messages must contain DecryptedMessage values")
        if not isinstance(content_refs, tuple) or len(content_refs) != len(messages):
            raise ValueError("content_refs must identify every decrypted message")
        if not isinstance(seen_msgids, frozenset) or not all(
            isinstance(msgid, str) for msgid in seen_msgids
        ):
            raise TypeError("seen_msgids must be a frozenset of strings")

        pairs = tuple(
            sorted(
                zip(messages, content_refs, strict=True),
                key=lambda pair: pair[0].seq,
            )
        )
        _validate_decrypted_sequences(tuple(message for message, _ in pairs))
        emitted = set(seen_msgids)
        items: list[ConnectorItem] = []
        controls: list[ArchiveControlState] = []
        duplicates: list[str] = []
        for message, content_ref in pairs:
            _require_content_ref(content_ref)
            payload = _parse_decrypted_json(message.exact_bytes)
            msgid = _required_string(payload, "msgid")
            msgtype = _required_string(payload, "msgtype")
            occurred_at = _message_time(payload)
            if msgid in emitted:
                duplicates.append(msgid)
                continue
            emitted.add(msgid)
            control = _control_state(
                seq=message.seq,
                msgid=msgid,
                msgtype=msgtype,
                action=payload.get("action"),
                content_ref=content_ref,
            )
            if control is not None:
                controls.append(control)
                continue
            items.append(
                ConnectorItem(
                    provider_event_id=msgid,
                    occurred_at=occurred_at,
                    source_cursor=str(message.seq),
                    payload={
                        "message_type": msgtype,
                        "decrypted_content_ref": content_ref,
                        "media_pending": msgtype in _MEDIA_MESSAGE_TYPES,
                    },
                )
            )
        return NormalizedArchiveBatch(
            items=tuple(items),
            controls=tuple(controls),
            duplicate_msgids=tuple(duplicates),
        )

    def describe_media(self, message: DecryptedMessage) -> tuple[MediaDownloadRequest, ...]:
        """Describe read-only media requests without downloading or decoding bytes."""

        payload = _parse_decrypted_json(message.exact_bytes)
        requests: list[MediaDownloadRequest] = []
        for candidate in _walk_mappings(payload):
            sdk_file_id = candidate.get("sdkfileid")
            if isinstance(sdk_file_id, str) and sdk_file_id:
                requests.append(MediaDownloadRequest(sdk_file_id=sdk_file_id))
        return tuple(requests)

    def download_media(self, request: MediaDownloadRequest) -> MediaDownloadChunk:
        """Download one opaque chunk; callers retry the same descriptor on failure."""

        if request.read_only is not True:
            raise ValueError("media requests must be read-only")
        try:
            chunk = self._sdk.download_media(
                sdk_file_id=request.sdk_file_id,
                cursor=request.cursor,
            )
        except Exception:
            raise WeComArchiveError(
                "wecom_archive.media_download_failed",
                ArchiveDisposition.RETRY,
            ) from None
        if not isinstance(chunk, MediaDownloadChunk):
            raise WeComArchiveError(
                "wecom_archive.media_download_failed",
                ArchiveDisposition.RETRY,
            )
        return chunk


def _parse_checkpoint(checkpoint: str | None) -> int:
    if checkpoint is None:
        return 0
    if (
        not isinstance(checkpoint, str)
        or not checkpoint
        or not checkpoint.isascii()
        or not checkpoint.isdecimal()
    ):
        raise ValueError("checkpoint must be a non-negative decimal seq")
    value = int(checkpoint)
    if value < 0:
        raise ValueError("checkpoint must be a non-negative decimal seq")
    return value


def _validate_sequences(
    envelopes: tuple[EncryptedEnvelope, ...],
    requested_seq: int,
) -> None:
    previous = requested_seq
    for envelope in envelopes:
        if envelope.seq <= previous:
            raise WeComArchiveError(
                "wecom_archive.seq_rollback",
                ArchiveDisposition.QUARANTINE,
            )
        if envelope.seq != previous + 1:
            raise WeComArchiveError(
                "wecom_archive.seq_gap",
                ArchiveDisposition.QUARANTINE,
            )
        previous = envelope.seq


def _validate_decrypted_sequences(messages: tuple[DecryptedMessage, ...]) -> None:
    previous: int | None = None
    for message in messages:
        if previous is not None and message.seq <= previous:
            raise WeComArchiveError(
                "wecom_archive.seq_rollback",
                ArchiveDisposition.QUARANTINE,
            )
        previous = message.seq


def _require_content_ref(content_ref: str) -> None:
    if (
        not isinstance(content_ref, str)
        or not content_ref
        or content_ref != content_ref.strip()
        or len(content_ref) > 512
    ):
        raise ValueError("invalid decrypted content reference")


def _parse_decrypted_json(exact_bytes: bytes) -> Mapping[str, object]:
    try:
        value = json.loads(exact_bytes)
    except UnicodeDecodeError, json.JSONDecodeError:
        raise WeComArchiveError(
            "wecom_archive.invalid_decrypted_message",
            ArchiveDisposition.QUARANTINE,
        ) from None
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise WeComArchiveError(
            "wecom_archive.invalid_decrypted_message",
            ArchiveDisposition.QUARANTINE,
        )
    return value


def _required_string(payload: Mapping[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value or len(value) > 512:
        raise WeComArchiveError(
            "wecom_archive.invalid_decrypted_message",
            ArchiveDisposition.QUARANTINE,
        )
    return value


def _message_time(payload: Mapping[str, object]) -> datetime:
    value = payload.get("msgtime")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WeComArchiveError(
            "wecom_archive.invalid_decrypted_message",
            ArchiveDisposition.QUARANTINE,
        )
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except OverflowError, OSError, ValueError:
        raise WeComArchiveError(
            "wecom_archive.invalid_decrypted_message",
            ArchiveDisposition.QUARANTINE,
        ) from None


def _control_state(
    *,
    seq: int,
    msgid: str,
    msgtype: str,
    action: object,
    content_ref: str,
) -> ArchiveControlState | None:
    if action == "recall":
        return ArchiveControlState(
            seq=seq,
            provider_event_id=msgid,
            disposition=ArchiveDisposition.PRESERVE_ONLY,
            reason_code="wecom_archive.message_recalled",
            content_ref=content_ref,
        )
    if msgtype == "revoke":
        return ArchiveControlState(
            seq=seq,
            provider_event_id=msgid,
            disposition=ArchiveDisposition.PRESERVE_ONLY,
            reason_code="wecom_archive.message_revoked",
            content_ref=content_ref,
        )
    if msgtype == "agree":
        return ArchiveControlState(
            seq=seq,
            provider_event_id=msgid,
            disposition=ArchiveDisposition.PRESERVE_ONLY,
            reason_code="wecom_archive.consent_granted",
            content_ref=content_ref,
        )
    if msgtype == "disagree":
        return ArchiveControlState(
            seq=seq,
            provider_event_id=msgid,
            disposition=ArchiveDisposition.PAUSE,
            reason_code="wecom_archive.consent_declined",
            content_ref=content_ref,
        )
    return None


def _walk_mappings(value: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    found: list[Mapping[str, object]] = [value]
    for nested in value.values():
        if isinstance(nested, dict) and all(isinstance(key, str) for key in nested):
            found.extend(_walk_mappings(nested))
        elif isinstance(nested, list):
            for item in nested:
                if isinstance(item, dict) and all(isinstance(key, str) for key in item):
                    found.extend(_walk_mappings(item))
    return tuple(found)
