from __future__ import annotations

import importlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from observer.models import TenantScope

ROOT = Path(__file__).parents[2]
MIGRATION = (
    ROOT / "services" / "observer" / "migrations" / "009_local_pilot_identity_resolution.sql"
)
DIGEST_MIGRATION = (
    ROOT / "services" / "observer" / "migrations" / "013_local_pilot_identity_digest_boundary.sql"
)
NOW = datetime(2026, 8, 9, 9, tzinfo=UTC)
SCOPE = TenantScope("alpha.example", "observation_processing")
MAPPING_REF = "EID-01K" + "A" * 23
SUBJECT_REF = "extid:v1:email:N6juwc4ZaH0TL-KQUdymKdFk4sSVi6FB1fQTOjPwaI8"
TARGET_REF = "protected-user@example.invalid"


def _module():
    return importlib.import_module("observer.identity_resolution")


def _resolution(**overrides: object):
    module = _module()
    values: dict[str, object] = {
        "site_id": SCOPE.site_id,
        "identity_provider": "email",
        "external_subject_ref": SUBJECT_REF,
        "mapping_ref": MAPPING_REF,
        "mapping_revision": 1,
        "team_ref": "team-sales",
        "target_type": "User",
        "target_ref": TARGET_REF,
        "status": "confirmed",
        "resolved_at": NOW,
        "recorded_at": NOW + timedelta(seconds=1),
    }
    values.update(overrides)
    return module.ParticipantIdentityResolution(**values)


def test_migration_adds_protected_connector_owner_and_forced_rls_projection() -> None:
    sql = MIGRATION.read_text()

    assert "ADD COLUMN IF NOT EXISTS account_user_ref text" in sql
    assert "CREATE TABLE IF NOT EXISTS observer.participant_identity_resolutions" in sql
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "participant_identity_resolutions_site_isolation" in sql
    assert "enforce_identity_resolution_insert" in sql
    assert "pg_advisory_xact_lock" in sql
    assert "REVOKE ALL ON observer.participant_identity_resolutions FROM PUBLIC" in sql
    assert "SELECT, INSERT, UPDATE" in sql
    for forbidden in (
        "display_name text",
        "phone text",
        "prompt text",
        "response text",
        "jsonb",
    ):
        assert forbidden not in sql.lower()


def test_digest_boundary_migration_is_idempotent_rls_safe_and_canonical() -> None:
    sql = DIGEST_MIGRATION.read_text(encoding="utf-8").lower()

    assert "participant_identity_resolutions_digest_ref_ck" in sql
    assert "identity_resolution_work_digest_ref_ck" in sql
    assert "identity_authority_denials_digest_ref_ck" in sql
    assert "participants_external_identity_digest_ref_ck" in sql
    assert "[a-za-z0-9_-]{43}" in sql
    assert sql.count("force row level security") >= 3
    assert "revoke all on observer.participant_identity_resolutions" in sql
    assert "revoke all on observer.identity_resolution_work" in sql
    assert "revoke all on observer.identity_authority_denials" in sql


def test_resolution_projection_validates_closed_contract_and_redacts_protected_refs() -> None:
    resolution = _resolution()

    assert resolution.external_subject_ref == SUBJECT_REF
    assert resolution.target_ref == TARGET_REF
    assert SUBJECT_REF not in repr(resolution)
    assert TARGET_REF not in repr(resolution)
    assert "external_subject_ref=<redacted>" in repr(resolution)
    assert "target_ref=<redacted>" in repr(resolution)


@pytest.mark.parametrize(
    "changes",
    [
        {"identity_provider": "smtp"},
        {"external_subject_ref": "extid:v1:email:raw@example.invalid"},
        {"external_subject_ref": "extid:v1:email:13800138000"},
        {"external_subject_ref": "extid:v1:email:internal-user"},
        {"external_subject_ref": "extid:v1:email:" + "A" * 42},
        {"external_subject_ref": "extid:v1:email:" + "A" * 44},
        {"external_subject_ref": SUBJECT_REF + "="},
        {"external_subject_ref": "extid:v1:email:" + "A" * 42 + "+"},
        {"external_subject_ref": "extid:v1:wecom:opaque-token"},
        {"mapping_ref": "EID-not-a-ulid"},
        {"mapping_revision": 0},
        {"mapping_revision": True},
        {"team_ref": "team\nsales"},
        {"target_type": "Contact"},
        {"target_ref": " protected-user"},
        {"target_ref": "protected\x7fuser"},
        {"status": "pending"},
        {"resolved_at": datetime(2026, 8, 9, 9)},
    ],
)
def test_resolution_projection_rejects_open_or_untrusted_values_without_echo(
    changes: dict[str, object],
) -> None:
    sentinel = str(next(iter(changes.values())))

    with pytest.raises((TypeError, ValueError)) as caught:
        _resolution(**changes)

    assert sentinel not in str(caught.value)


def test_in_memory_projection_is_idempotent_and_revocation_removes_authority() -> None:
    module = _module()
    repository = module.InMemoryIdentityResolutionRepository()
    confirmed = _resolution()

    assert repository.record(SCOPE, confirmed) is confirmed
    assert repository.record(SCOPE, confirmed) is confirmed
    assert repository.latest(SCOPE, "email", SUBJECT_REF) is confirmed

    revoked = replace(
        confirmed,
        mapping_revision=2,
        status="revoked",
        resolved_at=NOW + timedelta(minutes=1),
        recorded_at=NOW + timedelta(minutes=1, seconds=1),
    )
    assert repository.record(SCOPE, revoked) is revoked
    assert repository.latest(SCOPE, "email", SUBJECT_REF) is revoked
    assert repository.history(SCOPE, "email", SUBJECT_REF) == (confirmed, revoked)


def test_exact_authoritative_replay_keeps_original_local_recorded_at() -> None:
    module = _module()
    repository = module.InMemoryIdentityResolutionRepository()
    confirmed = _resolution()
    repository.record(SCOPE, confirmed)

    replay = replace(confirmed, recorded_at=confirmed.recorded_at + timedelta(hours=1))

    assert repository.record(SCOPE, replay) is confirmed
    assert repository.history(SCOPE, "email", SUBJECT_REF) == (confirmed,)


@pytest.mark.parametrize(
    "changes",
    [
        {"mapping_revision": 1, "status": "revoked"},
        {"mapping_revision": 2, "mapping_ref": "EID-01K" + "B" * 23},
        {"mapping_revision": 2, "team_ref": "team-other"},
        {"mapping_revision": 2, "target_type": "Party", "target_ref": "PARTY-001"},
        {"mapping_revision": 2, "target_ref": "another-user@example.invalid"},
    ],
)
def test_in_memory_projection_rejects_same_revision_and_identity_drift(
    changes: dict[str, object],
) -> None:
    module = _module()
    repository = module.InMemoryIdentityResolutionRepository()
    repository.record(SCOPE, _resolution())

    with pytest.raises(module.IdentityResolutionConflict) as caught:
        repository.record(SCOPE, _resolution(**changes))

    assert SUBJECT_REF not in str(caught.value)
    assert TARGET_REF not in str(caught.value)


def test_in_memory_projection_rejects_stale_unseen_revision_and_aba_reconfirmation() -> None:
    module = _module()
    repository = module.InMemoryIdentityResolutionRepository()
    first = _resolution(mapping_revision=3)
    revoked = replace(
        first,
        mapping_revision=4,
        status="revoked",
        resolved_at=NOW + timedelta(minutes=1),
        recorded_at=NOW + timedelta(minutes=1, seconds=1),
    )
    repository.record(SCOPE, first)
    repository.record(SCOPE, revoked)

    with pytest.raises(module.IdentityResolutionConflict, match="stale"):
        repository.record(SCOPE, replace(first, mapping_revision=2))
    with pytest.raises(module.IdentityResolutionConflict, match="transition"):
        repository.record(
            SCOPE,
            replace(
                first,
                mapping_revision=5,
                resolved_at=NOW + timedelta(minutes=2),
                recorded_at=NOW + timedelta(minutes=2, seconds=1),
            ),
        )
