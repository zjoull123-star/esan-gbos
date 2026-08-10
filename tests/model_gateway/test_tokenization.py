from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from services.model_gateway.tokenization import (
    EncryptedFileMappingVault,
    InMemoryMappingVault,
    PiiResidualError,
    StableTokenizer,
    contains_obvious_pii,
)

NOW = datetime(2026, 8, 7, 2, 0, tzinfo=UTC)
KEY = b"k" * 32


class RecordingCipher:
    algorithm = "test-authenticated-cipher"

    def seal(self, plaintext: bytes, *, associated_data: bytes) -> bytes:
        return b"sealed:" + associated_data + b":" + plaintext[::-1]

    def open(self, ciphertext: bytes, *, associated_data: bytes) -> bytes:
        prefix = b"sealed:" + associated_data + b":"
        if not ciphertext.startswith(prefix):
            raise ValueError("authentication failed")
        return ciphertext.removeprefix(prefix)[::-1]


def tokenizer(vault: InMemoryMappingVault | EncryptedFileMappingVault) -> StableTokenizer:
    return StableTokenizer(hmac_key=KEY, vault=vault)


def test_tokens_are_stable_within_site_and_purpose_but_unlinkable_across_scopes() -> None:
    source = "Contact Alice at alice@example.com or +86 138 0013 8000 for Acme Trading."

    same_a = tokenizer(InMemoryMappingVault()).tokenize(
        source,
        site_id="gbos.localhost",
        purpose="sales_follow_up",
        phrases=("Alice", "Acme Trading"),
        now=NOW,
    )
    same_b = tokenizer(InMemoryMappingVault()).tokenize(
        source,
        site_id="gbos.localhost",
        purpose="sales_follow_up",
        phrases=("Alice", "Acme Trading"),
        now=NOW,
    )
    other_site = tokenizer(InMemoryMappingVault()).tokenize(
        source,
        site_id="other.localhost",
        purpose="sales_follow_up",
        phrases=("Alice", "Acme Trading"),
        now=NOW,
    )
    other_purpose = tokenizer(InMemoryMappingVault()).tokenize(
        source,
        site_id="gbos.localhost",
        purpose="customer_service",
        phrases=("Alice", "Acme Trading"),
        now=NOW,
    )

    assert same_a.text == same_b.text
    assert same_a.text != other_site.text
    assert same_a.text != other_purpose.text
    for plaintext in ("Alice", "alice@example.com", "+86 138 0013 8000", "Acme Trading"):
        assert plaintext not in same_a.text


def test_receipt_matches_contract_shape_and_contains_no_plaintext() -> None:
    result = tokenizer(InMemoryMappingVault()).tokenize(
        "alice@example.com / +1 (415) 555-0199",
        site_id="gbos.localhost",
        purpose="sales_follow_up",
        now=NOW,
        expires_at=NOW + timedelta(hours=12),
    )

    receipt = result.receipt.as_dict()
    serialized = json.dumps(receipt, sort_keys=True)
    assert receipt["schema_version"] == "1.0"
    assert receipt["tokenizer_version"] == "stable-hmac-tokenizer-v1"
    assert receipt["source_token_count"] == 2
    assert receipt["emitted_token_count"] == 2
    assert receipt["mapping_reference"].startswith("vault://token-mappings/")
    assert len(receipt["mapping_digest"]) == 64
    assert "alice@example.com" not in serialized
    assert "415" not in serialized


def test_receipt_defaults_to_thirty_day_retention() -> None:
    result = tokenizer(InMemoryMappingVault()).tokenize(
        "alice@example.com",
        site_id="gbos.localhost",
        purpose="sales_follow_up",
        now=NOW,
    )

    assert result.receipt.expires_at == NOW + timedelta(days=30)


@pytest.mark.parametrize(
    "expires_at",
    [
        NOW - timedelta(seconds=1),
        NOW,
        NOW + timedelta(days=30, seconds=1),
    ],
)
def test_receipt_rejects_expired_or_overlong_retention(expires_at: datetime) -> None:
    with pytest.raises(ValueError, match="expires_at"):
        tokenizer(InMemoryMappingVault()).tokenize(
            "alice@example.com",
            site_id="gbos.localhost",
            purpose="sales_follow_up",
            now=NOW,
            expires_at=expires_at,
        )


def test_receipt_accepts_exactly_thirty_day_retention() -> None:
    result = tokenizer(InMemoryMappingVault()).tokenize(
        "alice@example.com",
        site_id="gbos.localhost",
        purpose="sales_follow_up",
        now=NOW,
        expires_at=NOW + timedelta(days=30),
    )

    assert result.receipt.expires_at == NOW + timedelta(days=30)


def test_obvious_residual_pii_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    active = tokenizer(InMemoryMappingVault())
    monkeypatch.setattr(active, "_replace_detected", lambda text, **_: (text, {}))

    with pytest.raises(PiiResidualError):
        active.tokenize(
            "unredacted@example.com",
            site_id="gbos.localhost",
            purpose="sales_follow_up",
            now=NOW,
        )


@pytest.mark.parametrize("punctuation", [".", ",", ";", ":", "!", "?", ")", "]"])
def test_email_before_sentence_punctuation_is_detected_and_tokenized(
    punctuation: str,
) -> None:
    address = "ada.private@example.invalid"
    source = f"Reply to {address}{punctuation}"

    assert contains_obvious_pii(source) is True
    result = tokenizer(InMemoryMappingVault()).tokenize(
        source,
        site_id="gbos.localhost",
        purpose="observation_processing",
        now=NOW,
    )

    assert address not in result.text
    assert result.text.endswith(punctuation)
    assert result.receipt.source_token_count == 1


def test_encrypted_vault_persists_only_ciphertext_and_is_scope_bound(tmp_path: Path) -> None:
    vault = EncryptedFileMappingVault(root=tmp_path, cipher=RecordingCipher())
    active = tokenizer(vault)
    result = active.tokenize(
        "alice@example.com",
        site_id="gbos.localhost",
        purpose="sales_follow_up",
        now=NOW,
    )

    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    assert b"alice@example.com" not in files[0].read_bytes()
    restored = vault.load(
        result.receipt.mapping_reference,
        site_id="gbos.localhost",
        purpose="sales_follow_up",
    )
    assert "alice@example.com" in restored.values()
    with pytest.raises(PermissionError):
        vault.load(
            result.receipt.mapping_reference,
            site_id="other.localhost",
            purpose="sales_follow_up",
        )
