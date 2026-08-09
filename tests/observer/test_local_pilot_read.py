from __future__ import annotations

from datetime import UTC, datetime

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
        raw_policy: str,
    ) -> CommunicationDetail | None:
        assert scope == SCOPE and observation_id == "event-001"
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
