from __future__ import annotations

import os
from pathlib import Path

import pytest

from services.local_pilot_runtime.secret_provider import (
    MountedFileSecretProvider,
    SecretBytes,
    SecretProviderError,
    SecretSpec,
    SecretText,
)


def test_public_surface_is_importable() -> None:
    assert SecretSpec is not None
    assert MountedFileSecretProvider is not None


def test_secret_wrappers_require_explicit_reveal_and_never_render_payload() -> None:
    bytes_payload = b"distinctive-private-bytes"
    text_payload = "distinctive-private-text"
    first_bytes = SecretBytes(bytes_payload)
    second_bytes = SecretBytes(bytes_payload)
    first_text = SecretText(text_payload)
    second_text = SecretText(text_payload)

    assert first_bytes.reveal() == bytes_payload
    assert first_text.reveal() == text_payload
    assert first_bytes != second_bytes
    assert first_text != second_text
    for rendered in (repr(first_bytes), str(first_bytes), repr(first_text), str(first_text)):
        assert bytes_payload.decode() not in rendered
        assert text_payload not in rendered
    for error in (RuntimeError(first_bytes), RuntimeError(first_text)):
        assert bytes_payload.decode() not in f"{error!s} {error!r}"
        assert text_payload not in f"{error!s} {error!r}"

    with pytest.raises(AssertionError) as caught:
        assert first_bytes == second_bytes
    assert bytes_payload.decode() not in str(caught.value)
    assert bytes_payload.decode() not in repr(caught.value)


@pytest.mark.parametrize(
    ("wrapper", "payload"),
    [(SecretBytes, "not-bytes"), (SecretText, b"not-text")],
)
def test_secret_wrapper_validation_error_is_redacted(wrapper: object, payload: object) -> None:
    with pytest.raises(SecretProviderError) as caught:
        wrapper(payload)  # type: ignore[operator]

    _assert_redacted_error(caught.value, payload)


def _spec(
    *,
    name: str = "api-token",
    filename: str = "api-token.secret",
    kind: str = "bytes",
    minimum_bytes: int = 1,
    maximum_bytes: int = 64,
    exact_bytes: int | None = None,
    required: bool = True,
) -> SecretSpec:
    return SecretSpec(
        name=name,
        filename=filename,
        kind=kind,  # type: ignore[arg-type]
        minimum_bytes=minimum_bytes,
        maximum_bytes=maximum_bytes,
        exact_bytes=exact_bytes,
        required=required,
    )


def test_spec_and_provider_rendering_redacts_names_and_root(tmp_path: Path) -> None:
    sensitive_name = "customer-private-token"
    sensitive_filename = "customer-private-token.secret"
    spec = _spec(name=sensitive_name, filename=sensitive_filename, exact_bytes=32)
    provider = MountedFileSecretProvider(tmp_path, [spec])

    for rendered in (repr(spec), str(spec), repr(provider), str(provider)):
        assert sensitive_name not in rendered
        assert sensitive_filename not in rendered
        assert str(tmp_path) not in rendered
    assert "exact_bytes=32" in repr(spec)


@pytest.mark.parametrize(
    "mutation",
    [
        {"name": ""},
        {"name": "."},
        {"name": ".."},
        {"name": "../token"},
        {"name": "/token"},
        {"name": "token/child"},
        {"name": "token\\child"},
        {"name": "token\x00child"},
        {"name": "token\nchild"},
        {"filename": ""},
        {"filename": "."},
        {"filename": ".."},
        {"filename": "../token"},
        {"filename": "/token"},
        {"filename": "token/child"},
        {"filename": "token\\child"},
        {"filename": "token\x00child"},
        {"filename": "token\tchild"},
        {"kind": "json"},
        {"kind": ["bytes"]},
        {"minimum_bytes": -1},
        {"maximum_bytes": 0},
        {"minimum_bytes": 2, "maximum_bytes": 1},
        {"minimum_bytes": True},
        {"maximum_bytes": True},
        {"exact_bytes": 0},
        {"exact_bytes": 65},
        {"exact_bytes": True},
        {"required": 1},
    ],
)
def test_spec_rejects_open_or_invalid_policy(mutation: dict[str, object]) -> None:
    values: dict[str, object] = {
        "name": "api-token",
        "filename": "api-token.secret",
        "kind": "bytes",
        "minimum_bytes": 1,
        "maximum_bytes": 64,
        "exact_bytes": None,
        "required": True,
    }
    values.update(mutation)

    with pytest.raises(SecretProviderError) as caught:
        SecretSpec(**values)  # type: ignore[arg-type]

    assert "token" not in str(caught.value)
    assert "token" not in repr(caught.value)


def test_provider_requires_absolute_root_and_closed_unique_specs(tmp_path: Path) -> None:
    cases = [
        (Path("relative"), [_spec()]),
        (tmp_path, []),
        (tmp_path, [_spec(), _spec(filename="other.secret")]),
        (tmp_path, [_spec(), _spec(name="other-token")]),
    ]

    for root, specs in cases:
        with pytest.raises(SecretProviderError) as caught:
            MountedFileSecretProvider(root, specs)
        assert "token" not in str(caught.value)
        assert str(tmp_path) not in str(caught.value)


@pytest.mark.parametrize(
    "name",
    [
        "unknown",
        "",
        ".",
        "..",
        "../api-token",
        "/api-token",
        "api/token",
        "api\\token",
        "x\x00y",
        "x\ny",
    ],
)
def test_provider_rejects_unknown_or_unsafe_logical_names(tmp_path: Path, name: str) -> None:
    provider = MountedFileSecretProvider(tmp_path, [_spec()])

    with pytest.raises(SecretProviderError) as caught:
        provider.read_bytes(name)

    rendered = repr(caught.value)
    assert name not in rendered or name == ""
    assert "api-token" not in rendered
    assert str(tmp_path) not in rendered


def test_provider_has_no_enumeration_or_reveal_all_surface(tmp_path: Path) -> None:
    provider = MountedFileSecretProvider(tmp_path, [_spec()])

    for name in ("list", "list_names", "enumerate", "read_all", "reveal_all", "items"):
        assert not hasattr(provider, name)


def _private_file(path: Path, payload: bytes, *, mode: int = 0o600) -> Path:
    path.write_bytes(payload)
    path.chmod(mode)
    return path


def _assert_redacted_error(error: BaseException, *secrets: object) -> None:
    rendered = f"{error!s} {error!r}"
    for secret in secrets:
        assert str(secret) not in rendered


@pytest.mark.parametrize("mode", [0o400, 0o600])
def test_read_bytes_preserves_exact_payload_and_accepts_private_modes(
    tmp_path: Path,
    mode: int,
) -> None:
    payload = (b"\x00\r\nsecret-binary\xff" + bytes(range(15)))[:32]
    assert len(payload) == 32
    _private_file(tmp_path / "binary.key", payload, mode=mode)
    provider = MountedFileSecretProvider(
        tmp_path,
        [_spec(filename="binary.key", exact_bytes=32)],
    )

    result = provider.read_bytes("api-token")

    assert type(result) is SecretBytes
    assert result.reveal() == payload


def test_read_json_bytes_returns_unparsed_exact_bytes(tmp_path: Path) -> None:
    payload = b'{"duplicate":1,"duplicate":2}\x00not-parsed\n'
    _private_file(tmp_path / "credential.json", payload)
    provider = MountedFileSecretProvider(
        tmp_path,
        [_spec(filename="credential.json", kind="closed_json", maximum_bytes=256)],
    )

    result = provider.read_json_bytes("api-token")

    assert type(result) is SecretBytes
    assert result.reveal() == payload


@pytest.mark.parametrize("payload", [b"private-value", b"private-value\n"])
def test_read_text_accepts_at_most_one_trailing_lf(tmp_path: Path, payload: bytes) -> None:
    _private_file(tmp_path / "text.secret", payload)
    provider = MountedFileSecretProvider(
        tmp_path,
        [_spec(filename="text.secret", kind="text")],
    )

    result = provider.read_text("api-token")

    assert type(result) is SecretText
    assert result.reveal() == "private-value"


@pytest.mark.parametrize(
    "payload",
    [b"", b"\n", b"value\n\n", b"left\nright", b"value\r", b"left\rright", b"nul\x00byte", b"\xff"],
)
def test_read_text_rejects_empty_or_noncanonical_content(tmp_path: Path, payload: bytes) -> None:
    path = _private_file(tmp_path / "text.secret", payload)
    provider = MountedFileSecretProvider(
        tmp_path,
        [_spec(filename="text.secret", kind="text", minimum_bytes=0)],
    )

    with pytest.raises(SecretProviderError) as caught:
        provider.read_text("api-token")

    _assert_redacted_error(caught.value, tmp_path, path, "api-token", payload)


@pytest.mark.parametrize(
    ("kind", "reader"),
    [
        ("text", "read_bytes"),
        ("text", "read_json_bytes"),
        ("bytes", "read_text"),
        ("bytes", "read_json_bytes"),
        ("closed_json", "read_text"),
        ("closed_json", "read_bytes"),
    ],
)
def test_readers_enforce_declared_secret_kind(tmp_path: Path, kind: str, reader: str) -> None:
    payload = b"private-value"
    path = _private_file(tmp_path / "value.secret", payload)
    provider = MountedFileSecretProvider(
        tmp_path,
        [_spec(filename="value.secret", kind=kind)],
    )

    with pytest.raises(SecretProviderError) as caught:
        getattr(provider, reader)("api-token")

    _assert_redacted_error(caught.value, tmp_path, path, "api-token", payload)


def test_missing_required_rejects_while_missing_optional_returns_none(tmp_path: Path) -> None:
    required = MountedFileSecretProvider(tmp_path, [_spec(required=True)])
    optional = MountedFileSecretProvider(tmp_path, [_spec(required=False)])

    with pytest.raises(SecretProviderError) as caught:
        required.read_bytes("api-token")

    _assert_redacted_error(caught.value, tmp_path, "api-token")
    assert optional.read_bytes("api-token") is None


@pytest.mark.parametrize("case", ["symlink", "directory", "mode", "undersize", "oversize"])
def test_read_rejects_unsafe_file_type_mode_and_size(tmp_path: Path, case: str) -> None:
    path = tmp_path / "value.secret"
    payload = b"private-value"
    if case == "symlink":
        target = _private_file(tmp_path / "private-target", payload)
        path.symlink_to(target)
    elif case == "directory":
        path.mkdir()
        path.chmod(0o600)
    elif case == "mode":
        _private_file(path, payload, mode=0o640)
    elif case == "undersize":
        _private_file(path, b"123")
    else:
        _private_file(path, b"x" * 9)
    provider = MountedFileSecretProvider(
        tmp_path,
        [_spec(filename="value.secret", minimum_bytes=4, maximum_bytes=8)],
    )

    with pytest.raises(SecretProviderError) as caught:
        provider.read_bytes("api-token")

    _assert_redacted_error(caught.value, tmp_path, path, "api-token", payload)


def test_exact_size_is_enforced(tmp_path: Path) -> None:
    path = _private_file(tmp_path / "value.secret", b"x" * 31)
    provider = MountedFileSecretProvider(
        tmp_path,
        [_spec(filename="value.secret", maximum_bytes=64, exact_bytes=32)],
    )

    with pytest.raises(SecretProviderError) as caught:
        provider.read_bytes("api-token")

    _assert_redacted_error(caught.value, tmp_path, path, "api-token")


def test_read_uses_no_follow_close_on_exec_lstat_and_fstat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"private-value"
    _private_file(tmp_path / "value.secret", payload)
    provider = MountedFileSecretProvider(tmp_path, [_spec(filename="value.secret")])
    original_open = os.open
    original_lstat = os.lstat
    original_fstat = os.fstat
    observed_flags: list[int] = []
    observed = {"lstat": 0, "fstat": 0}

    def recording_open(path: Path, flags: int) -> int:
        observed_flags.append(flags)
        return original_open(path, flags)

    def recording_lstat(path: Path) -> os.stat_result:
        observed["lstat"] += 1
        return original_lstat(path)

    def recording_fstat(descriptor: int) -> os.stat_result:
        observed["fstat"] += 1
        return original_fstat(descriptor)

    monkeypatch.setattr("services.local_pilot_runtime.secret_provider.os.open", recording_open)
    monkeypatch.setattr("services.local_pilot_runtime.secret_provider.os.lstat", recording_lstat)
    monkeypatch.setattr("services.local_pilot_runtime.secret_provider.os.fstat", recording_fstat)

    result = provider.read_bytes("api-token")
    assert result is not None
    assert result.reveal() == payload
    assert observed_flags[0] & os.O_NOFOLLOW
    assert observed_flags[0] & os.O_CLOEXEC
    assert observed["lstat"] >= 2
    assert observed["fstat"] >= 2


def test_short_reads_are_accumulated_with_bounded_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"private-value"
    _private_file(tmp_path / "value.secret", payload)
    provider = MountedFileSecretProvider(tmp_path, [_spec(filename="value.secret")])
    original_read = os.read
    requested: list[int] = []

    def short_read(descriptor: int, amount: int) -> bytes:
        requested.append(amount)
        return original_read(descriptor, min(amount, 2))

    monkeypatch.setattr("services.local_pilot_runtime.secret_provider.os.read", short_read)

    result = provider.read_bytes("api-token")
    assert result is not None
    assert result.reveal() == payload
    assert requested[-1] == 1
    assert all(0 < amount <= 64 for amount in requested)


def test_early_eof_is_rejected_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"private-value"
    path = _private_file(tmp_path / "value.secret", payload)
    provider = MountedFileSecretProvider(tmp_path, [_spec(filename="value.secret")])
    original_read = os.read
    calls = 0

    def early_eof(descriptor: int, amount: int) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_read(descriptor, min(amount, 2))
        return b""

    monkeypatch.setattr("services.local_pilot_runtime.secret_provider.os.read", early_eof)

    with pytest.raises(SecretProviderError) as caught:
        provider.read_bytes("api-token")

    _assert_redacted_error(caught.value, tmp_path, path, "api-token", payload)


def test_growth_during_read_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"private-value"
    path = _private_file(tmp_path / "value.secret", payload)
    provider = MountedFileSecretProvider(tmp_path, [_spec(filename="value.secret")])
    original_read = os.read
    changed = False

    def growing_read(descriptor: int, amount: int) -> bytes:
        nonlocal changed
        chunk = original_read(descriptor, amount)
        if not changed:
            changed = True
            with path.open("ab") as handle:
                handle.write(b"growth")
        return chunk

    monkeypatch.setattr("services.local_pilot_runtime.secret_provider.os.read", growing_read)

    with pytest.raises(SecretProviderError) as caught:
        provider.read_bytes("api-token")

    _assert_redacted_error(caught.value, tmp_path, path, "api-token", payload)


def test_replacement_between_lstat_and_open_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"private-value"
    path = _private_file(tmp_path / "value.secret", payload)
    moved = tmp_path / "moved.secret"
    provider = MountedFileSecretProvider(tmp_path, [_spec(filename="value.secret")])
    original_open = os.open
    replaced = False

    def replacing_open(open_path: Path, flags: int) -> int:
        nonlocal replaced
        if not replaced:
            replaced = True
            path.rename(moved)
            _private_file(path, payload)
        return original_open(open_path, flags)

    monkeypatch.setattr("services.local_pilot_runtime.secret_provider.os.open", replacing_open)

    with pytest.raises(SecretProviderError) as caught:
        provider.read_bytes("api-token")

    _assert_redacted_error(caught.value, tmp_path, path, moved, "api-token", payload)


def test_replacement_during_read_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"private-value"
    path = _private_file(tmp_path / "value.secret", payload)
    moved = tmp_path / "moved.secret"
    provider = MountedFileSecretProvider(tmp_path, [_spec(filename="value.secret")])
    original_read = os.read
    replaced = False

    def replacing_read(descriptor: int, amount: int) -> bytes:
        nonlocal replaced
        chunk = original_read(descriptor, amount)
        if not replaced:
            replaced = True
            path.rename(moved)
            _private_file(path, payload)
        return chunk

    monkeypatch.setattr("services.local_pilot_runtime.secret_provider.os.read", replacing_read)

    with pytest.raises(SecretProviderError) as caught:
        provider.read_bytes("api-token")

    _assert_redacted_error(caught.value, tmp_path, path, moved, "api-token", payload)
