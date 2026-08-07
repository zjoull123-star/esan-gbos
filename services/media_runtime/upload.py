from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

_OBJECT_REFERENCE = re.compile(
    r"^(?:object|localobject)://[A-Za-z0-9][A-Za-z0-9.-]{0,139}/[A-Za-z0-9_-]{1,256}$"
)
_EVIDENCE_REFERENCE = re.compile(
    r"^evidence://[A-Za-z0-9][A-Za-z0-9.-]{0,139}/[A-Za-z0-9_-]{1,256}$"
)


class SourceKind(StrEnum):
    PHONE = "phone"
    MEETING = "meeting"
    FILE = "file"


class UploadRejected(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.code!r})"


@dataclass(frozen=True, slots=True)
class UploadBinding:
    site_id: str
    purpose: str
    source_kind: SourceKind
    request_id: str
    declared_size: int

    def __post_init__(self) -> None:
        if not self.site_id:
            raise ValueError("site_id_required")
        if not self.purpose:
            raise ValueError("purpose_required")
        if not self.request_id:
            raise ValueError("request_id_required")
        if self.declared_size < 0:
            raise ValueError("declared_size_invalid")


@dataclass(frozen=True, slots=True)
class UploadRequest:
    binding: UploadBinding
    credential: str = field(repr=False)
    media_type: str
    filename_metadata_ref: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.credential:
            raise ValueError("credential_required")
        if not self.media_type or "/" not in self.media_type:
            raise ValueError("media_type_invalid")
        if self.filename_metadata_ref is not None and not self.filename_metadata_ref.startswith(
            "localmeta://"
        ):
            raise ValueError("filename_metadata_ref_invalid")


@dataclass(frozen=True, slots=True)
class StoredUploadRefs:
    object_ref: str
    evidence_ref: str


@dataclass(frozen=True, slots=True)
class UploadReceipt:
    receipt_id: str
    site_id: str
    purpose: str
    request_id: str
    source_kind: SourceKind
    media_type: str
    byte_size: int
    sha256: str
    object_ref: str
    evidence_ref: str
    received_at: datetime
    immutable_checksum: str
    retention_days: int = 30
    consent_basis: str = "pilot_deferred_review"

    def to_contract(self) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "receipt_id": self.receipt_id,
            "site_id": self.site_id,
            "purpose": self.purpose,
            "request_id": self.request_id,
            "source_kind": self.source_kind.value,
            "media_type": self.media_type,
            "byte_size": self.byte_size,
            "sha256": self.sha256,
            "object_ref": self.object_ref,
            "evidence_ref": self.evidence_ref,
            "received_at": _rfc3339(self.received_at),
            "retention_days": self.retention_days,
            "consent_basis": self.consent_basis,
            "immutable_checksum": self.immutable_checksum,
        }


class UploadVerifier(Protocol):
    def verify(self, credential: str, binding: UploadBinding) -> bool: ...


class TemporaryUpload(Protocol):
    def write(self, chunk: bytes) -> None: ...

    def finalize(self, *, sha256: str, byte_size: int, media_type: str) -> StoredUploadRefs: ...

    def abort(self) -> None: ...


class TemporarySink(Protocol):
    def open(self, binding: UploadBinding, *, idempotency_key: str) -> TemporaryUpload: ...


class UploadService:
    def __init__(
        self,
        *,
        verifier: UploadVerifier,
        temporary_sink: TemporarySink,
        clock: Callable[[], datetime],
        receipt_id_factory: Callable[[], str],
        max_bytes: int,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes_invalid")
        self._verifier = verifier
        self._temporary_sink = temporary_sink
        self._clock = clock
        self._receipt_id_factory = receipt_id_factory
        self._max_bytes = max_bytes

    def receive(self, request: UploadRequest, chunks: Iterable[bytes]) -> UploadReceipt:
        binding = request.binding
        try:
            authenticated = self._verifier.verify(request.credential, binding)
        except Exception:
            raise UploadRejected("authentication_failed") from None
        if not authenticated:
            raise UploadRejected("authentication_failed")
        if binding.declared_size > self._max_bytes:
            raise UploadRejected("size_limit_exceeded")

        handle = self._open_temporary(binding)
        digest = hashlib.sha256()
        byte_size = 0
        try:
            for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise UploadRejected("invalid_chunk")
                byte_size += len(chunk)
                if byte_size > binding.declared_size or byte_size > self._max_bytes:
                    raise UploadRejected("size_exceeded")
                handle.write(chunk)
                digest.update(chunk)
        except UploadRejected:
            _safe_abort(handle)
            raise
        except Exception:
            _safe_abort(handle)
            raise UploadRejected("stream_interrupted") from None
        if byte_size != binding.declared_size:
            _safe_abort(handle)
            raise UploadRejected("size_mismatch")

        sha256 = digest.hexdigest()
        try:
            received_at = self._clock()
            receipt_id = self._receipt_id_factory()
        except Exception:
            _safe_abort(handle)
            raise UploadRejected("receipt_metadata_failure") from None
        if received_at.tzinfo is None or received_at.utcoffset() is None:
            _safe_abort(handle)
            raise UploadRejected("clock_invalid")
        if not receipt_id:
            _safe_abort(handle)
            raise UploadRejected("receipt_id_invalid")
        try:
            refs = handle.finalize(
                sha256=sha256,
                byte_size=byte_size,
                media_type=request.media_type,
            )
        except Exception:
            _safe_abort(handle)
            raise UploadRejected("storage_failure") from None
        if (
            not isinstance(refs, StoredUploadRefs)
            or _OBJECT_REFERENCE.fullmatch(refs.object_ref) is None
            or _EVIDENCE_REFERENCE.fullmatch(refs.evidence_ref) is None
        ):
            _safe_abort(handle)
            raise UploadRejected("storage_reference_invalid")

        checksum = _receipt_checksum(
            receipt_id=receipt_id,
            binding=binding,
            media_type=request.media_type,
            byte_size=byte_size,
            sha256=sha256,
            refs=refs,
            received_at=received_at,
        )
        return UploadReceipt(
            receipt_id=receipt_id,
            site_id=binding.site_id,
            purpose=binding.purpose,
            request_id=binding.request_id,
            source_kind=binding.source_kind,
            media_type=request.media_type,
            byte_size=byte_size,
            sha256=sha256,
            object_ref=refs.object_ref,
            evidence_ref=refs.evidence_ref,
            received_at=received_at,
            immutable_checksum=checksum,
        )

    def _open_temporary(self, binding: UploadBinding) -> TemporaryUpload:
        document = "\x1f".join(
            (
                "authenticated-stream-upload-v1",
                binding.site_id,
                binding.purpose,
                binding.source_kind.value,
                binding.request_id,
                str(binding.declared_size),
            )
        )
        idempotency_key = f"upload:{hashlib.sha256(document.encode()).hexdigest()}"
        try:
            return self._temporary_sink.open(binding, idempotency_key=idempotency_key)
        except Exception:
            raise UploadRejected("temporary_sink_unavailable") from None


def _receipt_checksum(
    *,
    receipt_id: str,
    binding: UploadBinding,
    media_type: str,
    byte_size: int,
    sha256: str,
    refs: StoredUploadRefs,
    received_at: datetime,
) -> str:
    document = {
        "checksum_version": "upload-receipt-v1",
        "receipt_id": receipt_id,
        "site_id": binding.site_id,
        "purpose": binding.purpose,
        "request_id": binding.request_id,
        "source_kind": binding.source_kind.value,
        "media_type": media_type,
        "byte_size": byte_size,
        "sha256": sha256,
        "object_ref": refs.object_ref,
        "evidence_ref": refs.evidence_ref,
        "received_at": _rfc3339(received_at),
        "retention_days": 30,
        "consent_basis": "pilot_deferred_review",
    }
    encoded = json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _safe_abort(handle: TemporaryUpload) -> None:
    with suppress(Exception):
        handle.abort()
