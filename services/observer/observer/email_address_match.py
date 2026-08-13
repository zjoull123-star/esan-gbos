"""Purpose-bound transient comparison of one email EvidenceRef and authority candidate."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from email import policy
from email.headerregistry import AddressHeader
from email.parser import BytesParser
from typing import Protocol

from .identity_tokens import IdentityTokenError, normalize_identity_subject
from .models import TenantScope, _require_aware, stable_ulid

EMAIL_ADDRESS_NORMALIZATION_VERSION = "email-address-v1"
EMAIL_ADDRESS_MATCH_PURPOSE = "email_address_identity_confirmation"
_ROLES = frozenset({"from", "to", "cc", "bcc"})
_TARGET_TYPES = frozenset({"User", "Party"})
_OPAQUE_ADDRESS_REF = re.compile(r"^extid:v1:email:[A-Za-z0-9_-]{43}$")
_TARGET_REF = re.compile(r"^(USR|PTY)-[0-9A-HJKMNP-TV-Z]{26}$")
_EVIDENCE_REF = re.compile(r"^EVR-[0-9A-HJKMNP-TV-Z]{26}$")


class AddressMatchRejected(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r})"


class RestrictedEmailEvidenceReader(Protocol):
    def read_authorized(
        self,
        scope: TenantScope,
        evidence_ref: str,
        *,
        caller_ref: str,
        purpose: str,
    ) -> bytes: ...


@dataclass(frozen=True, slots=True, repr=False)
class AddressMatchRequest:
    request_id: str
    site_id: str
    processing_purpose: str
    caller_ref: str
    evidence_ref: str
    address_role: str
    role_index: int
    opaque_address_ref: str
    candidate_target_ref: str
    candidate_target_type: str
    candidate_address: str

    def __post_init__(self) -> None:
        for value in (
            self.request_id,
            self.site_id,
            self.processing_purpose,
            self.caller_ref,
            self.evidence_ref,
            self.opaque_address_ref,
            self.candidate_target_ref,
        ):
            if not isinstance(value, str) or not value or len(value) > 512:
                raise ValueError("invalid address match request")
        if self.processing_purpose != EMAIL_ADDRESS_MATCH_PURPOSE:
            raise ValueError("invalid address match processing purpose")
        if self.address_role not in _ROLES:
            raise ValueError("invalid address role")
        if isinstance(self.role_index, bool) or not 0 <= self.role_index <= 999:
            raise ValueError("invalid role index")
        if self.candidate_target_type not in _TARGET_TYPES:
            raise ValueError("invalid candidate target type")
        target_match = _TARGET_REF.fullmatch(self.candidate_target_ref)
        expected_prefix = "USR" if self.candidate_target_type == "User" else "PTY"
        if target_match is None or target_match.group(1) != expected_prefix:
            raise ValueError("candidate target ref/type mismatch")
        if _OPAQUE_ADDRESS_REF.fullmatch(self.opaque_address_ref) is None:
            raise ValueError("invalid opaque address ref")
        if _EVIDENCE_REF.fullmatch(self.evidence_ref) is None:
            raise ValueError("invalid email evidence ref")
        if not isinstance(self.candidate_address, str) or not self.candidate_address:
            raise ValueError("invalid candidate address")

    def to_wire(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "site_id": self.site_id,
            "processing_purpose": self.processing_purpose,
            "caller_ref": self.caller_ref,
            "evidence_ref": self.evidence_ref,
            "address_role": self.address_role,
            "role_index": self.role_index,
            "opaque_address_ref": self.opaque_address_ref,
            "candidate_target_ref": self.candidate_target_ref,
            "candidate_target_type": self.candidate_target_type,
            "candidate_address": self.candidate_address,
        }

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(request_id={self.request_id!r}, "
            f"site_id={self.site_id!r}, caller_ref={self.caller_ref!r}, "
            f"processing_purpose={self.processing_purpose!r}, "
            f"address_role={self.address_role!r}, "
            f"role_index={self.role_index}, candidate_address="
            f"<redacted chars={len(self.candidate_address)}>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class EmailAddressMatchAttestation:
    attestation_id: str
    site_id: str
    processing_purpose: str
    opaque_address_ref: str
    candidate_target_ref: str
    candidate_target_type: str
    evidence_ref: str
    address_role: str
    normalization_version: str
    matched: bool
    observed_at: datetime
    expires_at: datetime
    digest: str

    def to_wire(self) -> dict[str, object]:
        return {
            "opaque_address_ref": self.opaque_address_ref,
            "candidate_target_ref": self.candidate_target_ref,
            "candidate_target_type": self.candidate_target_type,
            "evidence_ref": self.evidence_ref,
            "normalization_version": self.normalization_version,
            "matched": self.matched,
            "observed_at": self.observed_at.isoformat().replace("+00:00", "Z"),
            "expires_at": self.expires_at.isoformat().replace("+00:00", "Z"),
            "digest": self.digest,
        }

    def to_payload(self) -> dict[str, object]:
        return self.to_wire()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(attestation_id={self.attestation_id!r}, "
            f"site_id={self.site_id!r}, address_role={self.address_role!r}, "
            f"matched={self.matched!r}, expires_at={self.expires_at.isoformat()!r}, "
            f"digest={self.digest!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class EmailAddressMatchResponse:
    attestation_ref: str
    attestation: EmailAddressMatchAttestation

    def __post_init__(self) -> None:
        if re.fullmatch(r"EMA-[0-9A-HJKMNP-TV-Z]{26}", self.attestation_ref) is None:
            raise ValueError("invalid email address match attestation ref")
        if not isinstance(self.attestation, EmailAddressMatchAttestation):
            raise TypeError("invalid email address match attestation")
        if self.attestation.attestation_id != self.attestation_ref:
            raise ValueError("email address match attestation ref mismatch")

    def to_wire(self) -> dict[str, object]:
        return {
            "attestation_ref": self.attestation_ref,
            "attestation": self.attestation.to_wire(),
        }

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(attestation_ref={self.attestation_ref!r}, "
            f"matched={self.attestation.matched!r}, "
            f"expires_at={self.attestation.expires_at.isoformat()!r})"
        )


class EmailAddressMatchService:
    __slots__ = (
        "_allowed_caller_ref",
        "_clock",
        "_evidence_reader",
        "_ledger",
        "_required_purpose",
        "_signing_key",
        "_ttl_seconds",
    )

    def __init__(
        self,
        *,
        evidence_reader: RestrictedEmailEvidenceReader,
        signing_key: bytes,
        allowed_caller_ref: str,
        clock: Callable[[], datetime],
        ttl_seconds: int = 300,
    ) -> None:
        if not callable(getattr(evidence_reader, "read_authorized", None)):
            raise TypeError("invalid evidence reader")
        if not isinstance(signing_key, bytes) or len(signing_key) < 32:
            raise ValueError("invalid signing key")
        if not allowed_caller_ref:
            raise ValueError("address match authority must be closed")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if isinstance(ttl_seconds, bool) or not 1 <= ttl_seconds <= 900:
            raise ValueError("invalid attestation ttl")
        self._evidence_reader = evidence_reader
        self._signing_key = bytes(signing_key)
        self._allowed_caller_ref = allowed_caller_ref
        self._required_purpose = EMAIL_ADDRESS_MATCH_PURPOSE
        self._clock = clock
        self._ttl_seconds = ttl_seconds
        self._ledger: dict[str, tuple[str, EmailAddressMatchResponse]] = {}

    def attest(self, request: AddressMatchRequest) -> EmailAddressMatchResponse:
        if not isinstance(request, AddressMatchRequest):
            raise TypeError("invalid address match request")
        if request.caller_ref != self._allowed_caller_ref:
            raise AddressMatchRejected("caller_forbidden")
        if request.processing_purpose != self._required_purpose:
            raise AddressMatchRejected("purpose_forbidden")
        try:
            scope = TenantScope(request.site_id, request.processing_purpose)
        except ValueError:
            raise AddressMatchRejected("site_or_purpose_invalid") from None
        request_digest = self._request_digest(request)
        existing = self._ledger.get(request.request_id)
        if existing is not None:
            if not hmac.compare_digest(existing[0], request_digest):
                raise AddressMatchRejected("request_replay_drift")
            return existing[1]
        try:
            exact_bytes = self._evidence_reader.read_authorized(
                scope,
                request.evidence_ref,
                caller_ref=request.caller_ref,
                purpose=self._required_purpose,
            )
        except Exception:
            raise AddressMatchRejected("evidence_unavailable") from None
        evidence_address = self._selected_address(
            exact_bytes,
            role=request.address_role,
            role_index=request.role_index,
        )
        try:
            normalized_evidence = normalize_identity_subject("email", evidence_address)
            normalized_candidate = normalize_identity_subject("email", request.candidate_address)
        except IdentityTokenError:
            raise AddressMatchRejected("address_invalid") from None
        evidence_digest = hmac.new(
            self._signing_key, normalized_evidence.encode(), hashlib.sha256
        ).digest()
        candidate_digest = hmac.new(
            self._signing_key, normalized_candidate.encode(), hashlib.sha256
        ).digest()
        matched = hmac.compare_digest(evidence_digest, candidate_digest)
        now = self._clock()
        _require_aware(now, "clock")
        expires_at = now + timedelta(seconds=self._ttl_seconds)
        safe_material = "\x1f".join(
            (
                request_digest,
                str(matched).lower(),
                now.isoformat(),
                expires_at.isoformat(),
            )
        )
        digest = (
            "sha256:"
            + hmac.new(self._signing_key, safe_material.encode(), hashlib.sha256).hexdigest()
        )
        attestation = EmailAddressMatchAttestation(
            attestation_id="EMA-" + stable_ulid("email-address-match", digest),
            site_id=request.site_id,
            processing_purpose=request.processing_purpose,
            opaque_address_ref=request.opaque_address_ref,
            candidate_target_ref=request.candidate_target_ref,
            candidate_target_type=request.candidate_target_type,
            evidence_ref=request.evidence_ref,
            address_role=request.address_role,
            normalization_version=EMAIL_ADDRESS_NORMALIZATION_VERSION,
            matched=matched,
            observed_at=now,
            expires_at=expires_at,
            digest=digest,
        )
        response = EmailAddressMatchResponse(
            attestation_ref=attestation.attestation_id,
            attestation=attestation,
        )
        self._ledger[request.request_id] = (request_digest, response)
        return response

    @staticmethod
    def require_current(attestation: EmailAddressMatchAttestation, *, now: datetime) -> None:
        _require_aware(now, "now")
        if now >= attestation.expires_at:
            raise AddressMatchRejected("attestation_expired")

    @staticmethod
    def _selected_address(exact_bytes: bytes, *, role: str, role_index: int) -> str:
        if not isinstance(exact_bytes, bytes) or len(exact_bytes) > 100_000_000:
            raise AddressMatchRejected("evidence_invalid")
        try:
            message = BytesParser(policy=policy.default).parsebytes(exact_bytes)
            headers = message.get_all(role, [])
            addresses: list[str] = []
            for header in headers:
                if not isinstance(header, AddressHeader) or getattr(header, "defects", False):
                    raise AddressMatchRejected("evidence_invalid")
                addresses.extend(value.addr_spec for value in header.addresses)
        except AddressMatchRejected:
            raise
        except Exception:
            raise AddressMatchRejected("evidence_invalid") from None
        if role_index >= len(addresses):
            raise AddressMatchRejected("address_role_mismatch")
        return addresses[role_index]

    @staticmethod
    def _request_digest(request: AddressMatchRequest) -> str:
        safe = {
            "request_id": request.request_id,
            "site_id": request.site_id,
            "processing_purpose": request.processing_purpose,
            "caller_ref": request.caller_ref,
            "evidence_ref": request.evidence_ref,
            "address_role": request.address_role,
            "role_index": request.role_index,
            "opaque_address_ref": request.opaque_address_ref,
            "candidate_target_ref": request.candidate_target_ref,
            "candidate_target_type": request.candidate_target_type,
            "candidate_address_digest": hashlib.sha256(
                request.candidate_address.casefold().encode()
            ).hexdigest(),
        }
        encoded = json.dumps(safe, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(allowed_caller_ref={self._allowed_caller_ref!r}, "
            f"required_purpose={self._required_purpose!r}, signing_key=<redacted>, "
            f"request_count={len(self._ledger)})"
        )


__all__ = [
    "AddressMatchRejected",
    "AddressMatchRequest",
    "EMAIL_ADDRESS_MATCH_PURPOSE",
    "EMAIL_ADDRESS_NORMALIZATION_VERSION",
    "EmailAddressMatchAttestation",
    "EmailAddressMatchResponse",
    "EmailAddressMatchService",
    "RestrictedEmailEvidenceReader",
]
