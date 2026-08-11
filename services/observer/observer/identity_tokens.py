"""Provider-scoped opaque participant identities for the Observer boundary."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import stat
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from services.local_pilot_runtime.secret_provider import (
    MountedFileSecretProvider,
    SecretBytes,
    SecretProviderError,
    SecretSpec,
)

IDENTITY_PROVIDERS = frozenset({"email", "wecom", "whatsapp", "phone", "manual_import"})

_SAFE_CODE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,79}$")
_SITE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,139}$")
_PURPOSE = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")
_EMAIL_LOCAL = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}$")
_DOMAIN_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_WHATSAPP = re.compile(r"^[1-9][0-9]{4,14}$")
_WECOM = re.compile(r"^[A-Za-z0-9._@=-]{1,128}$")
_PHONE = re.compile(r"^\+?[1-9][0-9]{6,14}$")
_MANUAL_IMPORT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~=-]{0,255}$")
_MINIMUM_KEY_BYTES = 32
_MAXIMUM_KEY_BYTES = 4096


class IdentityTokenError(ValueError):
    """Safe identity-token failure which never renders secret or subject data."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if _SAFE_CODE.fullmatch(code) is None:
            raise ValueError("invalid identity token error code")
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"IdentityTokenError(code={self.code!r})"


class IdentityTokenResolver(Protocol):
    """Resolve one trusted provider subject to a stable opaque identity reference."""

    def resolve(
        self,
        site_id: str,
        purpose: str,
        provider: str,
        subject: str,
    ) -> str: ...


class IdentitySecretProvider(Protocol):
    """Narrow provider surface required by identity HMAC loading."""

    def read_bytes(self, name: str) -> SecretBytes | None: ...


@dataclass(frozen=True, slots=True, repr=False)
class TransientIdentitySubject:
    """In-memory provider subject discarded immediately after token derivation."""

    provider: str
    subject: str

    def __post_init__(self) -> None:
        if self.provider not in IDENTITY_PROVIDERS:
            raise IdentityTokenError("identity_token.unknown_provider")
        if not isinstance(self.subject, str) or not self.subject:
            raise IdentityTokenError("identity_token.invalid_subject")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(provider={self.provider!r}, "
            f"subject=<redacted chars={len(self.subject)}>)"
        )


def normalize_identity_subject(provider: str, subject: str) -> str:
    """Return a deterministic provider-specific subject or fail with a safe code."""

    if provider not in IDENTITY_PROVIDERS:
        raise IdentityTokenError("identity_token.unknown_provider")
    if not isinstance(subject, str):
        raise IdentityTokenError("identity_token.invalid_subject")

    if provider == "email":
        normalized = subject.strip().casefold()
        if (
            len(normalized.encode("utf-8")) > 254
            or normalized.count("@") != 1
            or any(ord(character) < 33 or ord(character) == 127 for character in normalized)
        ):
            raise IdentityTokenError("identity_token.invalid_subject")
        local, domain = normalized.rsplit("@", 1)
        labels = domain.split(".")
        if (
            _EMAIL_LOCAL.fullmatch(local) is None
            or not domain
            or len(domain) > 253
            or any(_DOMAIN_LABEL.fullmatch(label) is None for label in labels)
        ):
            raise IdentityTokenError("identity_token.invalid_subject")
        return normalized

    if subject != subject.strip() or "\x00" in subject:
        raise IdentityTokenError("identity_token.invalid_subject")
    pattern = {
        "whatsapp": _WHATSAPP,
        "wecom": _WECOM,
        "phone": _PHONE,
        "manual_import": _MANUAL_IMPORT,
    }[provider]
    if pattern.fullmatch(subject) is None:
        raise IdentityTokenError("identity_token.invalid_subject")
    return subject


class HmacSha256IdentityTokenResolver:
    """Production HMAC-SHA256 resolver with a non-renderable in-memory key."""

    __slots__ = ("_key",)

    def __init__(self, key: bytes) -> None:
        self._key = _validated_key(key)

    @classmethod
    def from_secret_file(
        cls,
        path: str | os.PathLike[str],
    ) -> HmacSha256IdentityTokenResolver:
        try:
            path_value = Path(path)
            path_metadata = os.lstat(path_value)
            if stat.S_ISLNK(path_metadata.st_mode) or not stat.S_ISREG(path_metadata.st_mode):
                raise IdentityTokenError("identity_token.invalid_secret_file")
        except IdentityTokenError:
            raise
        except OSError, TypeError, ValueError:
            raise IdentityTokenError("identity_token.invalid_secret_file") from None

        if stat.S_IMODE(path_metadata.st_mode) not in {0o400, 0o600}:
            raise IdentityTokenError("identity_token.invalid_secret_mode")
        if not _MINIMUM_KEY_BYTES <= path_metadata.st_size <= _MAXIMUM_KEY_BYTES:
            raise IdentityTokenError("identity_token.invalid_secret_size")
        absolute = path_value.absolute()
        try:
            provider = MountedFileSecretProvider(
                absolute.parent,
                (
                    SecretSpec(
                        "identity_hmac_key",
                        absolute.name,
                        "bytes",
                        _MINIMUM_KEY_BYTES,
                        _MAXIMUM_KEY_BYTES,
                    ),
                ),
            )
        except SecretProviderError:
            raise IdentityTokenError("identity_token.invalid_secret_file") from None
        try:
            secret = provider.read_bytes("identity_hmac_key")
        except SecretProviderError:
            raise IdentityTokenError("identity_token.invalid_secret_file") from None
        if not isinstance(secret, SecretBytes):
            raise IdentityTokenError("identity_token.invalid_secret_file")
        key = secret.reveal()
        if not _MINIMUM_KEY_BYTES <= len(key) <= _MAXIMUM_KEY_BYTES:
            raise IdentityTokenError("identity_token.invalid_secret_size")
        return cls(key)

    @classmethod
    def from_secret_provider(
        cls,
        provider: IdentitySecretProvider,
    ) -> HmacSha256IdentityTokenResolver:
        """Load the identity key from its closed deployment logical name."""

        try:
            secret = provider.read_bytes("identity_hmac_key")
        except SecretProviderError:
            raise IdentityTokenError("identity_token.secret_provider_failure") from None
        if not isinstance(secret, SecretBytes):
            raise IdentityTokenError("identity_token.secret_provider_failure")
        key = secret.reveal()
        if len(key) != 32:
            raise IdentityTokenError("identity_token.invalid_secret_size")
        return cls(key)

    def resolve(
        self,
        site_id: str,
        purpose: str,
        provider: str,
        subject: str,
    ) -> str:
        if not isinstance(site_id, str) or _SITE_ID.fullmatch(site_id) is None:
            raise IdentityTokenError("identity_token.invalid_site")
        if not isinstance(purpose, str) or _PURPOSE.fullmatch(purpose) is None:
            raise IdentityTokenError("identity_token.invalid_purpose")
        normalized = normalize_identity_subject(provider, subject)
        parts = ("v1", site_id, purpose, provider, normalized)
        material = b"".join(
            struct.pack(">I", len(encoded)) + encoded
            for encoded in (part.encode("utf-8") for part in parts)
        )
        digest = hmac.new(self._key, material, hashlib.sha256).digest()
        token = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        return f"extid:v1:{provider}:{token}"

    def __repr__(self) -> str:
        return f"{type(self).__name__}(key=<redacted bytes={len(self._key)}>)"


def _validated_key(key: bytes) -> bytes:
    if not isinstance(key, bytes):
        raise IdentityTokenError("identity_token.invalid_secret")
    if not _MINIMUM_KEY_BYTES <= len(key) <= _MAXIMUM_KEY_BYTES:
        raise IdentityTokenError("identity_token.invalid_secret_size")
    return bytes(key)


__all__ = [
    "HmacSha256IdentityTokenResolver",
    "IDENTITY_PROVIDERS",
    "IdentityTokenError",
    "IdentityTokenResolver",
    "IdentitySecretProvider",
    "TransientIdentitySubject",
    "normalize_identity_subject",
]
