from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Protocol

from .email_publication import EmailHeaderFacts, EmailParticipantSubject
from .identity_tokens import (
    IdentityTokenError,
    IdentityTokenResolver,
    TransientIdentitySubject,
    normalize_identity_subject,
)
from .models import (
    ConnectorItem,
    EvidenceArtifact,
    NormalizedObservationInput,
    Participant,
    RawDelivery,
    stable_ulid,
)

_SAFE_KIND = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,79}$")
_OPAQUE_IDENTITY = re.compile(
    r"^extid:v1:(email|wecom|whatsapp|phone|manual_import):"
    r"[A-Za-z0-9_-]{43}$"
)
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
    participants: tuple[Participant, ...] | None = None,
) -> NormalizedObservationInput:
    return NormalizedObservationInput(
        channel=channel,
        participants=participants
        or (
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


def _validate_identity_configuration(
    resolver: IdentityTokenResolver | None,
    site_id: str | None,
    purpose: str | None,
) -> None:
    configured = (resolver is not None, site_id is not None, purpose is not None)
    if any(configured) and not all(configured):
        raise ValueError("identity token configuration must be complete")
    if resolver is not None and not callable(getattr(resolver, "resolve", None)):
        raise TypeError("invalid identity token resolver")


def _scoped_participants(
    *,
    item: ConnectorItem,
    channel: str,
    source_ref: str,
    provider: str,
    subjects: tuple[TransientIdentitySubject, ...],
    role: str,
    resolver: IdentityTokenResolver | None,
    site_id: str | None,
    purpose: str | None,
    invalid_code: str,
) -> tuple[Participant, ...]:
    if len(subjects) > 1_000 or any(
        not isinstance(value, TransientIdentitySubject) or value.provider != provider
        for value in subjects
    ):
        raise NormalizationRejected(invalid_code)

    normalized_subjects: list[str] = []
    for value in subjects:
        try:
            normalized_subjects.append(normalize_identity_subject(provider, value.subject))
        except IdentityTokenError:
            raise NormalizationRejected(invalid_code) from None
    if not normalized_subjects or resolver is None or site_id is None or purpose is None:
        return (
            Participant(
                role=role,
                identity_ref=_unresolved_identity(
                    channel,
                    source_ref,
                    item.provider_event_id,
                ),
            ),
        )

    participants: list[Participant] = []
    seen: set[str] = set()
    for normalized_subject in normalized_subjects:
        try:
            identity_ref = resolver.resolve(site_id, purpose, provider, normalized_subject)
        except IdentityTokenError:
            raise NormalizationRejected(invalid_code) from None
        except Exception:
            raise NormalizationRejected(f"{provider}.identity_token_failed") from None
        match = _OPAQUE_IDENTITY.fullmatch(identity_ref) if isinstance(identity_ref, str) else None
        if match is None or match.group(1) != provider:
            raise NormalizationRejected(f"{provider}.identity_token_failed")
        if identity_ref in seen:
            continue
        seen.add(identity_ref)
        participants.append(Participant(role=role, identity_ref=identity_ref))
    return tuple(participants)


class WhatsAppObservationNormalizer:
    __slots__ = ("_identity_resolver", "_max_contacts", "_purpose", "_site_id")

    def __init__(
        self,
        *,
        max_contacts: int = 64,
        identity_resolver: IdentityTokenResolver | None = None,
        site_id: str | None = None,
        purpose: str | None = None,
    ) -> None:
        if isinstance(max_contacts, bool) or not 0 <= max_contacts <= 1_000:
            raise ValueError("invalid max_contacts")
        _validate_identity_configuration(identity_resolver, site_id, purpose)
        self._max_contacts = max_contacts
        self._identity_resolver = identity_resolver
        self._site_id = site_id
        self._purpose = purpose

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
        raw_sender = message.get("from")
        contact_subjects: list[str] = []
        for contact in contacts:
            contact_subject = contact.get("wa_id")
            if not isinstance(contact_subject, str):
                raise NormalizationRejected("whatsapp.invalid_sender")
            contact_subjects.append(contact_subject)
        unique_contacts = tuple(dict.fromkeys(contact_subjects))
        if raw_sender is None:
            if len(unique_contacts) > 1:
                raise NormalizationRejected("whatsapp.ambiguous_sender")
            sender = unique_contacts[0] if unique_contacts else None
        elif isinstance(raw_sender, str) and raw_sender and len(raw_sender) <= 256:
            sender = raw_sender
            if unique_contacts and unique_contacts != (sender,):
                raise NormalizationRejected("whatsapp.ambiguous_sender")
        else:
            raise NormalizationRejected("whatsapp.invalid_sender")
        if sender is None:
            role = "unknown"
            identity_subjects: tuple[TransientIdentitySubject, ...] = ()
        else:
            role = "external"
            identity_subjects = (TransientIdentitySubject(provider="whatsapp", subject=sender),)
        return _normalized(
            channel="chat",
            item=item,
            role=role,
            source_ref=source_ref,
            participants=_scoped_participants(
                item=item,
                channel="chat",
                source_ref=source_ref,
                provider="whatsapp",
                subjects=identity_subjects,
                role=role,
                resolver=self._identity_resolver,
                site_id=self._site_id,
                purpose=self._purpose,
                invalid_code="whatsapp.invalid_sender",
            ),
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
    __slots__ = ("_identity_resolver", "_purpose", "_site_id")

    def __init__(
        self,
        *,
        identity_resolver: IdentityTokenResolver | None = None,
        site_id: str | None = None,
        purpose: str | None = None,
    ) -> None:
        _validate_identity_configuration(identity_resolver, site_id, purpose)
        self._identity_resolver = identity_resolver
        self._site_id = site_id
        self._purpose = purpose

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

    def _normalize_raw_delivery(
        self,
        item: ConnectorItem,
        *,
        source_ref: str,
    ) -> NormalizedObservationInput:
        payload = item.payload
        base_fields = {
            "kind",
            "source_ref",
            "body_evidence",
            "attachment_evidence",
            "identity_subjects",
        }
        enhanced_fields = base_fields | {
            "email_participant_subjects",
            "email_header_facts",
        }
        if (
            set(payload) not in {frozenset(base_fields), frozenset(enhanced_fields)}
            or payload.get("source_ref") != source_ref
        ):
            raise NormalizationRejected("email.invalid_adapter_shape")
        body = payload.get("body_evidence")
        attachments = payload.get("attachment_evidence")
        identity_subjects = payload.get("identity_subjects")
        participant_subjects = payload.get("email_participant_subjects")
        header_facts = payload.get("email_header_facts")
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
            or not isinstance(identity_subjects, tuple)
        ):
            raise NormalizationRejected("email.invalid_evidence_shape")
        if set(payload) == enhanced_fields and (
            not isinstance(participant_subjects, tuple)
            or not participant_subjects
            or not all(isinstance(value, EmailParticipantSubject) for value in participant_subjects)
            or not isinstance(header_facts, EmailHeaderFacts)
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
            participants=(
                tuple(
                    _scoped_participants(
                        item=item,
                        channel="email",
                        source_ref=source_ref,
                        provider="email",
                        subjects=(value.subject,),
                        role="unknown",
                        resolver=self._identity_resolver,
                        site_id=self._site_id,
                        purpose=self._purpose,
                        invalid_code="email.invalid_subject",
                    )[0]
                    for value in participant_subjects
                )
                if isinstance(participant_subjects, tuple)
                else _scoped_participants(
                    item=item,
                    channel="email",
                    source_ref=source_ref,
                    provider="email",
                    subjects=identity_subjects,
                    role="unknown",
                    resolver=self._identity_resolver,
                    site_id=self._site_id,
                    purpose=self._purpose,
                    invalid_code="email.invalid_subject",
                )
            ),
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
    __slots__ = ("_identity_resolver", "_purpose", "_site_id")

    def __init__(
        self,
        *,
        identity_resolver: IdentityTokenResolver | None = None,
        site_id: str | None = None,
        purpose: str | None = None,
    ) -> None:
        _validate_identity_configuration(identity_resolver, site_id, purpose)
        self._identity_resolver = identity_resolver
        self._site_id = site_id
        self._purpose = purpose

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
            "identity_subjects",
        }:
            raise NormalizationRejected("wecom.invalid_adapter_shape")
        message_type = payload.get("message_type")
        decrypted_ref = payload.get("decrypted_content_ref")
        media_pending = payload.get("media_pending")
        identity_subjects = payload.get("identity_subjects")
        if (
            not isinstance(message_type, str)
            or _SAFE_KIND.fullmatch(message_type) is None
            or not isinstance(decrypted_ref, str)
            or not isinstance(media_pending, bool)
            or not isinstance(identity_subjects, tuple)
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
            participants=_scoped_participants(
                item=item,
                channel="chat",
                source_ref=source_ref,
                provider="wecom",
                subjects=identity_subjects,
                role="unknown",
                resolver=self._identity_resolver,
                site_id=self._site_id,
                purpose=self._purpose,
                invalid_code="wecom.invalid_subject",
            ),
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
