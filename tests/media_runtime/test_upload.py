from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

import pytest

from services.media_runtime.upload import (
    SourceKind,
    StoredUploadRefs,
    UploadBinding,
    UploadRejected,
    UploadRequest,
    UploadService,
)


class RecordingVerifier:
    def __init__(self, *, accepted: bool = True, failure: Exception | None = None) -> None:
        self.accepted = accepted
        self.failure = failure
        self.calls: list[tuple[str, UploadBinding]] = []

    def verify(self, credential: str, binding: UploadBinding) -> bool:
        self.calls.append((credential, binding))
        if self.failure is not None:
            raise self.failure
        return self.accepted


class RecordingTemporaryUpload:
    def __init__(self) -> None:
        self.written = bytearray()
        self.aborted = False
        self.finalized: tuple[str, int, str] | None = None

    def write(self, chunk: bytes) -> None:
        self.written.extend(chunk)

    def finalize(self, *, sha256: str, byte_size: int, media_type: str) -> StoredUploadRefs:
        self.finalized = (sha256, byte_size, media_type)
        return StoredUploadRefs(
            object_ref="object://site-a/upload-01",
            evidence_ref="evidence://site-a/upload-01",
        )

    def abort(self) -> None:
        self.aborted = True


class RecordingSink:
    def __init__(self) -> None:
        self.handles: list[RecordingTemporaryUpload] = []
        self.calls: list[tuple[UploadBinding, str]] = []

    def open(self, binding: UploadBinding, *, idempotency_key: str) -> RecordingTemporaryUpload:
        self.calls.append((binding, idempotency_key))
        handle = RecordingTemporaryUpload()
        self.handles.append(handle)
        return handle


def _service(
    verifier: RecordingVerifier | None = None,
    sink: RecordingSink | None = None,
    *,
    max_bytes: int = 32,
) -> tuple[UploadService, RecordingVerifier, RecordingSink]:
    actual_verifier = verifier or RecordingVerifier()
    actual_sink = sink or RecordingSink()
    service = UploadService(
        verifier=actual_verifier,
        temporary_sink=actual_sink,
        clock=lambda: datetime(2026, 8, 7, 3, tzinfo=UTC),
        receipt_id_factory=lambda: "upload-01",
        max_bytes=max_bytes,
    )
    return service, actual_verifier, actual_sink


def _request(
    *,
    declared_size: int = 6,
    credential: str = "credential-SENTINEL",
    source_kind: SourceKind = SourceKind.MEETING,
) -> UploadRequest:
    return UploadRequest(
        binding=UploadBinding(
            site_id="site-a",
            purpose="observation_processing",
            source_kind=source_kind,
            request_id="request-01",
            declared_size=declared_size,
        ),
        credential=credential,
        media_type="audio/wav",
        filename_metadata_ref="localmeta://encrypted/SENTINEL-original.wav",
    )


def test_authenticated_upload_streams_to_temporary_sink_and_returns_content_free_receipt() -> None:
    service, verifier, sink = _service()

    receipt = service.receive(_request(), [b"abc", b"def"])

    assert verifier.calls == [("credential-SENTINEL", _request().binding)]
    assert bytes(sink.handles[0].written) == b"abcdef"
    assert sink.handles[0].finalized == (
        "bef57ec7f53a6d40beb640a780a639c83bc29ac8a9816f1fc6c5c6dcd93c4721",
        6,
        "audio/wav",
    )
    assert receipt.site_id == "site-a"
    assert receipt.source_kind is SourceKind.MEETING
    assert receipt.byte_size == 6
    assert receipt.object_ref == "object://site-a/upload-01"
    assert receipt.evidence_ref == "evidence://site-a/upload-01"
    assert receipt.retention_days == 30
    assert receipt.consent_basis == "pilot_deferred_review"
    assert len(receipt.immutable_checksum) == 64
    assert "abcdef" not in repr(receipt)
    assert "SENTINEL" not in repr(receipt)
    assert set(receipt.to_contract()) == {
        "schema_version",
        "receipt_id",
        "site_id",
        "purpose",
        "request_id",
        "source_kind",
        "media_type",
        "byte_size",
        "sha256",
        "object_ref",
        "evidence_ref",
        "received_at",
        "retention_days",
        "consent_basis",
        "immutable_checksum",
    }


@pytest.mark.parametrize("accepted", (False,))
def test_authentication_failure_is_closed_before_temporary_object_creation(accepted: bool) -> None:
    service, _verifier, sink = _service(RecordingVerifier(accepted=accepted))

    with pytest.raises(UploadRejected, match="authentication_failed") as caught:
        service.receive(_request(), [b"abcdef"])

    assert sink.calls == []
    assert "credential-SENTINEL" not in repr(caught.value)


def test_authenticator_exception_is_redacted_and_closed() -> None:
    sentinel = "credential-SENTINEL leaked by verifier"
    service, _verifier, sink = _service(RecordingVerifier(failure=RuntimeError(sentinel)))

    with pytest.raises(UploadRejected, match="authentication_failed") as caught:
        service.receive(_request(), [b"abcdef"])

    assert sink.calls == []
    assert sentinel not in str(caught.value)
    assert sentinel not in repr(caught.value)


def test_zero_byte_upload_is_rejected_before_temporary_object_creation() -> None:
    service, verifier, sink = _service()

    with pytest.raises(UploadRejected, match="empty_upload"):
        service.receive(_request(declared_size=0), [])

    assert verifier.calls == [("credential-SENTINEL", _request(declared_size=0).binding)]
    assert sink.calls == []


@pytest.mark.parametrize(
    ("declared_size", "chunks", "code"),
    (
        (6, [b"abc"], "size_mismatch"),
        (6, [b"abcdefg"], "size_exceeded"),
        (6, [b"abc", b"defg"], "size_exceeded"),
        (33, [b""], "size_limit_exceeded"),
    ),
)
def test_size_failures_abort_temporary_object(
    declared_size: int,
    chunks: list[bytes],
    code: str,
) -> None:
    service, _verifier, sink = _service()

    with pytest.raises(UploadRejected, match=code):
        service.receive(_request(declared_size=declared_size), chunks)

    if sink.handles:
        assert sink.handles[0].aborted is True
        assert sink.handles[0].finalized is None


def test_interrupted_stream_aborts_and_redacts_underlying_error() -> None:
    def interrupted() -> Iterable[bytes]:
        yield b"abc"
        raise RuntimeError("stream-SENTINEL")

    service, _verifier, sink = _service()

    with pytest.raises(UploadRejected, match="stream_interrupted") as caught:
        service.receive(_request(), interrupted())

    assert sink.handles[0].aborted is True
    assert "stream-SENTINEL" not in repr(caught.value)


def test_finalize_failure_aborts_and_redacts_storage_error() -> None:
    class FailingTemporaryUpload(RecordingTemporaryUpload):
        def finalize(
            self,
            *,
            sha256: str,
            byte_size: int,
            media_type: str,
        ) -> StoredUploadRefs:
            del sha256, byte_size, media_type
            raise RuntimeError("storage-SENTINEL")

    class FailingSink(RecordingSink):
        def open(
            self,
            binding: UploadBinding,
            *,
            idempotency_key: str,
        ) -> RecordingTemporaryUpload:
            self.calls.append((binding, idempotency_key))
            handle = FailingTemporaryUpload()
            self.handles.append(handle)
            return handle

    service, _verifier, sink = _service(sink=FailingSink())

    with pytest.raises(UploadRejected, match="storage_failure") as caught:
        service.receive(_request(), [b"abcdef"])

    assert sink.handles[0].aborted is True
    assert "storage-SENTINEL" not in repr(caught.value)


def test_invalid_clock_aborts_before_promoting_temporary_object() -> None:
    verifier = RecordingVerifier()
    sink = RecordingSink()
    service = UploadService(
        verifier=verifier,
        temporary_sink=sink,
        clock=lambda: datetime(2026, 8, 7, 3),
        receipt_id_factory=lambda: "upload-01",
        max_bytes=32,
    )

    with pytest.raises(UploadRejected, match="clock_invalid"):
        service.receive(_request(), [b"abcdef"])

    assert sink.handles[0].aborted is True
    assert sink.handles[0].finalized is None


def test_unsafe_storage_reference_is_rejected_and_temporary_object_is_aborted() -> None:
    class UnsafeReferenceUpload(RecordingTemporaryUpload):
        def finalize(
            self,
            *,
            sha256: str,
            byte_size: int,
            media_type: str,
        ) -> StoredUploadRefs:
            del sha256, byte_size, media_type
            return StoredUploadRefs(
                object_ref="/tmp/original-SENTINEL.wav",
                evidence_ref="evidence://site-a/upload-01",
            )

    class UnsafeReferenceSink(RecordingSink):
        def open(
            self,
            binding: UploadBinding,
            *,
            idempotency_key: str,
        ) -> RecordingTemporaryUpload:
            self.calls.append((binding, idempotency_key))
            handle = UnsafeReferenceUpload()
            self.handles.append(handle)
            return handle

    service, _verifier, sink = _service(sink=UnsafeReferenceSink())

    with pytest.raises(UploadRejected, match="storage_reference_invalid") as caught:
        service.receive(_request(), [b"abcdef"])

    assert sink.handles[0].aborted is True
    assert "original-SENTINEL.wav" not in repr(caught.value)


def test_non_bytes_chunk_aborts_without_echoing_value() -> None:
    service, _verifier, sink = _service()

    with pytest.raises(UploadRejected, match="invalid_chunk") as caught:
        service.receive(_request(), [b"abc", "raw-SENTINEL"])  # type: ignore[list-item]

    assert sink.handles[0].aborted is True
    assert "raw-SENTINEL" not in repr(caught.value)


def test_request_repr_excludes_credential_and_private_filename_metadata_reference() -> None:
    representation = repr(_request())

    assert "credential-SENTINEL" not in representation
    assert "SENTINEL-original.wav" not in representation


@pytest.mark.parametrize("source_kind", tuple(SourceKind))
def test_only_explicit_phone_meeting_and_file_sources_are_supported(
    source_kind: SourceKind,
) -> None:
    service, _verifier, _sink = _service()

    receipt = service.receive(_request(source_kind=source_kind), [b"abcdef"])

    assert receipt.source_kind is source_kind
