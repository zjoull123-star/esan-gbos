from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from services.local_pilot_runtime.secret_provider import SecretBytes, SecretProviderError


def test_hmac_identity_refs_are_stable_scoped_and_provider_prefixed() -> None:
    from observer.identity_tokens import HmacSha256IdentityTokenResolver

    resolver = HmacSha256IdentityTokenResolver(b"k" * 32)

    first = resolver.resolve(
        "gbos.localhost",
        "observation_processing",
        "email",
        "alice@example.invalid",
    )
    replay = resolver.resolve(
        "gbos.localhost",
        "observation_processing",
        "email",
        "alice@example.invalid",
    )

    assert first == replay
    assert first.startswith("extid:v1:email:")
    assert "alice" not in first
    assert len(first.rsplit(":", 1)[1]) == 43
    assert re.fullmatch(
        r"extid:v1:email:[A-Za-z0-9_-]{43}",
        first,
    )
    assert (
        resolver.resolve(
            "other.localhost",
            "observation_processing",
            "email",
            "alice@example.invalid",
        )
        != first
    )
    assert (
        resolver.resolve(
            "gbos.localhost",
            "entity_resolution",
            "email",
            "alice@example.invalid",
        )
        != first
    )
    whatsapp = resolver.resolve(
        "gbos.localhost",
        "observation_processing",
        "whatsapp",
        "15550001111",
    )
    assert whatsapp.startswith("extid:v1:whatsapp:")
    assert whatsapp != first


def test_identity_subject_normalization_is_provider_specific_and_safe() -> None:
    from observer.identity_tokens import IdentityTokenError, normalize_identity_subject

    assert normalize_identity_subject("email", "  Alice@Example.INVALID  ") == (
        "alice@example.invalid"
    )
    assert normalize_identity_subject("wecom", "CaseSensitive_User") == "CaseSensitive_User"
    assert normalize_identity_subject("whatsapp", "15550001111") == "15550001111"
    assert normalize_identity_subject("whatsapp", "1" * 15) == "1" * 15
    assert normalize_identity_subject("phone", "+" + "1" * 15) == "+" + "1" * 15

    sentinel = "SUBJECT-SENTINEL@example.invalid"
    for provider, subject in (
        ("email", sentinel * 20),
        ("whatsapp", " 15550001111 "),
        ("whatsapp", "1" * 16),
        ("phone", "+" + "1" * 16),
        ("wecom", "bad user id"),
        ("carrier_pigeon", sentinel),
    ):
        with pytest.raises(IdentityTokenError) as captured:
            normalize_identity_subject(provider, subject)
        assert sentinel not in str(captured.value)
        assert sentinel not in repr(captured.value)


def test_secret_file_loading_handles_bounded_short_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import observer.identity_tokens as identity_tokens

    secret = tmp_path / "short-read.key"
    secret.write_bytes(b"r" * 32)
    secret.chmod(0o600)
    real_read = identity_tokens.os.read

    def short_read(descriptor: int, maximum: int) -> bytes:
        return real_read(descriptor, min(maximum, 5))

    monkeypatch.setattr(identity_tokens.os, "read", short_read)

    resolver = identity_tokens.HmacSha256IdentityTokenResolver.from_secret_file(secret)
    assert resolver.resolve(
        "gbos.localhost",
        "observation_processing",
        "email",
        "alice@example.invalid",
    ).startswith("extid:v1:email:")


def test_secret_file_loading_requires_regular_non_symlink_safe_bounded_key(
    tmp_path: Path,
) -> None:
    from observer.identity_tokens import HmacSha256IdentityTokenResolver, IdentityTokenError

    secret = tmp_path / "identity-token.key"
    secret.write_bytes(b"s" * 32)
    secret.chmod(0o600)

    resolver = HmacSha256IdentityTokenResolver.from_secret_file(secret)
    assert resolver.resolve(
        "gbos.localhost",
        "observation_processing",
        "email",
        "alice@example.invalid",
    ).startswith("extid:v1:email:")
    assert "ssss" not in repr(resolver)

    link = tmp_path / "identity-token-link.key"
    link.symlink_to(secret)
    with pytest.raises(IdentityTokenError, match="identity_token.invalid_secret_file"):
        HmacSha256IdentityTokenResolver.from_secret_file(link)

    secret.chmod(0o644)
    with pytest.raises(IdentityTokenError, match="identity_token.invalid_secret_mode"):
        HmacSha256IdentityTokenResolver.from_secret_file(secret)

    secret.chmod(0o600)
    secret.write_bytes(b"short")
    with pytest.raises(IdentityTokenError, match="identity_token.invalid_secret_size"):
        HmacSha256IdentityTokenResolver.from_secret_file(secret)

    secret.write_bytes(b"x" * 4097)
    os.chmod(secret, 0o400)
    with pytest.raises(IdentityTokenError, match="identity_token.invalid_secret_size"):
        HmacSha256IdentityTokenResolver.from_secret_file(secret)


def test_non_regular_secret_path_is_rejected_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import observer.identity_tokens as identity_tokens

    directory = tmp_path / "not-a-secret-file"
    directory.mkdir(mode=0o700)
    open_calls: list[object] = []
    real_open = identity_tokens.os.open

    def recording_open(path: object, flags: int) -> int:
        open_calls.append(path)
        return real_open(path, flags)

    monkeypatch.setattr(identity_tokens.os, "open", recording_open)

    with pytest.raises(
        identity_tokens.IdentityTokenError,
        match="identity_token.invalid_secret_file",
    ):
        identity_tokens.HmacSha256IdentityTokenResolver.from_secret_file(directory)
    assert open_calls == []


def test_path_compatibility_wrapper_keeps_file_error_code_on_provider_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from observer.identity_tokens import HmacSha256IdentityTokenResolver, IdentityTokenError

    from services.local_pilot_runtime import secret_provider

    secret = tmp_path / "identity-token.key"
    secret.write_bytes(b"s" * 32)
    secret.chmod(0o600)

    def disappearing_open(*args: object, **kwargs: object) -> int:
        del args, kwargs
        raise FileNotFoundError

    monkeypatch.setattr(secret_provider.os, "open", disappearing_open)

    with pytest.raises(
        IdentityTokenError,
        match="identity_token.invalid_secret_file",
    ):
        HmacSha256IdentityTokenResolver.from_secret_file(secret)


def test_identity_resolver_rejects_unsafe_inputs_without_rendering_them() -> None:
    from observer.identity_tokens import HmacSha256IdentityTokenResolver, IdentityTokenError

    secret = b"KEY-SENTINEL-" + b"x" * 32
    resolver = HmacSha256IdentityTokenResolver(secret)
    subject = "SUBJECT-SENTINEL@example.invalid"

    with pytest.raises(IdentityTokenError) as captured:
        resolver.resolve(
            "gbos.localhost",
            "observation_processing",
            "email",
            subject * 20,
        )

    rendered = repr((resolver, captured.value))
    assert subject not in rendered
    assert secret.decode() not in rendered


def test_identity_hmac_consumes_exact_32_secret_bytes_from_logical_name() -> None:
    from observer.identity_tokens import HmacSha256IdentityTokenResolver

    requested: list[str] = []

    class Provider:
        def read_bytes(self, logical_name: str) -> SecretBytes:
            requested.append(logical_name)
            return SecretBytes(b"P" * 32)

    resolver = HmacSha256IdentityTokenResolver.from_secret_provider(Provider())

    assert requested == ["identity_hmac_key"]
    assert "PPPP" not in repr(resolver)
    assert resolver.resolve(
        "gbos.localhost",
        "observation_processing",
        "email",
        "alice@example.invalid",
    ).startswith("extid:v1:email:")


@pytest.mark.parametrize("key", [b"x" * 31, b"x" * 33])
def test_identity_provider_rejects_non_exact_key_without_rendering_it(key: bytes) -> None:
    from observer.identity_tokens import HmacSha256IdentityTokenResolver, IdentityTokenError

    class Provider:
        def read_bytes(self, logical_name: str) -> SecretBytes:
            assert logical_name == "identity_hmac_key"
            return SecretBytes(key)

    with pytest.raises(
        IdentityTokenError,
        match="identity_token.invalid_secret_size",
    ) as captured:
        HmacSha256IdentityTokenResolver.from_secret_provider(Provider())
    assert key.decode() not in repr(captured.value)


def test_identity_provider_error_is_translated_without_detail() -> None:
    from observer.identity_tokens import HmacSha256IdentityTokenResolver, IdentityTokenError

    class Provider:
        def read_bytes(self, logical_name: str) -> SecretBytes:
            assert logical_name == "identity_hmac_key"
            raise SecretProviderError("SECRET-PROVIDER-DETAIL")

    with pytest.raises(
        IdentityTokenError,
        match="identity_token.secret_provider_failure",
    ) as captured:
        HmacSha256IdentityTokenResolver.from_secret_provider(Provider())
    assert "SECRET-PROVIDER-DETAIL" not in repr(captured.value)
