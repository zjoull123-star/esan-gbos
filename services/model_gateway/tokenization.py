from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

TOKENIZER_VERSION = "stable-hmac-tokenizer-v1"
_EMAIL_PATTERN = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+(?![\w.-])")
_PHONE_PATTERN = re.compile(
    r"(?<!\w)(?:\+\d{1,3}[\s.-]?)?(?:\(?\d{2,4}\)?[\s.-]?){2,3}\d{3,4}(?!\w)"
)


class PiiResidualError(ValueError):
    """Tokenized model input still contains an obvious email or phone."""


class MappingVault(Protocol):
    """Narrow reversible store for token mappings."""

    def store(
        self,
        mapping: Mapping[str, str],
        *,
        record_id: str,
        site_id: str,
        purpose: str,
    ) -> str: ...

    def load(
        self,
        reference: str,
        *,
        site_id: str,
        purpose: str,
    ) -> Mapping[str, str]: ...


class AuthenticatedCipher(Protocol):
    """Adapter for a vetted AEAD implementation supplied by the deployment."""

    algorithm: str

    def seal(self, plaintext: bytes, *, associated_data: bytes) -> bytes: ...

    def open(self, ciphertext: bytes, *, associated_data: bytes) -> bytes: ...


@dataclass(frozen=True, slots=True)
class TokenizationReceipt:
    receipt_id: str
    site_id: str
    purpose: str
    tokenizer_version: str
    mapping_reference: str
    mapping_digest: str
    source_token_count: int
    emitted_token_count: int
    created_at: datetime
    expires_at: datetime

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "receipt_id": self.receipt_id,
            "site_id": self.site_id,
            "purpose": self.purpose,
            "tokenizer_version": self.tokenizer_version,
            "mapping_reference": self.mapping_reference,
            "mapping_digest": self.mapping_digest,
            "source_token_count": self.source_token_count,
            "emitted_token_count": self.emitted_token_count,
            "created_at": _timestamp(self.created_at),
            "expires_at": _timestamp(self.expires_at),
        }


@dataclass(frozen=True, slots=True)
class TokenizationResult:
    text: str
    receipt: TokenizationReceipt


class InMemoryMappingVault:
    """Process-local test and development vault; never emits mappings to receipts."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str, str], dict[str, str]] = {}

    def store(
        self,
        mapping: Mapping[str, str],
        *,
        record_id: str,
        site_id: str,
        purpose: str,
    ) -> str:
        self._records[(record_id, site_id, purpose)] = dict(mapping)
        return f"vault://token-mappings/{record_id}"

    def load(
        self,
        reference: str,
        *,
        site_id: str,
        purpose: str,
    ) -> Mapping[str, str]:
        record_id = _record_id(reference)
        try:
            return dict(self._records[(record_id, site_id, purpose)])
        except KeyError as exc:
            raise PermissionError("mapping reference is absent or outside scope") from exc


class EncryptedFileMappingVault:
    """Persistent vault that stores only deployment-supplied AEAD ciphertext."""

    def __init__(self, *, root: Path, cipher: AuthenticatedCipher) -> None:
        self._root = root
        self._cipher = cipher
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._root, 0o700)

    def store(
        self,
        mapping: Mapping[str, str],
        *,
        record_id: str,
        site_id: str,
        purpose: str,
    ) -> str:
        plaintext = _canonical_json(dict(mapping))
        associated_data = _scope_bytes(record_id, site_id, purpose)
        ciphertext = self._cipher.seal(plaintext, associated_data=associated_data)
        envelope = _canonical_json(
            {
                "version": 1,
                "algorithm": self._cipher.algorithm,
                "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
            }
        )
        path = self._path(record_id)
        path.write_bytes(envelope)
        os.chmod(path, 0o600)
        return f"vault://token-mappings/{record_id}"

    def load(
        self,
        reference: str,
        *,
        site_id: str,
        purpose: str,
    ) -> Mapping[str, str]:
        record_id = _record_id(reference)
        envelope = json.loads(self._path(record_id).read_bytes())
        if envelope.get("algorithm") != self._cipher.algorithm:
            raise ValueError("mapping cipher algorithm mismatch")
        ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
        associated_data = _scope_bytes(record_id, site_id, purpose)
        try:
            plaintext = self._cipher.open(ciphertext, associated_data=associated_data)
        except ValueError as exc:
            raise PermissionError("mapping reference is outside scope") from exc
        decoded = json.loads(plaintext)
        if not isinstance(decoded, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in decoded.items()
        ):
            raise ValueError("invalid encrypted mapping record")
        return decoded

    def _path(self, record_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{64}", record_id):
            raise ValueError("invalid mapping record id")
        return self._root / f"{record_id}.json"


class StableTokenizer:
    """Stable scoped HMAC tokenizer with fail-closed residual PII detection."""

    def __init__(
        self,
        *,
        hmac_key: bytes,
        vault: MappingVault,
        tokenizer_version: str = TOKENIZER_VERSION,
    ) -> None:
        if len(hmac_key) < 32:
            raise ValueError("hmac_key must contain at least 32 bytes")
        self._hmac_key = bytes(hmac_key)
        self._vault = vault
        self.tokenizer_version = tokenizer_version

    def tokenize(
        self,
        text: str,
        *,
        site_id: str,
        purpose: str,
        now: datetime,
        phrases: tuple[str, ...] = (),
        expires_at: datetime | None = None,
    ) -> TokenizationResult:
        if not site_id or not purpose:
            raise ValueError("site_id and purpose are required")
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        created_at = now.astimezone(UTC)
        if expires_at is not None and expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        expiry = (
            expires_at.astimezone(UTC)
            if expires_at is not None
            else created_at + timedelta(days=30)
        )
        if not created_at < expiry <= created_at + timedelta(days=30):
            raise ValueError("expires_at must be after now and no more than 30 days later")
        tokenized, mapping = self._replace_detected(
            text,
            site_id=site_id,
            purpose=purpose,
            phrases=phrases,
        )
        if contains_obvious_pii(tokenized):
            raise PiiResidualError("obvious PII remains after tokenization")
        mapping_bytes = _canonical_json(mapping)
        mapping_digest = hashlib.sha256(mapping_bytes).hexdigest()
        record_seed = _canonical_json(
            {
                "site_id": site_id,
                "purpose": purpose,
                "mapping_digest": mapping_digest,
                "tokenizer_version": self.tokenizer_version,
            }
        )
        record_id = hashlib.sha256(record_seed).hexdigest()
        mapping_reference = self._vault.store(
            mapping,
            record_id=record_id,
            site_id=site_id,
            purpose=purpose,
        )
        receipt_seed = record_seed + _timestamp(created_at).encode("ascii")
        receipt = TokenizationReceipt(
            receipt_id=f"tokenization-{hashlib.sha256(receipt_seed).hexdigest()[:32]}",
            site_id=site_id,
            purpose=purpose,
            tokenizer_version=self.tokenizer_version,
            mapping_reference=mapping_reference,
            mapping_digest=mapping_digest,
            source_token_count=len(mapping),
            emitted_token_count=len(mapping),
            created_at=created_at,
            expires_at=expiry,
        )
        return TokenizationResult(text=tokenized, receipt=receipt)

    def _replace_detected(
        self,
        text: str,
        *,
        site_id: str,
        purpose: str,
        phrases: tuple[str, ...],
    ) -> tuple[str, dict[str, str]]:
        matches: list[tuple[int, int, str, str]] = []
        for pattern, kind in ((_EMAIL_PATTERN, "EMAIL"), (_PHONE_PATTERN, "PHONE")):
            matches.extend(
                (match.start(), match.end(), match.group(0), kind)
                for match in pattern.finditer(text)
            )
        for phrase in sorted(set(phrases), key=len, reverse=True):
            if not phrase:
                continue
            matches.extend(
                (match.start(), match.end(), match.group(0), "ENTITY")
                for match in re.finditer(re.escape(phrase), text, flags=re.IGNORECASE)
            )
        selected: list[tuple[int, int, str, str]] = []
        for candidate in sorted(matches, key=lambda item: (item[0], -(item[1] - item[0]))):
            if any(candidate[0] < item[1] and item[0] < candidate[1] for item in selected):
                continue
            selected.append(candidate)
        mapping: dict[str, str] = {}
        output = text
        for start, end, plaintext, kind in sorted(selected, reverse=True):
            token = self._token(plaintext, kind=kind, site_id=site_id, purpose=purpose)
            mapping[token] = plaintext
            output = output[:start] + token + output[end:]
        return output, mapping

    def _token(self, plaintext: str, *, kind: str, site_id: str, purpose: str) -> str:
        scoped_value = b"\x00".join(
            (
                self.tokenizer_version.encode(),
                site_id.encode(),
                purpose.encode(),
                kind.encode(),
                plaintext.casefold().encode(),
            )
        )
        digest = hmac.new(self._hmac_key, scoped_value, hashlib.sha256).hexdigest()[:24]
        return f"<{kind}_{digest}>"


def contains_obvious_pii(text: str) -> bool:
    return _EMAIL_PATTERN.search(text) is not None or _PHONE_PATTERN.search(text) is not None


def _record_id(reference: str) -> str:
    prefix = "vault://token-mappings/"
    if not reference.startswith(prefix):
        raise ValueError("invalid mapping reference")
    record_id = reference.removeprefix(prefix)
    if not re.fullmatch(r"[a-f0-9]{64}", record_id):
        raise ValueError("invalid mapping reference")
    return record_id


def _scope_bytes(record_id: str, site_id: str, purpose: str) -> bytes:
    return _canonical_json({"record_id": record_id, "site_id": site_id, "purpose": purpose})


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
