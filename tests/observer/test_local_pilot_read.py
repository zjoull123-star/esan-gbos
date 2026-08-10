from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from observer.models import TenantScope
from observer.read_service import (
    IDENTITY_SELF_ACCESS_MAX_AGE,
    CommunicationAccess,
    CommunicationDetail,
    CommunicationNotFound,
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
USER_IDENTITY_REF = "extid:v1:email:BPiZbadjt6lpsQKO4wB1aerzpjVIbdqyEdUSyFud-Ps"
PARTY_IDENTITY_REF = "extid:v1:email:HQ_qOewz_3VD80W-hdHM001thkKX1BUbc3gCyylKM4w"


class _AuthorityFreshness:
    def __init__(
        self,
        fresh_identity_refs: frozenset[str],
        denied_identity_refs: frozenset[str] = frozenset(),
    ) -> None:
        self.fresh_identity_refs = set(fresh_identity_refs)
        self.denied_identity_refs = set(denied_identity_refs)
        self.calls: list[tuple[str, str, str, datetime, timedelta]] = []

    def is_confirmed_fresh(
        self,
        scope: TenantScope,
        identity_provider: str,
        identity_ref: str,
        team_ref: str,
        *,
        now: datetime,
        max_age: timedelta,
    ) -> bool:
        assert scope == SCOPE
        assert identity_provider == "email"
        self.calls.append((identity_ref, team_ref, identity_provider, now, max_age))
        return identity_ref in self.fresh_identity_refs

    def is_denied(
        self,
        scope: TenantScope,
        identity_provider: str,
        identity_ref: str,
        team_ref: str,
        mapping_ref: str,
        *,
        mapping_revision: int,
    ) -> bool:
        del mapping_ref, mapping_revision
        assert scope == SCOPE
        assert identity_provider == "email"
        assert team_ref == "team-sales"
        return identity_ref in self.denied_identity_refs


class _SqlCursor:
    def __init__(self, connection: _SqlConnection) -> None:
        self.connection = connection
        self.response: object = None

    def __enter__(self) -> _SqlCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
        normalized = " ".join(sql.split())
        self.connection.executed.append((normalized, params))
        if "set_config" not in normalized:
            self.response = self.connection.responses.pop(0)

    def fetchone(self) -> tuple[object, ...] | None:
        if isinstance(self.response, tuple):
            return self.response
        return None

    def fetchall(self) -> list[tuple[object, ...]]:
        if isinstance(self.response, list):
            return self.response
        return []


class _SqlConnection:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.executed: list[tuple[str, tuple[object, ...] | None]] = []

    def transaction(self) -> nullcontext[None]:
        return nullcontext()

    def cursor(self) -> _SqlCursor:
        return _SqlCursor(self)


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


def _in_memory_reader(
    *resolutions: object,
    fresh_identity_refs: frozenset[str] = frozenset(),
) -> LocalPilotReadService:
    from observer.identity_resolution import InMemoryIdentityResolutionRepository
    from observer.read_service import InMemoryCommunicationRepository

    identity_repository = InMemoryIdentityResolutionRepository()
    for resolution in resolutions:
        identity_repository.record(SCOPE, resolution)
    repository = InMemoryCommunicationRepository(
        identity_repository=identity_repository,
        authority_freshness=_AuthorityFreshness(fresh_identity_refs),
        clock=lambda: NOW + timedelta(hours=1),
    )
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
            USER_IDENTITY_REF,
            PARTY_IDENTITY_REF,
        ),
    )
    return LocalPilotReadService(repository=repository, cursor_secret=b"x" * 32)


def _in_memory_detail_reader(
    *resolutions: object,
    association_suggestions: tuple[dict[str, object], ...] = (),
    connector_account_user_ref: str | None = None,
    participant_refs: tuple[str, ...] = (
        USER_IDENTITY_REF,
        PARTY_IDENTITY_REF,
    ),
) -> LocalPilotReadService:
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
            association_suggestions=association_suggestions,
            model=MODEL,
            original_text=None,
            connector_account_user_ref=connector_account_user_ref,
        ),
        participant_refs=participant_refs,
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

    confirmed_but_stale = _in_memory_reader(
        _identity_resolution(
            subject=USER_IDENTITY_REF,
            target_type="User",
            target_ref=actor,
        )
    )
    assert confirmed_but_stale.list_communications(SCOPE, outside_team).communications == ()
    with pytest.raises(ScopeMismatch):
        confirmed_but_stale.get_communication(
            SCOPE,
            outside_team,
            observation_id="event-001",
        )

    confirmed = _in_memory_reader(
        _identity_resolution(
            subject=USER_IDENTITY_REF,
            target_type="User",
            target_ref=actor,
        ),
        fresh_identity_refs=frozenset({USER_IDENTITY_REF}),
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


def test_durable_authority_denial_blocks_self_access_before_worker_polling() -> None:
    from observer.identity_resolution import InMemoryIdentityResolutionRepository
    from observer.read_service import InMemoryCommunicationRepository

    subject = USER_IDENTITY_REF
    actor = "protected-user@example.invalid"
    resolution = _identity_resolution(
        subject=subject,
        target_type="User",
        target_ref=actor,
        revision=4,
    )
    identity_repository = InMemoryIdentityResolutionRepository()
    identity_repository.record(SCOPE, resolution)
    authority = _AuthorityFreshness(frozenset({subject}))
    repository = InMemoryCommunicationRepository(
        identity_repository=identity_repository,
        authority_freshness=authority,
        clock=lambda: NOW + timedelta(hours=1),
    )
    repository.put(
        SCOPE,
        CommunicationDetail(
            summary=replace(SUMMARY, party_ref=None, actor_refs=frozenset()),
            evidence=(),
            fact_proposals=(),
            association_suggestions=(),
            model=MODEL,
            original_text=None,
        ),
        participant_refs=(subject,),
    )
    reader = LocalPilotReadService(repository=repository, cursor_secret=b"x" * 32)
    self_access = CommunicationAccess(
        team_refs=frozenset({"team-finance"}),
        actor_ref=actor,
    )

    assert [
        item.observation_id
        for item in reader.list_communications(SCOPE, self_access).communications
    ] == ["event-001"]

    authority.denied_identity_refs.add(subject)

    assert reader.list_communications(SCOPE, self_access).communications == ()
    with pytest.raises(ScopeMismatch):
        reader.get_communication(SCOPE, self_access, observation_id="event-001")
    latest = identity_repository.latest(SCOPE, "email", subject)
    assert latest is resolution and latest.status == "confirmed"


def test_postgres_self_access_requires_current_fresh_undenied_confirmed_authority() -> None:
    connection = _SqlConnection([[]])
    repository = PostgresCommunicationRepository(connection=connection)

    assert (
        repository.list_communications(
            SCOPE,
            CommunicationAccess(
                team_refs=frozenset({"team-finance"}),
                actor_ref="protected-user@example.invalid",
            ),
            channel=None,
            classification=None,
            review_status=None,
            before=None,
            limit=20,
        )
        == ()
    )

    query, _params = connection.executed[1]
    assert query.count("observer.identity_resolution_work") >= 2
    assert query.count("observer.identity_authority_denials") >= 2
    assert "authority.last_resolution_status = 'confirmed'" in query
    assert "authority.status = 'confirmed'" in query
    assert "authority.identity_ref = actor.identity_ref" in query
    assert "authority.team_ref = event.team_ref" in query
    assert "current_timestamp - make_interval" in query
    assert "secs => 7200" in query
    assert "denial.deny_through_revision >= latest_actor.mapping_revision" in query
    assert timedelta(hours=2) == IDENTITY_SELF_ACCESS_MAX_AGE


def test_confirmed_party_projection_enriches_without_using_immutable_event_party_ref() -> None:
    party = _identity_resolution(
        subject=PARTY_IDENTITY_REF,
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
        subject=USER_IDENTITY_REF,
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
        subject=PARTY_IDENTITY_REF,
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


def test_detail_exposes_closed_unresolved_identity_views_without_raw_participants() -> None:
    reader = _in_memory_detail_reader(
        participant_refs=(
            USER_IDENTITY_REF,
            "raw-person@example.invalid",
        )
    )

    detail = reader.get_communication(
        SCOPE,
        CommunicationAccess(team_refs=frozenset({"team-sales"})),
        observation_id="event-001",
    )

    assert [identity.as_dict() for identity in detail.participant_identities] == [
        {
            "identity_ref": USER_IDENTITY_REF,
            "provider": "email",
            "status": "unresolved",
        }
    ]
    rendered = detail.as_dict()
    assert "raw-person@example.invalid" not in repr(rendered)
    assert "target_ref" not in rendered["participant_identities"][0]


def test_detail_identity_views_show_confirmed_and_revoked_metadata_without_targets() -> None:
    protected_user = "protected-user@example.invalid"
    protected_party = "PARTY-PROTECTED-001"
    confirmed_user = _identity_resolution(
        subject=USER_IDENTITY_REF,
        target_type="User",
        target_ref=protected_user,
    )
    confirmed_party = _identity_resolution(
        subject=PARTY_IDENTITY_REF,
        target_type="Party",
        target_ref=protected_party,
    )
    revoked_party = replace(
        confirmed_party,
        mapping_revision=2,
        status="revoked",
        resolved_at=NOW + timedelta(minutes=2),
        recorded_at=NOW + timedelta(minutes=2, seconds=1),
    )
    reader = _in_memory_detail_reader(confirmed_user, confirmed_party, revoked_party)

    detail = reader.get_communication(
        SCOPE,
        CommunicationAccess(team_refs=frozenset({"team-sales"})),
        observation_id="event-001",
    )
    identities = {item.identity_ref: item.as_dict() for item in detail.participant_identities}

    assert identities[USER_IDENTITY_REF] == {
        "identity_ref": USER_IDENTITY_REF,
        "provider": "email",
        "status": "confirmed",
        "mapping_ref": confirmed_user.mapping_ref,
        "mapping_revision": 1,
        "target_type": "User",
    }
    assert identities[PARTY_IDENTITY_REF]["status"] == "revoked"
    assert identities[PARTY_IDENTITY_REF]["mapping_revision"] == 2
    rendered = repr(detail.as_dict())
    assert protected_user not in rendered
    assert protected_party not in rendered
    assert protected_user not in repr(detail)


def test_connector_account_owner_is_separate_from_participant_identity_views() -> None:
    owner = "USER-OWNER-001"
    reader = _in_memory_detail_reader(connector_account_user_ref=owner)

    detail = reader.get_communication(
        SCOPE,
        CommunicationAccess(team_refs=frozenset({"team-sales"})),
        observation_id="event-001",
    )
    rendered = detail.as_dict()

    assert rendered["connector_account_user_ref"] == owner
    assert all(identity["identity_ref"] != owner for identity in rendered["participant_identities"])
    assert owner not in repr(detail)


def test_association_suggestion_key_is_stable_closed_and_bound_to_observation() -> None:
    suggestion = {
        "type": "party",
        "target_ref": "PARTY-001",
        "confidence": 0.88,
    }
    reader = _in_memory_detail_reader(association_suggestions=(suggestion,))
    access = CommunicationAccess(team_refs=frozenset({"team-sales"}))

    first = reader.get_communication(SCOPE, access, observation_id="event-001")
    replay = reader.get_communication(SCOPE, access, observation_id="event-001")
    first_suggestion = first.association_suggestions[0]

    assert first_suggestion == replay.association_suggestions[0]
    assert set(first_suggestion) == {
        "type",
        "target_ref",
        "confidence",
        "suggestion_key",
    }
    assert first_suggestion["type"] == suggestion["type"]
    assert first_suggestion["target_ref"] == suggestion["target_ref"]
    assert first_suggestion["confidence"] == suggestion["confidence"]
    assert str(first_suggestion["suggestion_key"]).startswith("suggestion:v1:")

    other_observation = replace(
        first,
        summary=replace(first.summary, observation_id="event-002"),
        association_suggestions=(suggestion,),
    ).as_dict()["association_suggestions"][0]
    assert other_observation["suggestion_key"] != first_suggestion["suggestion_key"]

    with pytest.raises(ValueError, match="association suggestion"):
        replace(
            first,
            association_suggestions=({**suggestion, "arbitrary": "forbidden"},),
        ).as_dict()


def test_cross_team_identity_stays_unresolved_and_cross_site_detail_is_absent() -> None:
    cross_team = _identity_resolution(
        subject=USER_IDENTITY_REF,
        target_type="User",
        target_ref="protected-user@example.invalid",
        team_ref="team-other",
    )
    reader = _in_memory_detail_reader(cross_team)
    access = CommunicationAccess(team_refs=frozenset({"team-sales"}))

    detail = reader.get_communication(SCOPE, access, observation_id="event-001")
    assert detail.participant_identities[0].status == "unresolved"
    assert detail.participant_identities[0].mapping_ref is None
    with pytest.raises(CommunicationNotFound):
        reader.get_communication(
            TenantScope("other.example", "observation_processing"),
            access,
            observation_id="event-001",
        )


def test_postgres_detail_projects_closed_identity_and_separate_connector_owner() -> None:
    mapping_ref = "EID-01K" + "A" * 23
    connection = _SqlConnection(
        [
            (
                "event-001",
                "email",
                NOW,
                "客户询问交期",
                "zh-CN",
                "Restricted",
                "AI Draft",
                "team-sales",
                None,
                0,
                ["protected-user@example.invalid"],
                [],
                [],
                "deepseek-v4-flash",
                "2026-08-08",
                "USER-OWNER-001",
            ),
            [],
            [
                (
                    USER_IDENTITY_REF,
                    "email",
                    "confirmed",
                    mapping_ref,
                    3,
                    "User",
                )
            ],
        ]
    )
    repository = PostgresCommunicationRepository(connection=connection)

    detail = repository.get_communication(
        SCOPE,
        "event-001",
        access=CommunicationAccess(team_refs=frozenset({"team-sales"})),
        raw_policy="omit",
    )

    assert detail is not None
    assert detail.connector_account_user_ref == "USER-OWNER-001"
    assert detail.participant_identities[0].as_dict() == {
        "identity_ref": USER_IDENTITY_REF,
        "provider": "email",
        "status": "confirmed",
        "mapping_ref": mapping_ref,
        "mapping_revision": 3,
        "target_type": "User",
    }
    detail_sql = connection.executed[1][0]
    identity_sql, identity_params = connection.executed[3]
    assert "connector.account_user_ref" in detail_sql
    assert "resolution.team_ref = CAST(%s AS text)" in identity_sql
    assert "resolution.target_ref" not in identity_sql
    assert "participant.display_name" not in identity_sql
    assert "observer.identity_authority_denials" in identity_sql
    assert "deny_through_revision >= latest.mapping_revision" in identity_sql
    assert identity_params == (
        "team-sales",
        "team-sales",
        SCOPE.site_id,
        "event-001",
    )
