from __future__ import annotations

import pytest
from observer.email_mailbox_identity import (
    EmailMailboxIdentityError,
    EmailMailboxIdentityService,
)
from observer.identity_tokens import HmacSha256IdentityTokenResolver
from observer.models import TenantScope


def test_mailbox_identity_normalizes_casefolds_and_is_deterministic() -> None:
    service = EmailMailboxIdentityService(
        identity_resolver=HmacSha256IdentityTokenResolver(b"m" * 32)
    )
    scope = TenantScope("alpha.example", "observation_processing")

    first = service.derive(
        scope,
        canonical_mailbox_address="  Sales.Primary@Example.COM  ",
    )
    replay = service.derive(
        scope,
        canonical_mailbox_address="sales.primary@example.com",
    )

    assert first == replay
    assert first.to_wire() == {
        "opaque_address_ref": first.opaque_address_ref,
        "normalization_version": "email-v1",
    }
    assert first.opaque_address_ref.startswith("extid:v1:email:")


def test_mailbox_identity_is_site_isolated_and_uses_observation_processing() -> None:
    calls: list[tuple[str, str, str, str]] = []

    class Resolver:
        def resolve(self, site_id: str, purpose: str, provider: str, subject: str) -> str:
            calls.append((site_id, purpose, provider, subject))
            return "extid:v1:email:" + "A" * 43

    service = EmailMailboxIdentityService(identity_resolver=Resolver())
    service.derive(
        TenantScope("alpha.example", "sales_follow_up"),
        canonical_mailbox_address="OWNER@EXAMPLE.COM",
    )

    assert calls == [("alpha.example", "observation_processing", "email", "owner@example.com")]

    hmac_service = EmailMailboxIdentityService(
        identity_resolver=HmacSha256IdentityTokenResolver(b"m" * 32)
    )
    alpha = hmac_service.derive(
        TenantScope("alpha.example", "observation_processing"),
        canonical_mailbox_address="owner@example.com",
    )
    beta = hmac_service.derive(
        TenantScope("beta.example", "observation_processing"),
        canonical_mailbox_address="owner@example.com",
    )
    assert alpha.opaque_address_ref != beta.opaque_address_ref


@pytest.mark.parametrize(
    "value",
    ["not-an-email", "two@@example.com", "bad address@example.com", ""],
)
def test_mailbox_identity_rejects_invalid_address_with_safe_code(value: str) -> None:
    service = EmailMailboxIdentityService(
        identity_resolver=HmacSha256IdentityTokenResolver(b"m" * 32)
    )

    with pytest.raises(EmailMailboxIdentityError) as caught:
        service.derive(
            TenantScope("alpha.example", "observation_processing"),
            canonical_mailbox_address=value,
        )

    assert caught.value.code == "mailbox_identity.invalid_address"
    if value:
        assert value not in repr(caught.value)
        assert value not in str(caught.value)


def test_mailbox_identity_repr_redacts_raw_address_and_opaque_ref() -> None:
    raw = "secret-mailbox@example.com"
    identity = EmailMailboxIdentityService(
        identity_resolver=HmacSha256IdentityTokenResolver(b"m" * 32)
    ).derive(
        TenantScope("alpha.example", "observation_processing"),
        canonical_mailbox_address=raw,
    )

    rendered = repr(identity)
    assert raw not in rendered
    assert identity.opaque_address_ref not in rendered
    assert "<redacted>" in rendered
