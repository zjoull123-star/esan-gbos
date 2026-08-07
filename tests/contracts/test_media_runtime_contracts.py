from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

ROOT = Path(__file__).parents[2]
LOCAL_PILOT = ROOT / "contracts" / "local_pilot"


def _validator(filename: str) -> Draft202012Validator:
    path = LOCAL_PILOT / filename
    assert path.exists(), f"missing local-pilot media contract: {filename}"
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _upload_receipt() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "receipt_id": "upload_01",
        "site_id": "site-a",
        "purpose": "observation_processing",
        "request_id": "req-01",
        "source_kind": "meeting",
        "media_type": "audio/wav",
        "byte_size": 12,
        "sha256": "a" * 64,
        "object_ref": "object://site-a/upload_01",
        "evidence_ref": "evidence://site-a/upload_01",
        "received_at": "2026-08-07T03:00:00Z",
        "retention_days": 30,
        "consent_basis": "pilot_deferred_review",
        "immutable_checksum": "b" * 64,
    }


def _transcript() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "transcript_id": "transcript_01",
        "site_id": "site-a",
        "source_evidence_ref": "evidence://site-a/upload_01",
        "model": {
            "provider": "local_faster_whisper",
            "name": "large-v3-turbo",
            "version": "large-v3-turbo-ct2-local-v1",
            "sha256": hashlib.sha256(b"bound-test-model-artifact").hexdigest(),
        },
        "language": "en",
        "segments": [
            {
                "segment_id": "segment_000001",
                "start_ms": 0,
                "end_ms": 1250,
                "speaker": "Speaker 1",
                "confidence": 0.92,
                "text_ref": "localtext://site-a/transcript_01/segment_000001",
                "evidence_ref": "evidence://site-a/upload_01/t/0-1250",
            }
        ],
        "generated_at": "2026-08-07T03:01:00Z",
    }


def test_upload_receipt_schema_is_strict_2020_12_and_accepts_reference_only_payload() -> None:
    validator = _validator("upload-receipt-v1.0.schema.json")
    validator.validate(_upload_receipt())


def test_upload_receipt_rejects_zero_byte_payload() -> None:
    with pytest.raises(ValidationError):
        _validator("upload-receipt-v1.0.schema.json").validate(
            {**_upload_receipt(), "byte_size": 0}
        )


@pytest.mark.parametrize(
    "field",
    (
        "path",
        "token",
        "credential",
        "original_filename",
        "filename",
        "raw_bytes",
        "content",
        "plaintext",
    ),
)
def test_upload_receipt_rejects_paths_secrets_filenames_and_content(field: str) -> None:
    with pytest.raises(ValidationError):
        _validator("upload-receipt-v1.0.schema.json").validate(
            {**_upload_receipt(), field: "sentinel-secret-original.wav"}
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("retention_days", 29),
        ("retention_days", 31),
        ("consent_basis", "consent_obtained"),
        ("source_kind", "synthetic"),
        ("sha256", "not-a-digest"),
        ("immutable_checksum", "not-a-digest"),
    ),
)
def test_upload_receipt_enforces_fixed_pilot_security_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        _validator("upload-receipt-v1.0.schema.json").validate({**_upload_receipt(), field: value})


def test_transcript_segments_schema_is_strict_2020_12_and_accepts_reference_only_text() -> None:
    validator = _validator("transcript-segments-v1.0.schema.json")
    validator.validate(_transcript())


@pytest.mark.parametrize(
    "field",
    (
        "contact_id",
        "contact_mapping",
        "real_name",
        "prompt",
        "token",
        "plaintext",
        "text",
    ),
)
def test_transcript_rejects_identity_prompt_token_and_plaintext_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        _validator("transcript-segments-v1.0.schema.json").validate(
            {**_transcript(), field: "sentinel-person-or-secret"}
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("speaker", "Alice"),
        ("confidence", 1.01),
        ("start_ms", -1),
        ("text", "raw transcript"),
        ("contact_id", "contact-1"),
    ),
)
def test_transcript_segment_rejects_unsafe_or_unbounded_values(field: str, value: object) -> None:
    transcript = _transcript()
    segment = {**transcript["segments"][0], field: value}
    with pytest.raises(ValidationError):
        _validator("transcript-segments-v1.0.schema.json").validate(
            {**transcript, "segments": [segment]}
        )


def test_transcript_non_finite_confidence_is_not_valid_json() -> None:
    transcript = _transcript()
    segment = {**transcript["segments"][0], "confidence": float("nan")}

    with pytest.raises(ValueError, match="Out of range float values"):
        json.dumps({**transcript, "segments": [segment]}, allow_nan=False)


def test_transcript_schema_fixes_local_whisper_model_identity() -> None:
    validator = _validator("transcript-segments-v1.0.schema.json")
    transcript = _transcript()
    for model_change in (
        {"name": "small"},
        {"provider": "remote"},
        {"version": "runtime-selected"},
        {"sha256": "not-a-digest"},
        {"sha256": "c" * 64},
        {"sha256": "0123456789abcdef" * 4},
    ):
        with pytest.raises(ValidationError):
            validator.validate(
                {
                    **transcript,
                    "model": {**transcript["model"], **model_change},
                }
            )


def test_transcript_schema_records_any_bound_non_placeholder_artifact_sha() -> None:
    transcript = _transcript()
    second_bound_sha = hashlib.sha256(b"second-bound-test-model-artifact").hexdigest()

    _validator("transcript-segments-v1.0.schema.json").validate(
        {
            **transcript,
            "model": {**transcript["model"], "sha256": second_bound_sha},
        }
    )
