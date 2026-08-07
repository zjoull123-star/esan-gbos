from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Protocol

from .models import (
    ConnectorItem,
    EvidenceArtifact,
    NormalizedObservationInput,
    Participant,
    RawDelivery,
    stable_ulid,
)

_SAFE_KIND = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,79}$")
_POLICY = {
    "consent_basis": "pilot_deferred_review",
    "data_classification": "Restricted",
    "retention_class": "R1-operational",
    "original_language": "und",
}


class NormalizationRejected(ValueError):
    """Fail-closed adapter rejection whose rendering never includes provider data."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if _SAFE_KIND.fullmatch(code) is None:
            raise ValueError("invalid normalization rejection code")
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"NormalizationRejected(code={self.code!r})"


class _AttachmentCandidate(Protocol):
    part_index: int


class _EmailMessage(Protocol):
    provider_event_id: str
    raw_delivery: RawDelivery
    evidence_candidates: tuple[_AttachmentCandidate, ...]
    attachment_status: str
    attachment_error_code: str | None


class _TransientEvidenceArtifact(EvidenceArtifact):
    def __repr__(self) -> str:
        return (
            f"EvidenceArtifact(media_type={self.media_type!r}, "
            f"locator={self.locator!r}, role={self.role!r}, "
            f"content=<redacted bytes={len(self.content or b'')}>, reference=None)"
        )


def _require_ref(value: str, *, code: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 512:
        raise NormalizationRejected(code)
    return value


def _unresolved_identity(
    channel: str,
    source_ref: str,
    provider_event_id: str,
) -> str:
    token = stable_ulid(
        "delivery-scoped-unresolved-identity",
        channel,
        source_ref,
        provider_event_id,
    )
    return f"unresolved:delivery:{token}"


def _normalized(
    *,
    channel: str,
    item: ConnectorItem,
    role: str,
    source_ref: str,
    evidence: tuple[EvidenceArtifact, ...],
) -> NormalizedObservationInput:
    return NormalizedObservationInput(
        channel=channel,
        participants=(
            Participant(
                role=role,
                identity_ref=_unresolved_identity(
                    channel,
                    source_ref,
                    item.provider_event_id,
                ),
            ),
        ),
        evidence=evidence,
        correlation_id=stable_ulid(
            "normalized-observation",
            channel,
            item.provider_event_id,
        ),
        **_POLICY,
    )


class WhatsAppObservationNormalizer:
    __slots__ = ("_max_contacts",)

    def __init__(self, *, max_contacts: int = 64) -> None:
        if isinstance(max_contacts, bool) or not 0 <= max_contacts <= 1_000:
            raise ValueError("invalid max_contacts")
        self._max_contacts = max_contacts

    def normalize(
        self,
        item: ConnectorItem,
        *,
        source_ref: str,
    ) -> NormalizedObservationInput:
        source_ref = _require_ref(source_ref, code="whatsapp.invalid_source_ref")
        payload = item.payload
        allowed = {
            "kind",
            "message",
            "raw_contacts",
            "runtime_metadata",
        }
        if (
            not allowed <= set(payload)
            or set(payload) - (allowed | {"media_download_task"})
            or payload.get("kind") != "whatsapp_message"
        ):
            raise NormalizationRejected("whatsapp.invalid_adapter_shape")
        if "media_download_task" in payload:
            raise NormalizationRejected("whatsapp.media_pending")
        message = payload.get("message")
        contacts = payload.get("raw_contacts")
        if not isinstance(message, Mapping) or not isinstance(contacts, tuple):
            raise NormalizationRejected("whatsapp.invalid_adapter_shape")
        if len(contacts) > self._max_contacts:
            raise NormalizationRejected("whatsapp.contacts_limit")
        if any(not isinstance(contact, Mapping) for contact in contacts):
            raise NormalizationRejected("whatsapp.invalid_adapter_shape")
        if message.get("id") != item.provider_event_id:
            raise NormalizationRejected("whatsapp.provider_event_mismatch")
        sender = message.get("from")
        if sender is None:
            role = "unknown"
        elif isinstance(sender, str) and sender and len(sender) <= 256:
            role = "external"
        else:
            raise NormalizationRejected("whatsapp.invalid_sender")
        return _normalized(
            channel="chat",
            item=item,
            role=role,
            source_ref=source_ref,
            evidence=(
                EvidenceArtifact(
                    media_type="application/json",
                    locator="delivery",
                    role="source",
                    reference=source_ref,
                ),
            ),
        )


class EmailImapItemAdapter:
    def adapt(
        self,
        message: _EmailMessage,
        *,
        source_ref: str,
        attachment_refs: tuple[str, ...],
    ) -> ConnectorItem:
        source_ref = _require_ref(source_ref, code="email.invalid_source_ref")
        if message.attachment_status != "ready" or message.attachment_error_code is not None:
            raise NormalizationRejected("email.attachment_quarantined")
        if not isinstance(attachment_refs, tuple) or len(attachment_refs) != len(
            message.evidence_candidates
        ):
            raise NormalizationRejected("email.attachment_ref_mismatch")
        for reference in attachment_refs:
            _require_ref(reference, code="email.invalid_attachment_ref")
        if not isinstance(message.raw_delivery, RawDelivery):
            raise NormalizationRejected("email.invalid_adapter_shape")
        return ConnectorItem(
            provider_event_id=message.provider_event_id,
            occurred_at=message.raw_delivery.received_at,
            source_cursor=str(
                stable_ulid(
                    "email-imap-cursor",
                    message.provider_event_id,
                    source_ref,
                )
            ),
            payload={
                "kind": "email_imap_message",
                "source_ref": source_ref,
                "attachment_refs": attachment_refs,
            },
        )


class EmailObservationNormalizer:
    def normalize(
        self,
        item: ConnectorItem,
        *,
        source_ref: str,
    ) -> NormalizedObservationInput:
        source_ref = _require_ref(source_ref, code="email.invalid_source_ref")
        payload = item.payload
        if payload.get("kind") == "email_raw_delivery":
            return self._normalize_raw_delivery(
                item,
                source_ref=source_ref,
            )
        if (
            set(payload) != {"kind", "source_ref", "attachment_refs"}
            or payload.get("kind") != "email_imap_message"
        ):
            raise NormalizationRejected("email.invalid_adapter_shape")
        if payload.get("source_ref") != source_ref:
            raise NormalizationRejected("email.source_ref_mismatch")
        attachment_refs = payload.get("attachment_refs")
        if not isinstance(attachment_refs, tuple) or len(attachment_refs) > 1_000:
            raise NormalizationRejected("email.invalid_adapter_shape")
        references = (source_ref, *attachment_refs)
        if len(references) != len(set(references)):
            raise NormalizationRejected("email.duplicate_evidence_ref")
        for reference in references:
            _require_ref(reference, code="email.invalid_evidence_ref")
        return _normalized(
            channel="email",
            item=item,
            role="unknown",
            source_ref=source_ref,
            evidence=tuple(
                EvidenceArtifact(
                    media_type=("message/rfc822" if index == 0 else "application/octet-stream"),
                    locator="message" if index == 0 else f"attachment:{index}",
                    role="source" if index == 0 else "attachment",
                    reference=reference,
                )
                for index, reference in enumerate(references)
            ),
        )

    @staticmethod
    def _normalize_raw_delivery(
        item: ConnectorItem,
        *,
        source_ref: str,
    ) -> NormalizedObservationInput:
        payload = item.payload
        if (
            set(payload)
            != {
                "kind",
                "source_ref",
                "body_evidence",
                "attachment_evidence",
            }
            or payload.get("source_ref") != source_ref
        ):
            raise NormalizationRejected("email.invalid_adapter_shape")
        body = payload.get("body_evidence")
        attachments = payload.get("attachment_evidence")
        if (
            not isinstance(body, EvidenceArtifact)
            or body.content is None
            or body.reference is not None
            or body.media_type != "text/plain; charset=utf-8"
            or body.locator != "message-body"
            or body.role != "derived-text"
            or not isinstance(attachments, tuple)
            or len(attachments) > 1_000
            or not all(isinstance(value, EvidenceArtifact) for value in attachments)
        ):
            raise NormalizationRejected("email.invalid_evidence_shape")
        checked: list[EvidenceArtifact] = [
            _TransientEvidenceArtifact(
                media_type=body.media_type,
                locator=body.locator,
                role=body.role,
                content=body.content,
            )
        ]
        for index, artifact in enumerate(attachments, start=1):
            if (
                artifact.content is None
                or artifact.reference is not None
                or artifact.locator != f"attachment:{index}"
                or artifact.role != "attachment"
            ):
                raise NormalizationRejected("email.invalid_evidence_shape")
            checked.append(
                _TransientEvidenceArtifact(
                    media_type=artifact.media_type,
                    locator=artifact.locator,
                    role=artifact.role,
                    content=artifact.content,
                )
            )
        return _normalized(
            channel="email",
            item=item,
            role="unknown",
            source_ref=source_ref,
            evidence=(
                EvidenceArtifact(
                    media_type="message/rfc822",
                    locator="message",
                    role="source",
                    reference=source_ref,
                ),
                *checked,
            ),
        )


class WeComObservationNormalizer:
    def normalize(
        self,
        item: ConnectorItem,
        *,
        source_ref: str,
    ) -> NormalizedObservationInput:
        source_ref = _require_ref(source_ref, code="wecom.invalid_source_ref")
        payload = item.payload
        if set(payload) != {
            "message_type",
            "decrypted_content_ref",
            "media_pending",
        }:
            raise NormalizationRejected("wecom.invalid_adapter_shape")
        message_type = payload.get("message_type")
        decrypted_ref = payload.get("decrypted_content_ref")
        media_pending = payload.get("media_pending")
        if (
            not isinstance(message_type, str)
            or _SAFE_KIND.fullmatch(message_type) is None
            or not isinstance(decrypted_ref, str)
            or not isinstance(media_pending, bool)
        ):
            raise NormalizationRejected("wecom.invalid_adapter_shape")
        if media_pending:
            raise NormalizationRejected("wecom.media_pending")
        decrypted_ref = _require_ref(
            decrypted_ref,
            code="wecom.invalid_decrypted_ref",
        )
        references = (source_ref,) if source_ref == decrypted_ref else (source_ref, decrypted_ref)
        return _normalized(
            channel="chat",
            item=item,
            role="unknown",
            source_ref=source_ref,
            evidence=tuple(
                EvidenceArtifact(
                    media_type="application/json",
                    locator=("delivery" if index == 0 else "decrypted-message"),
                    role="source",
                    reference=reference,
                )
                for index, reference in enumerate(references)
            ),
        )
