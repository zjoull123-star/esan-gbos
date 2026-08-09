from __future__ import annotations

import json
import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from typing import Literal, Protocol

from ..models import RawDelivery, stable_ulid

_CHECKPOINT_KEYS = frozenset({"mailbox", "uid", "uidvalidity", "version"})
_FETCH_UID = re.compile(rb"(?:^|[ (])UID +([1-9][0-9]*)(?=[ )])")
_FETCH_INTERNAL_DATE = re.compile(rb'INTERNALDATE +"([^"]+)"')
_FETCH_BODY_LITERAL = re.compile(rb"BODY\[\](?:<\d+>)? +\{([0-9]+)\}")
_SAFE_TEXT = re.compile(r"^[^\x00-\x1f\x7f]+$")
_MAX_IMAP_VALUE = 2**32 - 1


class TlsImapClient(Protocol):
    def login(self, username: str, password: str) -> tuple[object, object]: ...

    def select(self, mailbox: str, readonly: bool = False) -> tuple[object, object]: ...

    def response(self, code: str) -> tuple[object, object]: ...

    def uid(self, command: str, *args: object) -> tuple[object, object]: ...

    def logout(self) -> tuple[object, object]: ...


TlsImapClientFactory = Callable[[str, int], TlsImapClient]


def _require_safe_text(value: str, field_name: str, *, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or _SAFE_TEXT.fullmatch(value) is None
    ):
        raise ValueError(f"invalid {field_name}")


def _require_int_range(value: int, field_name: str, *, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"invalid {field_name}")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class EmailImapConfig:
    """Non-secret, bounded settings for one local IMAP polling instance."""

    host: str
    port: int
    mailbox: str
    folder: str
    enabled_at: datetime
    poll_limit: int
    max_message_bytes: int
    max_attachment_bytes: int
    max_attachments: int
    rescan_max_window: timedelta
    rescan_max_uids: int

    def __post_init__(self) -> None:
        _require_safe_text(self.host, "host", maximum=253)
        _require_int_range(self.port, "port", minimum=1, maximum=65_535)
        _require_safe_text(self.mailbox, "mailbox", maximum=256)
        _require_safe_text(self.folder, "folder", maximum=256)
        if (
            not isinstance(self.enabled_at, datetime)
            or self.enabled_at.tzinfo is None
            or self.enabled_at.utcoffset() is None
        ):
            raise ValueError("enabled_at must be timezone-aware")
        _require_int_range(self.poll_limit, "poll_limit", minimum=1, maximum=1_000)
        _require_int_range(
            self.max_message_bytes,
            "max_message_bytes",
            minimum=1,
            maximum=100_000_000,
        )
        _require_int_range(
            self.max_attachment_bytes,
            "max_attachment_bytes",
            minimum=1,
            maximum=100_000_000,
        )
        _require_int_range(self.max_attachments, "max_attachments", minimum=1, maximum=1_000)
        if (
            not isinstance(self.rescan_max_window, timedelta)
            or self.rescan_max_window <= timedelta(0)
            or self.rescan_max_window > timedelta(days=90)
        ):
            raise ValueError("invalid rescan_max_window")
        _require_int_range(
            self.rescan_max_uids,
            "rescan_max_uids",
            minimum=1,
            maximum=10_000,
        )


@dataclass(frozen=True, slots=True)
class ImapCheckpoint:
    mailbox: str
    uidvalidity: int
    uid: int

    def __post_init__(self) -> None:
        _require_safe_text(self.mailbox, "checkpoint mailbox", maximum=256)
        _require_int_range(self.uidvalidity, "UIDVALIDITY", minimum=1, maximum=_MAX_IMAP_VALUE)
        _require_int_range(self.uid, "UID", minimum=0, maximum=_MAX_IMAP_VALUE)

    def serialize(self) -> str:
        return json.dumps(
            {
                "mailbox": self.mailbox,
                "uid": self.uid,
                "uidvalidity": self.uidvalidity,
                "version": 1,
            },
            separators=(",", ":"),
        )

    @classmethod
    def parse(cls, value: str) -> ImapCheckpoint:
        try:
            decoded = json.loads(value, object_pairs_hook=_unique_json_object)
            if not isinstance(decoded, dict) or frozenset(decoded) != _CHECKPOINT_KEYS:
                raise ValueError
            if decoded["version"] != 1 or type(decoded["version"]) is not int:
                raise ValueError
            mailbox = decoded["mailbox"]
            uidvalidity = decoded["uidvalidity"]
            uid = decoded["uid"]
            if not isinstance(mailbox, str) or type(uidvalidity) is not int or type(uid) is not int:
                raise ValueError
            return cls(mailbox=mailbox, uidvalidity=uidvalidity, uid=uid)
        except json.JSONDecodeError, TypeError, ValueError:
            raise ValueError("invalid IMAP checkpoint") from None

    def advance(self, uid: int) -> ImapCheckpoint:
        _require_int_range(uid, "UID", minimum=0, maximum=_MAX_IMAP_VALUE)
        if uid < self.uid:
            raise ValueError("checkpoint cannot move backwards")
        if uid == self.uid:
            return self
        return ImapCheckpoint(self.mailbox, self.uidvalidity, uid)


@dataclass(frozen=True, slots=True)
class AttachmentEvidenceCandidate:
    """Decoded attachment bytes offered as evidence without changing the source message."""

    part_index: int
    media_type: str
    filename: str | None
    content: bytes = field(repr=False)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(part_index={self.part_index}, "
            "metadata=<redacted>, "
            f"content=<redacted bytes={len(self.content)}>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class EmailImapMessage:
    """One raw email whose checkpoint candidate is usable only after it is durably accepted."""

    uid: int
    provider_event_id: str
    checkpoint_candidate: str
    raw_delivery: RawDelivery = field(repr=False)
    message_id: str | None
    evidence_candidates: tuple[AttachmentEvidenceCandidate, ...]
    attachment_status: Literal["ready", "quarantined"]
    attachment_error_code: str | None

    def __post_init__(self) -> None:
        if self.attachment_status == "ready":
            if self.attachment_error_code is not None:
                raise ValueError("ready attachments cannot have an error code")
        elif self.attachment_status == "quarantined":
            if self.attachment_error_code not in {
                "attachment_limit_exceeded",
                "attachment_too_large",
                "attachment_decode_failed",
            }:
                raise ValueError("quarantined attachments require a safe error code")
            if self.evidence_candidates:
                raise ValueError("quarantined attachments cannot expose partial candidates")
        else:
            raise ValueError("invalid attachment status")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(uid={self.uid}, "
            f"provider_event_id={self.provider_event_id!r}, "
            "checkpoint_candidate=<redacted>, raw_delivery=<redacted>, "
            "message_id=<redacted>, "
            f"attachment_status={self.attachment_status!r}, "
            f"attachment_error_code={self.attachment_error_code!r}, "
            f"evidence_candidate_count={len(self.evidence_candidates)})"
        )


@dataclass(frozen=True, slots=True)
class ImapRescanPlan:
    mailbox: str
    previous_uidvalidity: int
    observed_uidvalidity: int
    lower_bound: datetime
    max_window: timedelta
    max_uids: int


@dataclass(frozen=True, slots=True, repr=False)
class ImapPollResult:
    """Checkpoint candidates MUST NOT be committed until raw deliveries are durably accepted."""

    status: Literal["ok", "retry", "paused", "rejected"]
    checkpoint_candidate: str | None
    messages: tuple[EmailImapMessage, ...] = ()
    error_code: str | None = None
    rescan_plan: ImapRescanPlan | None = None

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(status={self.status!r}, "
            "checkpoint_candidate=<redacted>, "
            f"message_count={len(self.messages)}, "
            f"error_code={self.error_code!r}, "
            f"has_rescan_plan={self.rescan_plan is not None})"
        )


class _FetchFailure(Exception):
    def __init__(self, error_code: str) -> None:
        super().__init__()
        self.error_code = error_code


class EmailImapConnector:
    """Read-only local-pilot IMAP adapter with all secrets scoped to ``poll``."""

    def __init__(
        self,
        *,
        connector_instance_id: str,
        config: EmailImapConfig,
        tls_client_factory: TlsImapClientFactory,
        clock: Callable[[], datetime],
    ) -> None:
        _require_safe_text(connector_instance_id, "connector_instance_id", maximum=256)
        if not callable(tls_client_factory):
            raise TypeError("tls_client_factory must be callable")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._connector_instance_id = connector_instance_id
        self._config = config
        self._tls_client_factory = tls_client_factory
        self._clock = clock

    @property
    def config(self) -> EmailImapConfig:
        return self._config

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"connector_instance_id={self._connector_instance_id!r}, "
            f"config={self._config!r})"
        )

    def poll(
        self,
        checkpoint: str | None,
        *,
        username: str,
        password: str,
        limit: int | None = None,
    ) -> ImapPollResult:
        """Fetch one all-or-nothing UID batch without storing authentication values."""

        requested_limit = self._config.poll_limit if limit is None else limit
        _require_int_range(
            requested_limit,
            "limit",
            minimum=1,
            maximum=self._config.poll_limit,
        )
        if (
            not isinstance(username, str)
            or not username
            or not isinstance(password, str)
            or not password
        ):
            raise ValueError("invalid authentication input")

        parsed_checkpoint: ImapCheckpoint | None
        if checkpoint is None:
            parsed_checkpoint = None
        else:
            try:
                parsed_checkpoint = ImapCheckpoint.parse(checkpoint)
            except ValueError:
                return self._failure("retry", checkpoint, "invalid_checkpoint")
            if parsed_checkpoint.mailbox != self._config.mailbox:
                return self._failure("paused", checkpoint, "checkpoint_mailbox_mismatch")

        try:
            client = self._tls_client_factory(self._config.host, self._config.port)
        except Exception:
            return self._failure("retry", checkpoint, "connect_failed")

        try:
            try:
                login_status, _ = client.login(username, password)
            except Exception:
                return self._failure("retry", checkpoint, "authentication_failed")
            if not _status_is_ok(login_status):
                return self._failure("retry", checkpoint, "authentication_failed")

            try:
                select_status, _ = client.select(self._config.folder, readonly=True)
            except Exception:
                return self._failure("retry", checkpoint, "select_failed")
            if not _status_is_ok(select_status):
                return self._failure("retry", checkpoint, "select_failed")

            try:
                uidvalidity = _parse_uidvalidity(client.response("UIDVALIDITY"))
            except Exception:
                return self._failure("retry", checkpoint, "invalid_uidvalidity")

            if parsed_checkpoint is not None and parsed_checkpoint.uidvalidity != uidvalidity:
                now = self._clock()
                if now.tzinfo is None or now.utcoffset() is None:
                    return self._failure("retry", checkpoint, "invalid_clock")
                lower_bound = max(
                    self._config.enabled_at,
                    now - self._config.rescan_max_window,
                )
                return ImapPollResult(
                    status="paused",
                    checkpoint_candidate=checkpoint,
                    error_code="uidvalidity_changed",
                    rescan_plan=ImapRescanPlan(
                        mailbox=self._config.mailbox,
                        previous_uidvalidity=parsed_checkpoint.uidvalidity,
                        observed_uidvalidity=uidvalidity,
                        lower_bound=lower_bound,
                        max_window=self._config.rescan_max_window,
                        max_uids=self._config.rescan_max_uids,
                    ),
                )

            base_checkpoint = parsed_checkpoint or ImapCheckpoint(
                self._config.mailbox,
                uidvalidity,
                0,
            )
            try:
                uids = self._search_uids(client, base_checkpoint, requested_limit)
            except _FetchFailure as exc:
                return self._failure("retry", checkpoint, exc.error_code)

            messages: list[EmailImapMessage] = []
            next_checkpoint = base_checkpoint
            try:
                for uid in uids:
                    raw, received_at = self._fetch_message(client, uid)
                    next_checkpoint = next_checkpoint.advance(uid)
                    if received_at < self._config.enabled_at:
                        continue
                    messages.append(
                        self._build_message(
                            uid=uid,
                            uidvalidity=uidvalidity,
                            raw=raw,
                            received_at=received_at,
                            checkpoint_candidate=next_checkpoint.serialize(),
                        )
                    )
            except _FetchFailure as exc:
                status: Literal["retry", "rejected"] = (
                    "rejected" if exc.error_code == "message_too_large" else "retry"
                )
                return self._failure(status, checkpoint, exc.error_code)
            except Exception:
                return self._failure("retry", checkpoint, "fetch_failed")

            return ImapPollResult(
                status="ok",
                checkpoint_candidate=next_checkpoint.serialize(),
                messages=tuple(messages),
            )
        finally:
            with suppress(Exception):
                client.logout()

    def _search_uids(
        self,
        client: TlsImapClient,
        checkpoint: ImapCheckpoint,
        limit: int,
    ) -> tuple[int, ...]:
        if checkpoint.uid == _MAX_IMAP_VALUE:
            return ()
        try:
            if checkpoint.uid == 0:
                search_status, search_data = client.uid(
                    "SEARCH",
                    None,
                    "SINCE",
                    self._config.enabled_at.strftime("%d-%b-%Y"),
                )
            else:
                search_status, search_data = client.uid(
                    "SEARCH",
                    None,
                    "UID",
                    f"{checkpoint.uid + 1}:*",
                )
        except Exception:
            raise _FetchFailure("search_failed") from None
        if not _status_is_ok(search_status):
            raise _FetchFailure("search_failed")
        try:
            values = _single_bytes_response(search_data)
            if not values:
                return ()
            parsed = {int(token) for token in values.split()}
            if any(uid <= checkpoint.uid or uid <= 0 or uid > _MAX_IMAP_VALUE for uid in parsed):
                raise ValueError
        except TypeError, ValueError:
            raise _FetchFailure("invalid_search_response") from None
        return tuple(sorted(parsed))[:limit]

    def _fetch_message(
        self,
        client: TlsImapClient,
        uid: int,
    ) -> tuple[bytes, datetime]:
        try:
            fetch_status, fetch_data = client.uid(
                "FETCH",
                str(uid),
                "(UID INTERNALDATE BODY.PEEK[])",
            )
        except Exception:
            raise _FetchFailure("fetch_failed") from None
        if not _status_is_ok(fetch_status):
            raise _FetchFailure("fetch_failed")
        try:
            metadata, raw = _single_fetch_tuple(fetch_data)
            uid_match = _FETCH_UID.search(metadata)
            date_match = _FETCH_INTERNAL_DATE.search(metadata)
            body_match = _FETCH_BODY_LITERAL.search(metadata)
            if uid_match is None or date_match is None or body_match is None:
                raise ValueError
            if int(uid_match.group(1)) != uid or int(body_match.group(1)) != len(raw):
                raise ValueError
            received_at = parsedate_to_datetime(date_match.group(1).decode("ascii"))
            if received_at.tzinfo is None or received_at.utcoffset() is None:
                raise ValueError
        except UnicodeDecodeError, TypeError, ValueError:
            raise _FetchFailure("invalid_fetch_response") from None
        if len(raw) > self._config.max_message_bytes:
            raise _FetchFailure("message_too_large")
        return raw, received_at

    def _build_message(
        self,
        *,
        uid: int,
        uidvalidity: int,
        raw: bytes,
        received_at: datetime,
        checkpoint_candidate: str,
    ) -> EmailImapMessage:
        provider_event_id = stable_ulid(
            "email-imap",
            self._connector_instance_id,
            self._config.mailbox,
            str(uidvalidity),
            str(uid),
        )
        raw_delivery = RawDelivery(
            delivery_id=provider_event_id,
            exact_bytes=raw,
            media_type="message/rfc822",
            received_at=received_at,
        )
        message_id: str | None = None
        candidates: tuple[AttachmentEvidenceCandidate, ...] = ()
        attachment_status: Literal["ready", "quarantined"] = "ready"
        attachment_error_code: str | None = None
        try:
            parsed = BytesParser(policy=policy.default).parsebytes(raw)
            raw_message_id = parsed.get("Message-ID")
            message_id = str(raw_message_id) if raw_message_id is not None else None
            attachments = tuple(parsed.iter_attachments())
            if len(attachments) > self._config.max_attachments:
                raise _FetchFailure("attachment_limit_exceeded")
            candidate_list: list[AttachmentEvidenceCandidate] = []
            for index, attachment in enumerate(attachments, start=1):
                if attachment.defects:
                    raise _FetchFailure("attachment_decode_failed")
                content = attachment.get_payload(decode=True)
                if attachment.defects or not isinstance(content, bytes):
                    raise _FetchFailure("attachment_decode_failed")
                if len(content) > self._config.max_attachment_bytes:
                    raise _FetchFailure("attachment_too_large")
                candidate_list.append(
                    AttachmentEvidenceCandidate(
                        part_index=index,
                        media_type=attachment.get_content_type(),
                        filename=attachment.get_filename(),
                        content=content,
                    )
                )
            candidates = tuple(candidate_list)
        except _FetchFailure as exc:
            attachment_status = "quarantined"
            attachment_error_code = exc.error_code
            candidates = ()
        except Exception:
            attachment_status = "quarantined"
            attachment_error_code = "attachment_decode_failed"
            candidates = ()
        return EmailImapMessage(
            uid=uid,
            provider_event_id=provider_event_id,
            checkpoint_candidate=checkpoint_candidate,
            raw_delivery=raw_delivery,
            message_id=message_id,
            evidence_candidates=candidates,
            attachment_status=attachment_status,
            attachment_error_code=attachment_error_code,
        )

    @staticmethod
    def _failure(
        status: Literal["retry", "paused", "rejected"],
        checkpoint: str | None,
        error_code: str,
    ) -> ImapPollResult:
        return ImapPollResult(
            status=status,
            checkpoint_candidate=checkpoint,
            error_code=error_code,
        )


def _status_is_ok(status: object) -> bool:
    if isinstance(status, bytes):
        return status.upper() == b"OK"
    return isinstance(status, str) and status.upper() == "OK"


def _single_bytes_response(data: object) -> bytes:
    if not isinstance(data, (list, tuple)) or len(data) != 1 or not isinstance(data[0], bytes):
        raise ValueError
    return data[0]


def _single_fetch_tuple(data: object) -> tuple[bytes, bytes]:
    if not isinstance(data, (list, tuple)):
        raise ValueError
    candidates = [
        item
        for item in data
        if isinstance(item, tuple)
        and len(item) == 2
        and isinstance(item[0], bytes)
        and isinstance(item[1], bytes)
    ]
    if len(candidates) != 1:
        raise ValueError
    metadata, raw = candidates[0]
    return metadata, raw


def _parse_uidvalidity(response: tuple[object, object]) -> int:
    response_code, data = response
    code_is_valid = (
        isinstance(response_code, str)
        and response_code.upper() == "UIDVALIDITY"
        or isinstance(response_code, bytes)
        and response_code.upper() == b"UIDVALIDITY"
    )
    if not code_is_valid:
        raise ValueError
    raw = _single_bytes_response(data)
    if not raw.isdigit():
        raise ValueError
    value = int(raw)
    _require_int_range(value, "UIDVALIDITY", minimum=1, maximum=_MAX_IMAP_VALUE)
    return value
