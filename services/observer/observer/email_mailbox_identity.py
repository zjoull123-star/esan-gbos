"""Stateless derivation of one site-isolated opaque mailbox address identity."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .identity_tokens import IdentityTokenError, IdentityTokenResolver, normalize_identity_subject
from .models import TenantScope

_OPAQUE_EMAIL_REF = re.compile(r"^extid:v1:email:[A-Za-z0-9_-]{43}$")


class EmailMailboxIdentityError(ValueError):
    """Safe mailbox identity failure which never renders protected inputs."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if not re.fullmatch(r"mailbox_identity\.[a-z_]{1,60}", code):
            raise ValueError("invalid mailbox identity error code")
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r})"


@dataclass(frozen=True, slots=True, repr=False)
class EmailMailboxIdentity:
    opaque_address_ref: str
    normalization_version: str = "email-v1"

    def __post_init__(self) -> None:
        if _OPAQUE_EMAIL_REF.fullmatch(self.opaque_address_ref) is None:
            raise EmailMailboxIdentityError("mailbox_identity.unavailable")
        if self.normalization_version != "email-v1":
            raise EmailMailboxIdentityError("mailbox_identity.unavailable")

    def to_wire(self) -> dict[str, str]:
        return {
            "opaque_address_ref": self.opaque_address_ref,
            "normalization_version": self.normalization_version,
        }

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(opaque_address_ref=<redacted>, "
            f"normalization_version={self.normalization_version!r})"
        )


class EmailMailboxIdentityService:
    """Normalize an address transiently and derive its opaque Observer reference."""

    __slots__ = ("_identity_resolver",)

    def __init__(self, *, identity_resolver: IdentityTokenResolver) -> None:
        if not callable(getattr(identity_resolver, "resolve", None)):
            raise EmailMailboxIdentityError("mailbox_identity.unavailable")
        self._identity_resolver = identity_resolver

    def derive(
        self,
        scope: TenantScope,
        *,
        canonical_mailbox_address: str,
    ) -> EmailMailboxIdentity:
        if not isinstance(scope, TenantScope):
            raise EmailMailboxIdentityError("mailbox_identity.unavailable")
        try:
            normalized = normalize_identity_subject("email", canonical_mailbox_address)
        except IdentityTokenError:
            raise EmailMailboxIdentityError("mailbox_identity.invalid_address") from None
        try:
            opaque_ref = self._identity_resolver.resolve(
                scope.site_id,
                "observation_processing",
                "email",
                normalized,
            )
            return EmailMailboxIdentity(opaque_address_ref=opaque_ref)
        except EmailMailboxIdentityError:
            raise
        except Exception:
            raise EmailMailboxIdentityError("mailbox_identity.unavailable") from None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(identity_resolver=<redacted>)"


__all__ = [
    "EmailMailboxIdentity",
    "EmailMailboxIdentityError",
    "EmailMailboxIdentityService",
]
