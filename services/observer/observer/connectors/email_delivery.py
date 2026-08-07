"""Strict metadata-aware decoding for durably stored RFC 822 deliveries."""

from __future__ import annotations

from datetime import datetime
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from html.parser import HTMLParser

from ..local_pilot_ingestion import DeliveryQuarantine
from ..models import ConnectorItem, EvidenceArtifact


class _RedactedEvidenceArtifact(EvidenceArtifact):
    def __repr__(self) -> str:
        return (
            f"EvidenceArtifact(media_type={self.media_type!r}, "
            f"locator={self.locator!r}, role={self.role!r}, "
            f"content=<redacted bytes={len(self.content or b'')}>, reference=None)"
        )


class _BoundedHtmlText(HTMLParser):
    __slots__ = ("_chunks", "_ignored_depth", "_maximum", "_size")

    def __init__(self, *, maximum: int) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._ignored_depth = 0
        self._maximum = maximum
        self._size = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del attrs
        if tag.lower() in {"script", "style", "template", "noscript"}:
            self._ignored_depth += 1
        elif self._ignored_depth == 0 and tag.lower() in {
            "br",
            "p",
            "div",
            "li",
            "tr",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
        }:
            self._append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "template", "noscript"}:
            if self._ignored_depth > 0:
                self._ignored_depth -= 1
        elif self._ignored_depth == 0:
            self._append(" ")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0:
            self._append(data)

    def _append(self, value: str) -> None:
        encoded_size = len(value.encode("utf-8"))
        self._size += encoded_size
        if self._size > self._maximum:
            raise DeliveryQuarantine("email.text_too_large")
        self._chunks.append(value)

    def text(self) -> str:
        return " ".join("".join(self._chunks).split())


class EmailRawDeliveryDecoder:
    """Decode one exact MIME message only when durable delivery metadata is supplied."""

    __slots__ = (
        "_max_attachment_bytes",
        "_max_attachments",
        "_max_depth",
        "_max_header_bytes",
        "_max_headers",
        "_max_message_bytes",
        "_max_parts",
        "_max_text_bytes",
    )

    def __init__(
        self,
        *,
        max_message_bytes: int = 10_000_000,
        max_attachment_bytes: int = 5_000_000,
        max_attachments: int = 20,
        max_text_bytes: int = 1_000_000,
        max_header_bytes: int = 65_536,
        max_headers: int = 200,
        max_parts: int = 100,
        max_depth: int = 10,
    ) -> None:
        boundaries = (
            max_message_bytes,
            max_attachment_bytes,
            max_attachments,
            max_text_bytes,
            max_header_bytes,
            max_headers,
            max_parts,
            max_depth,
        )
        if any(type(value) is not int or value < 1 for value in boundaries):
            raise ValueError("email decoder boundaries must be positive integers")
        if max_message_bytes > 100_000_000 or max_attachment_bytes > 100_000_000:
            raise ValueError("email decoder byte boundary is unbounded")
        if max_attachments > 1_000 or max_parts > 10_000 or max_depth > 128:
            raise ValueError("email decoder structural boundary is unbounded")
        self._max_message_bytes = max_message_bytes
        self._max_attachment_bytes = max_attachment_bytes
        self._max_attachments = max_attachments
        self._max_text_bytes = max_text_bytes
        self._max_header_bytes = max_header_bytes
        self._max_headers = max_headers
        self._max_parts = max_parts
        self._max_depth = max_depth

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(max_message_bytes={self._max_message_bytes}, "
            f"max_attachment_bytes={self._max_attachment_bytes}, "
            f"max_attachments={self._max_attachments}, "
            f"max_text_bytes={self._max_text_bytes}, payload=<redacted>)"
        )

    def decode(self, exact_bytes: bytes) -> tuple[ConnectorItem, ...]:
        del exact_bytes
        raise DeliveryQuarantine("email.delivery_metadata_required")

    def decode_delivery(
        self,
        exact_bytes: bytes,
        *,
        delivery_id: str,
        received_at: datetime,
        source_ref: str,
    ) -> tuple[ConnectorItem, ...]:
        self._validate_metadata(delivery_id, received_at, source_ref)
        if not isinstance(exact_bytes, bytes):
            raise DeliveryQuarantine("email.invalid_message")
        if len(exact_bytes) > self._max_message_bytes:
            raise DeliveryQuarantine("email.message_too_large")
        self._validate_top_headers(exact_bytes)
        try:
            message = BytesParser(policy=policy.default).parsebytes(exact_bytes)
        except Exception:
            raise DeliveryQuarantine("email.invalid_message") from None
        if not isinstance(message, EmailMessage) or message.defects:
            raise DeliveryQuarantine("email.invalid_message")

        parts = self._bounded_parts(message)
        text_parts: list[tuple[str, bytes]] = []
        attachments: list[_RedactedEvidenceArtifact] = []
        for part in parts:
            if part.defects or len(part.keys()) > self._max_headers:
                raise DeliveryQuarantine("email.invalid_headers")
            content_type = part.get_content_type().lower()
            if content_type == "message/rfc822":
                raise DeliveryQuarantine("email.nested_message_forbidden")
            if part.is_multipart():
                continue
            disposition = part.get_content_disposition()
            is_attachment = disposition == "attachment" or part.get_filename() is not None
            content = self._decoded_payload(part)
            if is_attachment:
                if len(attachments) >= self._max_attachments:
                    raise DeliveryQuarantine("email.attachment_limit")
                if len(content) > self._max_attachment_bytes:
                    raise DeliveryQuarantine("email.attachment_too_large")
                attachments.append(
                    _RedactedEvidenceArtifact(
                        media_type=content_type,
                        locator=f"attachment:{len(attachments) + 1}",
                        role="attachment",
                        content=content,
                    )
                )
            elif content_type in {"text/plain", "text/html"}:
                text_parts.append((content_type, self._decode_text(part, content)))

        plain = [content for media_type, content in text_parts if media_type == "text/plain"]
        if plain:
            body_content = b"\n".join(plain)
        else:
            html_parts = [
                content for media_type, content in text_parts if media_type == "text/html"
            ]
            if not html_parts:
                raise DeliveryQuarantine("email.body_missing")
            body_content = self._html_to_text(b"\n".join(html_parts))
        if len(body_content) > self._max_text_bytes:
            raise DeliveryQuarantine("email.text_too_large")
        body = _RedactedEvidenceArtifact(
            media_type="text/plain; charset=utf-8",
            locator="message-body",
            role="derived-text",
            content=body_content,
        )
        return (
            ConnectorItem(
                provider_event_id=delivery_id,
                occurred_at=received_at,
                source_cursor=delivery_id,
                payload={
                    "kind": "email_raw_delivery",
                    "source_ref": source_ref,
                    "body_evidence": body,
                    "attachment_evidence": tuple(attachments),
                },
            ),
        )

    @staticmethod
    def _validate_metadata(
        delivery_id: str,
        received_at: datetime,
        source_ref: str,
    ) -> None:
        if (
            not isinstance(delivery_id, str)
            or not delivery_id
            or delivery_id != delivery_id.strip()
            or len(delivery_id) > 512
            or any(value in delivery_id for value in ("\x00", "\r", "\n"))
            or not isinstance(received_at, datetime)
            or received_at.tzinfo is None
            or received_at.utcoffset() is None
            or not isinstance(source_ref, str)
            or not source_ref
            or len(source_ref) > 512
        ):
            raise DeliveryQuarantine("email.invalid_delivery_metadata")

    def _validate_top_headers(self, raw: bytes) -> None:
        separator = raw.find(b"\r\n\r\n")
        separator_size = 4
        if separator < 0:
            separator = raw.find(b"\n\n")
            separator_size = 2
        if separator < 0:
            raise DeliveryQuarantine("email.invalid_message")
        header = raw[: separator + separator_size]
        if len(header) > self._max_header_bytes:
            raise DeliveryQuarantine("email.header_bytes_limit")
        if any(len(line) > 8_192 for line in header.splitlines()):
            raise DeliveryQuarantine("email.header_line_limit")

    def _bounded_parts(self, message: Message) -> tuple[Message, ...]:
        result: list[Message] = []
        stack: list[tuple[Message, int]] = [(message, 1)]
        while stack:
            part, depth = stack.pop()
            if depth > self._max_depth:
                raise DeliveryQuarantine("email.mime_depth_limit")
            result.append(part)
            if len(result) > self._max_parts:
                raise DeliveryQuarantine("email.mime_part_limit")
            if part.is_multipart():
                payload = part.get_payload()
                if not isinstance(payload, list) or not all(
                    isinstance(child, Message) for child in payload
                ):
                    raise DeliveryQuarantine("email.invalid_message")
                stack.extend((child, depth + 1) for child in reversed(payload))
        return tuple(result)

    @staticmethod
    def _decoded_payload(part: Message) -> bytes:
        try:
            content = part.get_payload(decode=True)
        except Exception:
            raise DeliveryQuarantine("email.transfer_decode_failed") from None
        if not isinstance(content, bytes):
            raise DeliveryQuarantine("email.transfer_decode_failed")
        return content

    def _decode_text(self, part: Message, content: bytes) -> bytes:
        charset = part.get_content_charset() or "utf-8"
        try:
            text = content.decode(charset, errors="strict")
        except LookupError, UnicodeDecodeError:
            raise DeliveryQuarantine("email.text_decode_failed") from None
        encoded = text.encode("utf-8")
        if len(encoded) > self._max_text_bytes:
            raise DeliveryQuarantine("email.text_too_large")
        return encoded

    def _html_to_text(self, content: bytes) -> bytes:
        try:
            html = content.decode("utf-8", errors="strict")
            parser = _BoundedHtmlText(maximum=self._max_text_bytes)
            parser.feed(html)
            parser.close()
        except UnicodeDecodeError:
            raise DeliveryQuarantine("email.text_decode_failed") from None
        except DeliveryQuarantine:
            raise
        except Exception:
            raise DeliveryQuarantine("email.html_decode_failed") from None
        text = parser.text()
        if not text:
            raise DeliveryQuarantine("email.body_missing")
        return text.encode("utf-8")


__all__ = ["EmailRawDeliveryDecoder"]
