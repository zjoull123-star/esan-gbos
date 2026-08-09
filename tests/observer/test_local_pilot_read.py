from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from observer.models import TenantScope
from observer.read_service import (
    CommunicationAccess,
    CommunicationDetail,
    CommunicationPage,
    CommunicationSummary,
    InvalidCursor,
    LocalPilotReadService,
    PostgresCommunicationRepository,
    RawAccessDenied,
    ScopeMismatch,
)

NOW = datetime(2026, 8, 8, 9, tzinfo=UTC)
SCOPE = TenantScope("alpha.example", "observation_processing")
SUMMARY = CommunicationSummary(
    observation_id="event-001",
    channel="email",
    occurred_at=NOW,
    summary_zh="客户询问交期",
    original_language="zh-CN",
    classification="Restricted",
    review_status="unreviewed",
    team_ref="team-sales",
    party_ref="party-001",
    evidence_count=1,
)
MODEL = {"name": "deepseek-v4-flash", "version": "2026-08-08"}


class FakeReadRepository:
    def __init__(self) -> None:
        self.before: tuple[datetime, str] | None = None
        self.raw_policies: list[str] = []

    def list_communications(
        self,
        scope: TenantScope,
        access: CommunicationAccess,
        *,
        channel: str | None,
        classification: str | None,
        review_status: str | None,
        before: tuple[datetime, str] | None,
        limit: int,
    ) -> tuple[CommunicationSummary, ...]:
        assert scope == SCOPE and access.team_refs == frozenset({"team-sales"})
        assert channel is None and classification is None and review_status is None
        self.before = before
        assert limit == 2
        return (SUMMARY,)

    def get_communication(
        self,
        scope: TenantScope,
        observation_id: str,
        *,
        access: CommunicationAccess,
        raw_policy: str,
    ) -> CommunicationDetail | None:
        assert scope == SCOPE and observation_id == "event-001"
        assert isinstance(access, CommunicationAccess)
        self.raw_policies.append(raw_policy)
        return CommunicationDetail(
            summary=SUMMARY,
            evidence=({"ref": "ev-001", "locator": "bytes:0-12"},),
            fact_proposals=(),
            association_suggestions=(),
            model=MODEL,
            original_text="restricted source body",
        )


def test_list_uses_team_scoped_opaque_cursor() -> None:
    repository = FakeReadRepository()
    service = LocalPilotReadService(repository=repository, cursor_secret=b"x" * 32)
    access = CommunicationAccess(team_refs=frozenset({"team-sales"}))

    first = service.list_communications(SCOPE, access, page_size=1)
    assert isinstance(first, CommunicationPage)
    assert first.communications == (SUMMARY,)
    assert first.next_cursor is None

    with pytest.raises(InvalidCursor):
        service.list_communications(SCOPE, access, page_size=1, cursor="not-a-cursor")


def test_get_denies_restricted_raw_by_default_and_enforces_team_scope() -> None:
    repository = FakeReadRepository()
    service = LocalPilotReadService(
        repository=repository,
        cursor_secret=b"x" * 32,
    )
    access = CommunicationAccess(team_refs=frozenset({"team-sales"}))

    detail = service.get_communication(SCOPE, access, observation_id="event-001")
    assert detail.original_text is None
    assert detail.raw_access_allowed is False
    assert repository.raw_policies == ["omit"]

    with pytest.raises(RawAccessDenied):
        service.get_communication(
            SCOPE,
            access,
            observation_id="event-001",
            include_raw=True,
        )
    with pytest.raises(ScopeMismatch):
        service.get_communication(
            SCOPE,
            CommunicationAccess(team_refs=frozenset({"team-finance"})),
            observation_id="event-001",
        )


def test_detail_requires_closed_model_metadata_for_frappe_v4() -> None:
    with pytest.raises(ValueError, match="model"):
        CommunicationDetail(
            summary=SUMMARY,
            evidence=(),
            fact_proposals=(),
            association_suggestions=(),
            model=None,  # type: ignore[arg-type]
            original_text=None,
        )


def test_postgres_communication_repository_is_a_real_projection_adapter() -> None:
    repository = PostgresCommunicationRepository(connection=object())

    assert repr(repository) == "PostgresCommunicationRepository(connection=<redacted>)"


def _identity_resolution(
    *,
    subject: str,
    target_type: str,
    target_ref: str,
    team_ref: str = "team-sales",
    revision: int = 1,
    status: str = "confirmed",
):
    from observer.identity_resolution import ParticipantIdentityResolution

    return ParticipantIdentityResolution(
        site_id=SCOPE.site_id,
        identity_provider="email",
        external_subject_ref=subject,
        mapping_ref="EID-01K" + ("A" if target_type == "User" else "B") * 23,
        mapping_revision=revision,
        team_ref=team_ref,
        target_type=target_type,
        target_ref=target_ref,
        status=status,
        resolved_at=NOW + timedelta(minutes=revision),
        recorded_at=NOW + timedelta(minutes=revision, seconds=1),
    )


def _in_memory_reader(*resolutions: object) -> LocalPilotReadService:
    from observer.identity_resolution import InMemoryIdentityResolutionRepository
    from observer.read_service import InMemoryCommunicationRepository

    identity_repository = InMemoryIdentityResolutionRepository()
    for resolution in resolutions:
        identity_repository.record(SCOPE, resolution)
    repository = InMemoryCommunicationRepository(identity_repository=identity_repository)
    repository.put(
        SCOPE,
        CommunicationDetail(
            summary=replace(SUMMARY, party_ref="legacy-party", actor_refs=frozenset()),
            evidence=(),
            fact_proposals=(),
            association_suggestions=(),
            model=MODEL,
            original_text="restricted source body",
        ),
        participant_refs=(
            "extid:v1:email:user-opaque",
            "extid:v1:email:party-opaque",
        ),
    )
    return LocalPilotReadService(repository=repository, cursor_secret=b"x" * 32)


def test_confirmed_user_projection_grants_self_access_but_raw_participant_never_does() -> None:
    actor = "protected-user@example.invalid"
    outside_team = CommunicationAccess(
        team_refs=frozenset({"team-finance"}),
        actor_ref=actor,
    )

    unresolved = _in_memory_reader()
    assert unresolved.list_communications(SCOPE, outside_team).communications == ()
    with pytest.raises(ScopeMismatch):
        unresolved.get_communication(SCOPE, outside_team, observation_id="event-001")

    confirmed = _in_memory_reader(
        _identity_resolution(
            subject="extid:v1:email:user-opaque",
            target_type="User",
            target_ref=actor,
        )
    )
    page = confirmed.list_communications(SCOPE, outside_team)
    assert [item.observation_id for item in page.communications] == ["event-001"]
    assert (
        confirmed.get_communication(
            SCOPE,
            outside_team,
            observation_id="event-001",
        ).summary.observation_id
        == "event-001"
    )
    assert actor not in repr(outside_team)


def test_confirmed_party_projection_enriches_without_using_immutable_event_party_ref() -> None:
    party = _identity_resolution(
        subject="extid:v1:email:party-opaque",
        target_type="Party",
        target_ref="PARTY-001",
    )
    reader = _in_memory_reader(party)
    access = CommunicationAccess(team_refs=frozenset({"team-sales"}))

    detail = reader.get_communication(SCOPE, access, observation_id="event-001")

    assert detail.summary.party_ref == "PARTY-001"
    assert SUMMARY.party_ref == "party-001"


def test_revoked_and_cross_team_projections_never_grant_or_enrich() -> None:
    actor = "protected-user@example.invalid"
    confirmed_user = _identity_resolution(
        subject="extid:v1:email:user-opaque",
        target_type="User",
        target_ref=actor,
    )
    revoked_user = replace(
        confirmed_user,
        mapping_revision=2,
        status="revoked",
        resolved_at=NOW + timedelta(minutes=2),
        recorded_at=NOW + timedelta(minutes=2, seconds=1),
    )
    cross_team_party = _identity_resolution(
        subject="extid:v1:email:party-opaque",
        target_type="Party",
        target_ref="PARTY-OTHER",
        team_ref="team-other",
    )
    reader = _in_memory_reader(confirmed_user, revoked_user, cross_team_party)

    assert (
        reader.list_communications(
            SCOPE,
            CommunicationAccess(team_refs=frozenset({"team-finance"}), actor_ref=actor),
        ).communications
        == ()
    )
    detail = reader.get_communication(
        SCOPE,
        CommunicationAccess(team_refs=frozenset({"*"}), allow_all_teams=True),
        observation_id="event-001",
    )
    assert detail.summary.party_ref is None
    assert detail.raw_access_allowed is False


def test_in_memory_filters_access_before_limit_so_unauthorized_rows_do_not_starve_page() -> None:
    from observer.identity_resolution import InMemoryIdentityResolutionRepository
    from observer.read_service import InMemoryCommunicationRepository

    repository = InMemoryCommunicationRepository(
        identity_repository=InMemoryIdentityResolutionRepository()
    )
    repository.put(
        SCOPE,
        CommunicationDetail(
            summary=replace(
                SUMMARY,
                observation_id="unauthorized-newer",
                occurred_at=NOW + timedelta(minutes=1),
                team_ref="team-other",
            ),
            evidence=(),
            fact_proposals=(),
            association_suggestions=(),
            model=MODEL,
            original_text=None,
        ),
        participant_refs=(),
    )
    repository.put(
        SCOPE,
        CommunicationDetail(
            summary=replace(SUMMARY, observation_id="authorized-older"),
            evidence=(),
            fact_proposals=(),
            association_suggestions=(),
            model=MODEL,
            original_text=None,
        ),
        participant_refs=(),
    )
    reader = LocalPilotReadService(repository=repository, cursor_secret=b"x" * 32)

    page = reader.list_communications(
        SCOPE,
        CommunicationAccess(team_refs=frozenset({"team-sales"})),
        page_size=1,
    )

    assert [item.observation_id for item in page.communications] == ["authorized-older"]
