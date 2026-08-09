"""Private, attested phrase lexicon for local model projection."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from services.observer.observer.models import TenantScope

    from .model_projection_worker import TrustedPhraseResolution

_LEXICON_FIELDS = frozenset(
    {
        "schema_version",
        "site_id",
        "resolver_version",
        "approved_by",
        "approved_at",
        "expires_at",
        "names_complete",
        "organizations_complete",
        "names",
        "organizations",
    }
)
_MAX_FILE_BYTES = 65_536
_MAX_PHRASES_PER_KIND = 1_000
_MAX_PHRASE_LENGTH = 512
_MAX_ATTESTATION_AGE = timedelta(days=30)
_TOKEN_PATTERN = re.compile(r"<(?:EMAIL|PHONE|ENTITY)_[0-9a-f]{24}>")


class TrustedPhraseLexiconError(RuntimeError):
    """The materialized trusted phrase lexicon failed closed."""


@dataclass(frozen=True, slots=True, repr=False)
class _TrustedPhraseLexicon:
    site_id: str
    resolver_version: str
    approved_by: str
    approved_at: datetime
    expires_at: datetime
    names: tuple[str, ...]
    organizations: tuple[str, ...]
    names_complete: Literal[True] = True
    organizations_complete: Literal[True] = True

    def __repr__(self) -> str:
        return "_TrustedPhraseLexicon(attestation=<redacted>, phrases=<redacted>)"


class TrustedPhraseLexiconResolver:
    """Resolve only phrases from one locally attested, site-bound lexicon."""

    __slots__ = ("_clock", "_lexicon")

    def __init__(
        self,
        *,
        lexicon: _TrustedPhraseLexicon,
        clock: Callable[[], datetime],
    ) -> None:
        self._lexicon = lexicon
        self._clock = clock

    def __repr__(self) -> str:
        return "TrustedPhraseLexiconResolver(attestation=<redacted>, phrases=<redacted>)"

    def __call__(
        self,
        scope: TenantScope,
        observation_id: str,
        raw_text: str,
    ) -> TrustedPhraseResolution:
        del observation_id, raw_text
        now = _clock_value(self._clock)
        if scope.site_id != self._lexicon.site_id:
            raise TrustedPhraseLexiconError("trusted phrase lexicon site binding is invalid")
        if not self._lexicon.approved_at <= now < self._lexicon.expires_at:
            raise TrustedPhraseLexiconError(
                "trusted phrase lexicon has expired or is not yet valid"
            )
        return _resolution(self._lexicon)


def load_trusted_phrase_resolver(
    path: Path,
    *,
    expected_site_id: str,
    clock: Callable[[], datetime],
) -> TrustedPhraseLexiconResolver:
    """Load a closed mode-0600 JSON lexicon and bind it to one local site."""

    if not callable(clock):
        raise TrustedPhraseLexiconError("trusted phrase lexicon clock is invalid")
    expected = _text(expected_site_id, "site binding", maximum=140)
    value = _read_private_json(Path(path))
    if set(value) != _LEXICON_FIELDS or value.get("schema_version") != "1.0":
        raise TrustedPhraseLexiconError("trusted phrase lexicon must use the closed v1 schema")
    site_id = _text(value.get("site_id"), "site binding", maximum=140)
    if site_id != expected:
        raise TrustedPhraseLexiconError("trusted phrase lexicon site binding is invalid")
    resolver_version = _text(value.get("resolver_version"), "resolver version", maximum=80)
    approved_by = _text(value.get("approved_by"), "approver", maximum=256)
    if value.get("names_complete") is not True or value.get("organizations_complete") is not True:
        raise TrustedPhraseLexiconError("trusted phrase lexicon coverage is not complete")
    approved_at = _timestamp(value.get("approved_at"), "approval timestamp")
    expires_at = _timestamp(value.get("expires_at"), "expiry timestamp")
    now = _clock_value(clock)
    if not approved_at <= now < expires_at <= approved_at + _MAX_ATTESTATION_AGE:
        raise TrustedPhraseLexiconError("trusted phrase lexicon attestation is not currently valid")
    names = _phrases(value.get("names"), "names")
    organizations = _phrases(value.get("organizations"), "organizations")
    if not names and not organizations:
        raise TrustedPhraseLexiconError("trusted phrase lexicon must contain approved phrases")
    lexicon = _TrustedPhraseLexicon(
        site_id=site_id,
        resolver_version=resolver_version,
        approved_by=approved_by,
        approved_at=approved_at,
        expires_at=expires_at,
        names=names,
        organizations=organizations,
    )
    _resolution(lexicon)
    return TrustedPhraseLexiconResolver(lexicon=lexicon, clock=clock)


def _read_private_json(path: Path) -> dict[str, object]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        path_details = path.lstat()
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise TrustedPhraseLexiconError("trusted phrase lexicon is absent or unsafe") from exc
    try:
        details = os.fstat(descriptor)
        if (
            stat.S_ISLNK(path_details.st_mode)
            or path_details.st_dev != details.st_dev
            or path_details.st_ino != details.st_ino
            or not stat.S_ISREG(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o600
            or not 0 < details.st_size <= _MAX_FILE_BYTES
        ):
            raise TrustedPhraseLexiconError(
                "trusted phrase lexicon must be a bounded mode-0600 regular non-symlink file"
            )
        payload = os.read(descriptor, _MAX_FILE_BYTES + 1)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        len(payload) != details.st_size
        or len(payload) > _MAX_FILE_BYTES
        or after.st_size != details.st_size
        or after.st_mtime_ns != details.st_mtime_ns
    ):
        raise TrustedPhraseLexiconError("trusted phrase lexicon changed while being read")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrustedPhraseLexiconError("trusted phrase lexicon is invalid JSON") from exc
    if not isinstance(value, dict):
        raise TrustedPhraseLexiconError("trusted phrase lexicon must be a JSON object")
    return value


def _clock_value(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TrustedPhraseLexiconError("trusted phrase lexicon clock must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: object, name: str) -> datetime:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 64
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        raise TrustedPhraseLexiconError(f"trusted phrase lexicon {name} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TrustedPhraseLexiconError(f"trusted phrase lexicon {name} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TrustedPhraseLexiconError(f"trusted phrase lexicon {name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _phrases(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > _MAX_PHRASES_PER_KIND:
        raise TrustedPhraseLexiconError(f"trusted phrase lexicon {name} are invalid")
    phrases: list[str] = []
    for phrase in value:
        if (
            not isinstance(phrase, str)
            or not phrase
            or phrase != phrase.strip()
            or len(phrase) > _MAX_PHRASE_LENGTH
            or any(character in phrase for character in ("\x00", "\r", "\n"))
            or _TOKEN_PATTERN.fullmatch(phrase) is not None
        ):
            raise TrustedPhraseLexiconError(f"trusted phrase lexicon {name} are invalid")
        phrases.append(phrase)
    if len(phrases) != len(set(phrases)):
        raise TrustedPhraseLexiconError(f"trusted phrase lexicon {name} are invalid")
    return tuple(phrases)


def _text(value: object, name: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        raise TrustedPhraseLexiconError(f"trusted phrase lexicon {name} is invalid")
    return value


def _resolution(lexicon: _TrustedPhraseLexicon) -> TrustedPhraseResolution:
    from .model_projection_worker import TrustedPhraseResolution

    return TrustedPhraseResolution(
        names=lexicon.names,
        organizations=lexicon.organizations,
        names_complete=lexicon.names_complete,
        organizations_complete=lexicon.organizations_complete,
        resolver_version=lexicon.resolver_version,
    )


__all__ = [
    "TrustedPhraseLexiconError",
    "TrustedPhraseLexiconResolver",
    "load_trusted_phrase_resolver",
]
