from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import Final, Literal, TypeGuard, final

SecretKind = Literal["text", "bytes", "closed_json"]
_SAFE_COMPONENT: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?")
_ERROR_MESSAGE: Final = "secret provider request rejected"
_READ_CHUNK_BYTES: Final = 64 * 1024


class SecretProviderError(ValueError):
    """A deliberately non-descriptive secret-provider failure."""


def _reject() -> SecretProviderError:
    return SecretProviderError(_ERROR_MESSAGE)


@final
class SecretBytes:
    """Opaque bytes that require an explicit boundary crossing to reveal."""

    __slots__ = ("__value",)

    def __init__(self, value: bytes) -> None:
        if type(value) is not bytes:
            raise _reject()
        self.__value = value

    def reveal(self) -> bytes:
        return self.__value

    def __repr__(self) -> str:
        return "SecretBytes(<redacted>)"

    __str__ = __repr__


@final
class SecretText:
    """Opaque text that requires an explicit boundary crossing to reveal."""

    __slots__ = ("__value",)

    def __init__(self, value: str) -> None:
        if type(value) is not str:
            raise _reject()
        self.__value = value

    def reveal(self) -> str:
        return self.__value

    def __repr__(self) -> str:
        return "SecretText(<redacted>)"

    __str__ = __repr__


def _is_safe_component(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and _SAFE_COMPONENT.fullmatch(value) is not None
        and value not in {".", ".."}
        and ".." not in value
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


@dataclass(frozen=True, slots=True)
class SecretSpec:
    name: str
    filename: str
    kind: SecretKind
    minimum_bytes: int
    maximum_bytes: int
    exact_bytes: int | None = None
    required: bool = True

    def __post_init__(self) -> None:
        integers_are_valid = (
            type(self.minimum_bytes) is int
            and type(self.maximum_bytes) is int
            and self.minimum_bytes >= 0
            and self.maximum_bytes > 0
            and self.minimum_bytes <= self.maximum_bytes
        )
        exact_is_valid = self.exact_bytes is None or (
            type(self.exact_bytes) is int
            and self.minimum_bytes <= self.exact_bytes <= self.maximum_bytes
        )
        if (
            not _is_safe_component(self.name)
            or not _is_safe_component(self.filename)
            or type(self.kind) is not str
            or self.kind not in ("text", "bytes", "closed_json")
            or not integers_are_valid
            or not exact_is_valid
            or type(self.required) is not bool
        ):
            raise _reject()

    def __repr__(self) -> str:
        return (
            "SecretSpec(name=<redacted>, filename=<redacted>, "
            f"kind={self.kind!r}, minimum_bytes={self.minimum_bytes}, "
            f"maximum_bytes={self.maximum_bytes}, exact_bytes={self.exact_bytes!r}, "
            f"required={self.required!r})"
        )

    __str__ = __repr__


class MountedFileSecretProvider:
    __slots__ = ("_root", "_specs")

    def __init__(
        self,
        root: str | PathLike[str],
        specs: Iterable[SecretSpec],
    ) -> None:
        try:
            root_path = Path(root)
            configured = tuple(specs)
        except TypeError, ValueError:
            raise _reject() from None
        if not root_path.is_absolute() or not configured:
            raise _reject()
        if any(not isinstance(spec, SecretSpec) for spec in configured):
            raise _reject()
        by_name = {spec.name: spec for spec in configured}
        filenames = {spec.filename for spec in configured}
        if len(by_name) != len(configured) or len(filenames) != len(configured):
            raise _reject()
        self._root = root_path
        self._specs = by_name

    def __repr__(self) -> str:
        return "MountedFileSecretProvider(root=<redacted>, specs=<redacted>)"

    __str__ = __repr__

    def _spec(self, name: object) -> SecretSpec:
        if not _is_safe_component(name):
            raise _reject()
        try:
            return self._specs[name]
        except KeyError:
            raise _reject() from None

    def read_bytes(self, name: str) -> SecretBytes | None:
        spec = self._spec_for_kind(name, "bytes")
        payload = self._read_file(spec)
        return None if payload is None else SecretBytes(payload)

    def read_text(self, name: str) -> SecretText | None:
        spec = self._spec_for_kind(name, "text")
        payload = self._read_file(spec)
        if payload is None:
            return None
        if payload.endswith(b"\n"):
            payload = payload[:-1]
        if not payload or b"\x00" in payload or b"\r" in payload or b"\n" in payload:
            raise _reject()
        try:
            return SecretText(payload.decode("utf-8"))
        except UnicodeDecodeError:
            raise _reject() from None

    def read_json_bytes(self, name: str) -> SecretBytes | None:
        spec = self._spec_for_kind(name, "closed_json")
        payload = self._read_file(spec)
        return None if payload is None else SecretBytes(payload)

    def _spec_for_kind(self, name: object, kind: SecretKind) -> SecretSpec:
        spec = self._spec(name)
        if spec.kind != kind:
            raise _reject()
        return spec

    def _read_file(self, spec: SecretSpec) -> bytes | None:
        path = self._root / spec.filename
        descriptor = -1
        try:
            try:
                before = os.lstat(path)
            except FileNotFoundError:
                if not spec.required:
                    return None
                raise _reject() from None
            if not _is_private_regular(before):
                raise _reject()
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            )
            opened = os.fstat(descriptor)
            if (
                not _same_file_and_size(before, opened)
                or not _is_private_regular(opened)
                or not _size_is_allowed(opened.st_size, spec)
            ):
                raise _reject()

            chunks: list[bytes] = []
            remaining = opened.st_size
            while remaining:
                chunk = os.read(descriptor, min(remaining, _READ_CHUNK_BYTES))
                if not chunk or len(chunk) > remaining:
                    raise _reject()
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1):
                raise _reject()

            after = os.fstat(descriptor)
            current = os.lstat(path)
            payload = b"".join(chunks)
            if (
                len(payload) != opened.st_size
                or not _same_file_and_size(opened, after)
                or not _same_file_and_size(opened, current)
                or not _is_private_regular(after)
                or not _is_private_regular(current)
            ):
                raise _reject()
            return payload
        except SecretProviderError:
            raise
        except OSError, TypeError, ValueError:
            raise _reject() from None
        finally:
            if descriptor >= 0:
                with suppress(OSError):
                    os.close(descriptor)


def _same_file_and_size(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_size == right.st_size
    )


def _is_private_regular(details: os.stat_result) -> bool:
    return stat.S_ISREG(details.st_mode) and stat.S_IMODE(details.st_mode) in {0o400, 0o600}


def _size_is_allowed(size: int, spec: SecretSpec) -> bool:
    return spec.minimum_bytes <= size <= spec.maximum_bytes and (
        spec.exact_bytes is None or size == spec.exact_bytes
    )
