from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from services.local_pilot_runtime import model_projection_worker
from services.local_pilot_runtime.trusted_phrase_lexicon import (
    TrustedPhraseLexiconError,
    load_trusted_phrase_resolver,
)
from services.observer.observer.models import TenantScope

NOW = datetime(2026, 8, 8, 10, tzinfo=UTC)
SCOPE = TenantScope("gbos.localhost", "observation_processing")


def _value() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "site_id": SCOPE.site_id,
        "resolver_version": "manual-attestation-2026-08-08",
        "approved_by": "local-data-steward",
        "approved_at": (NOW - timedelta(minutes=5)).isoformat(),
        "expires_at": (NOW + timedelta(days=7)).isoformat(),
        "names_complete": True,
        "organizations_complete": True,
        "names": ["Alice Zhang"],
        "organizations": ["Example Trading LLC"],
    }


def _private_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def test_private_attested_lexicon_returns_existing_resolution_and_ignores_inputs(
    tmp_path: Path,
) -> None:
    path = _private_json(tmp_path / "trusted-phrases.json", _value())
    resolver = load_trusted_phrase_resolver(
        path,
        expected_site_id=SCOPE.site_id,
        clock=lambda: NOW,
    )

    result = resolver(
        SCOPE,
        "untrusted-observation-id\r\n",
        "raw text must not influence the approved phrases <ENTITY_0123456789abcdef01234567>",
    )

    assert isinstance(result, model_projection_worker.TrustedPhraseResolution)
    assert result.names == ("Alice Zhang",)
    assert result.organizations == ("Example Trading LLC",)
    assert result.names_complete is True
    assert result.organizations_complete is True
    assert result.resolver_version == "manual-attestation-2026-08-08"
    rendered = repr(resolver)
    assert "Alice Zhang" not in rendered
    assert "Example Trading LLC" not in rendered
    assert "local-data-steward" not in rendered


def test_resolver_rechecks_expiry_on_every_call(tmp_path: Path) -> None:
    value = _value()
    expiry = NOW + timedelta(minutes=1)
    value["expires_at"] = expiry.isoformat()
    path = _private_json(tmp_path / "trusted-phrases.json", value)
    moments = [NOW]
    resolver = load_trusted_phrase_resolver(
        path,
        expected_site_id=SCOPE.site_id,
        clock=lambda: moments[0],
    )

    assert resolver(SCOPE, "first", "first raw").names == ("Alice Zhang",)
    moments[0] = expiry

    with pytest.raises(TrustedPhraseLexiconError, match="expired") as captured:
        resolver(SCOPE, "second", "Alice Zhang at Example Trading LLC")
    assert "Alice Zhang" not in str(captured.value)
    assert "Example Trading LLC" not in repr(captured.value)


def test_resolver_rejects_scope_site_drift_without_exposing_phrases(tmp_path: Path) -> None:
    path = _private_json(tmp_path / "trusted-phrases.json", _value())
    resolver = load_trusted_phrase_resolver(
        path,
        expected_site_id=SCOPE.site_id,
        clock=lambda: NOW,
    )

    with pytest.raises(TrustedPhraseLexiconError, match="site") as captured:
        resolver(TenantScope("other.localhost", SCOPE.processing_purpose), "ignored", "ignored")

    assert "Alice Zhang" not in str(captured.value)
    assert "Example Trading LLC" not in repr(captured.value)


@pytest.mark.parametrize(
    "mutation",
    [
        "extra_field",
        "wrong_schema",
        "wrong_site",
        "incomplete_names",
        "incomplete_organizations",
        "empty",
        "duplicate_name",
        "duplicate_organization",
        "token_name",
        "token_organization",
        "future_approval",
        "expired",
        "overlong_attestation",
        "naive_approval",
        "naive_expiry",
        "invalid_resolver_version",
        "invalid_approver",
    ],
)
def test_closed_lexicon_rejects_unattested_or_unsafe_values(
    tmp_path: Path,
    mutation: str,
) -> None:
    value = _value()
    if mutation == "extra_field":
        value["untrusted"] = "must fail closed"
    elif mutation == "wrong_schema":
        value["schema_version"] = "2.0"
    elif mutation == "wrong_site":
        value["site_id"] = "other.localhost"
    elif mutation == "incomplete_names":
        value["names_complete"] = False
    elif mutation == "incomplete_organizations":
        value["organizations_complete"] = 1
    elif mutation == "empty":
        value["names"] = []
        value["organizations"] = []
    elif mutation == "duplicate_name":
        value["names"] = ["Alice Zhang", "Alice Zhang"]
    elif mutation == "duplicate_organization":
        value["organizations"] = ["Example Trading LLC", "Example Trading LLC"]
    elif mutation == "token_name":
        value["names"] = ["<ENTITY_0123456789abcdef01234567>"]
    elif mutation == "token_organization":
        value["organizations"] = ["<EMAIL_0123456789abcdef01234567>"]
    elif mutation == "future_approval":
        value["approved_at"] = (NOW + timedelta(seconds=1)).isoformat()
    elif mutation == "expired":
        value["expires_at"] = NOW.isoformat()
    elif mutation == "overlong_attestation":
        value["expires_at"] = (NOW + timedelta(days=30)).isoformat()
        value["approved_at"] = (NOW - timedelta(seconds=1)).isoformat()
    elif mutation == "naive_approval":
        value["approved_at"] = NOW.replace(tzinfo=None).isoformat()
    elif mutation == "naive_expiry":
        value["expires_at"] = (NOW + timedelta(days=1)).replace(tzinfo=None).isoformat()
    elif mutation == "invalid_resolver_version":
        value["resolver_version"] = " version-with-padding "
    elif mutation == "invalid_approver":
        value["approved_by"] = "attacker\r\nforged"
    path = _private_json(tmp_path / "trusted-phrases.json", value)

    with pytest.raises(TrustedPhraseLexiconError) as captured:
        load_trusted_phrase_resolver(
            path,
            expected_site_id=SCOPE.site_id,
            clock=lambda: NOW,
        )

    assert "Alice Zhang" not in str(captured.value)
    assert "Example Trading LLC" not in repr(captured.value)


@pytest.mark.parametrize("unsafe_file", ["broad", "symlink", "empty", "oversized"])
def test_lexicon_requires_bounded_private_regular_non_symlink_file(
    tmp_path: Path,
    unsafe_file: str,
) -> None:
    path = _private_json(tmp_path / "trusted-phrases.json", _value())
    if unsafe_file == "broad":
        os.chmod(path, 0o640)
    elif unsafe_file == "symlink":
        target = path
        path = tmp_path / "trusted-phrases-link.json"
        path.symlink_to(target)
    elif unsafe_file == "empty":
        path.write_bytes(b"")
    else:
        path.write_bytes(b"{" + b" " * 65_536 + b"}")

    with pytest.raises(TrustedPhraseLexiconError):
        load_trusted_phrase_resolver(
            path,
            expected_site_id=SCOPE.site_id,
            clock=lambda: NOW,
        )


@pytest.mark.parametrize(
    ("field", "value_factory"),
    [
        ("names", lambda: ["x" * 513]),
        ("organizations", lambda: ["ok", "bad\x00phrase"]),
        ("names", lambda: [f"Person {index}" for index in range(1_001)]),
        ("organizations", lambda: {"not": "an array"}),
    ],
)
def test_phrase_arrays_are_bounded_strings(
    tmp_path: Path,
    field: str,
    value_factory: Callable[[], object],
) -> None:
    value = _value()
    value[field] = value_factory()
    path = _private_json(tmp_path / "trusted-phrases.json", value)

    with pytest.raises(TrustedPhraseLexiconError):
        load_trusted_phrase_resolver(
            path,
            expected_site_id=SCOPE.site_id,
            clock=lambda: NOW,
        )
