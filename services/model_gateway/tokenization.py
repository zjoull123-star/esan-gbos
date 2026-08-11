from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from services.local_pilot_runtime.secret_provider import (
    MountedFileSecretProvider,
    SecretBytes,
    SecretProviderError,
    SecretSpec,
)

TOKENIZER_VERSION = "stable-hmac-tokenizer-v1"
_EMAIL_PATTERN = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+(?![\w-]|\.[\w-])")
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
        created_at: datetime | None = None,
        expires_at: datetime | None = None,
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


class KeySecretProvider(Protocol):
    """Narrow provider surface required by model cryptographic keys."""

    def read_bytes(self, name: str) -> SecretBytes | None: ...


class _AES256GCMCipher:
    """Small non-exported AES-256-GCM adapter with a fresh nonce per seal."""

    algorithm = "AES-256-GCM"
    _NONCE_BYTES = 12

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("vault master key must contain exactly 32 bytes")
        self._cipher = AESGCM(key)

    def __repr__(self) -> str:
        return "_AES256GCMCipher(algorithm='AES-256-GCM', key=<redacted>)"

    def seal(self, plaintext: bytes, *, associated_data: bytes) -> bytes:
        nonce = os.urandom(self._NONCE_BYTES)
        return nonce + self._cipher.encrypt(nonce, plaintext, associated_data)

    def open(self, ciphertext: bytes, *, associated_data: bytes) -> bytes:
        if len(ciphertext) <= self._NONCE_BYTES:
            raise ValueError("encrypted mapping envelope failed authentication")
        nonce = ciphertext[: self._NONCE_BYTES]
        payload = ciphertext[self._NONCE_BYTES :]
        try:
            return self._cipher.decrypt(nonce, payload, associated_data)
        except InvalidTag as exc:
            raise ValueError("encrypted mapping envelope failed authentication") from exc


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
        created_at: datetime | None = None,
        expires_at: datetime | None = None,
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
    """Persistent authenticated vault with no plaintext mapping index."""

    def __init__(
        self,
        *,
        root: Path,
        cipher: AuthenticatedCipher,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._root = root
        self._cipher = cipher
        self._clock = clock or _utc_now
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._root, 0o700)

    def __repr__(self) -> str:
        return (
            "EncryptedFileMappingVault("
            f"root={self._root!r}, algorithm={self._cipher.algorithm!r}, key=<redacted>)"
        )

    @property
    def algorithm(self) -> str:
        return self._cipher.algorithm

    @classmethod
    def from_key_file(
        cls,
        *,
        root: Path,
        key_file: Path,
        clock: Callable[[], datetime] | None = None,
    ) -> EncryptedFileMappingVault:
        return cls.from_key_bytes(
            root=root,
            key=_read_exact_private_key_file(key_file),
            clock=clock,
        )

    @classmethod
    def from_key_bytes(
        cls,
        *,
        root: Path,
        key: bytes,
        clock: Callable[[], datetime] | None = None,
    ) -> EncryptedFileMappingVault:
        """Build an AES-256 mapping vault from an in-memory domain key."""

        if type(key) is not bytes or len(key) != 32:
            raise ValueError("vault master key must contain exactly 32 bytes")
        return cls(root=root, cipher=_AES256GCMCipher(bytes(key)), clock=clock)

    @classmethod
    def from_secret_provider(
        cls,
        *,
        root: Path,
        provider: KeySecretProvider,
        clock: Callable[[], datetime] | None = None,
    ) -> EncryptedFileMappingVault:
        """Load the AES-256 mapping key from its deployment logical name."""

        key = _provider_key(provider, "mapping_vault_key")
        return cls.from_key_bytes(root=root, key=key, clock=clock)

    def store(
        self,
        mapping: Mapping[str, str],
        *,
        record_id: str,
        site_id: str,
        purpose: str,
        created_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> str:
        created = _aware_utc(created_at or self._clock(), "created_at")
        expiry = _aware_utc(expires_at or created + timedelta(days=30), "expires_at")
        if not created < expiry <= created + timedelta(days=30):
            raise ValueError("expires_at must be after created_at and within 30 days")
        plaintext = _canonical_json(dict(mapping))
        metadata = {
            "version": 2,
            "algorithm": self._cipher.algorithm,
            "record_id": record_id,
            "scope_digest": hashlib.sha256(_scope_bytes(record_id, site_id, purpose)).hexdigest(),
            "created_at": _timestamp(created),
            "expires_at": _timestamp(expiry),
        }
        associated_data = _canonical_json(metadata)
        ciphertext = self._cipher.seal(plaintext, associated_data=associated_data)
        envelope = _canonical_json(
            {**metadata, "ciphertext": base64.b64encode(ciphertext).decode("ascii")}
        )
        path = self._path(record_id)
        _atomic_private_write(path, envelope)
        return f"vault://token-mappings/{record_id}"

    def load(
        self,
        reference: str,
        *,
        site_id: str,
        purpose: str,
    ) -> Mapping[str, str]:
        record_id = _record_id(reference)
        envelope = _load_envelope(self._path(record_id))
        if envelope.get("algorithm") != self._cipher.algorithm:
            raise ValueError("mapping cipher algorithm mismatch")
        version = envelope.get("version")
        if version == 1:
            associated_data = _scope_bytes(record_id, site_id, purpose)
        elif version == 2:
            metadata = _authenticated_metadata(envelope, record_id=record_id)
            expected_scope = hashlib.sha256(_scope_bytes(record_id, site_id, purpose)).hexdigest()
            if metadata["scope_digest"] != expected_scope:
                raise PermissionError("mapping reference is absent or outside scope")
            associated_data = _canonical_json(metadata)
        else:
            raise ValueError("invalid encrypted mapping envelope")
        ciphertext = _envelope_ciphertext(envelope)
        try:
            plaintext = self._cipher.open(ciphertext, associated_data=associated_data)
        except ValueError as exc:
            raise PermissionError("mapping reference is outside scope") from exc
        decoded = _decode_mapping(plaintext)
        if version == 2:
            expiry = _parse_timestamp(str(envelope["expires_at"]), "expires_at")
            if expiry <= _aware_utc(self._clock(), "clock"):
                raise PermissionError("mapping reference has expired")
        return decoded

    def cleanup_expired(self, *, now: datetime | None = None) -> int:
        cutoff = _aware_utc(now or self._clock(), "now")
        removed = 0
        for path in sorted(self._root.glob("*.json")):
            record_id = path.stem
            if re.fullmatch(r"[a-f0-9]{64}", record_id) is None:
                continue
            envelope = _load_envelope(path)
            metadata = _authenticated_metadata(envelope, record_id=record_id)
            ciphertext = _envelope_ciphertext(envelope)
            try:
                plaintext = self._cipher.open(
                    ciphertext,
                    associated_data=_canonical_json(metadata),
                )
            except ValueError as exc:
                raise ValueError(
                    "encrypted mapping retention metadata is not authenticated"
                ) from exc
            _decode_mapping(plaintext)
            expiry = _parse_timestamp(str(metadata["expires_at"]), "expires_at")
            if expiry <= cutoff:
                path.unlink()
                removed += 1
        return removed

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

    @classmethod
    def from_secret_provider(
        cls,
        *,
        provider: KeySecretProvider,
        vault: MappingVault,
        tokenizer_version: str = TOKENIZER_VERSION,
    ) -> StableTokenizer:
        """Load the tokenizer HMAC key from its deployment logical name."""

        key = _provider_key(provider, "tokenizer_hmac_key")
        if len(key) != 32:
            raise ValueError("hmac_key must contain exactly 32 bytes")
        return cls(hmac_key=key, vault=vault, tokenizer_version=tokenizer_version)

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
            created_at=created_at,
            expires_at=expiry,
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


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _parse_timestamp(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid encrypted mapping {name}") from exc
    return _aware_utc(parsed, name)


def _read_exact_private_key_file(path: Path) -> bytes:
    candidate = Path(path)
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise ValueError("vault master key file must be a regular non-symlink file") from exc
    if candidate.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("vault master key file must be a regular non-symlink file")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError("vault master key file permissions must be exactly 0600")
    if metadata.st_size != 32:
        raise ValueError("vault master key file must contain exactly 32 bytes")
    absolute = candidate.absolute()
    try:
        provider = MountedFileSecretProvider(
            absolute.parent,
            (SecretSpec("mapping_vault_key", absolute.name, "bytes", 32, 32, 32),),
        )
        secret = provider.read_bytes("mapping_vault_key")
    except SecretProviderError:
        raise ValueError("vault master key file must be a regular non-symlink file") from None
    if not isinstance(secret, SecretBytes):
        raise ValueError("vault master key file must contain exactly 32 bytes")
    return secret.reveal()


def _provider_key(provider: KeySecretProvider, logical_name: str) -> bytes:
    try:
        secret = provider.read_bytes(logical_name)
    except SecretProviderError:
        raise ValueError("secret provider request failed") from None
    if not isinstance(secret, SecretBytes):
        raise ValueError("secret provider request failed")
    return secret.reveal()


def _atomic_private_write(path: Path, value: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.urandom(8).hex()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        os.write(descriptor, value)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_envelope(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid encrypted mapping envelope") from exc
    if not isinstance(value, dict):
        raise ValueError("invalid encrypted mapping envelope")
    return value


def _authenticated_metadata(
    envelope: Mapping[str, object],
    *,
    record_id: str,
) -> dict[str, object]:
    metadata = {
        "version": envelope.get("version"),
        "algorithm": envelope.get("algorithm"),
        "record_id": envelope.get("record_id"),
        "scope_digest": envelope.get("scope_digest"),
        "created_at": envelope.get("created_at"),
        "expires_at": envelope.get("expires_at"),
    }
    if (
        metadata["version"] != 2
        or metadata["record_id"] != record_id
        or not isinstance(metadata["algorithm"], str)
        or not isinstance(metadata["scope_digest"], str)
        or re.fullmatch(r"[a-f0-9]{64}", metadata["scope_digest"]) is None
        or not isinstance(metadata["created_at"], str)
        or not isinstance(metadata["expires_at"], str)
    ):
        raise ValueError("invalid encrypted mapping envelope")
    _parse_timestamp(metadata["created_at"], "created_at")
    _parse_timestamp(metadata["expires_at"], "expires_at")
    return metadata


def _envelope_ciphertext(envelope: Mapping[str, object]) -> bytes:
    encoded = envelope.get("ciphertext")
    if not isinstance(encoded, str):
        raise ValueError("invalid encrypted mapping envelope")
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid encrypted mapping envelope") from exc


def _decode_mapping(plaintext: bytes) -> dict[str, str]:
    try:
        decoded = json.loads(plaintext)
    except json.JSONDecodeError, UnicodeDecodeError:
        raise ValueError("invalid encrypted mapping record") from None
    if not isinstance(decoded, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in decoded.items()
    ):
        raise ValueError("invalid encrypted mapping record")
    return decoded
