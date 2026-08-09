from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from services.context.context_service.communication_intelligence import (
    PostgresCommunicationIntelligenceRepository,
)
from services.context.context_service.storage import connect_postgres_components
from services.observer.observer.model_projection import ContextIntelligencePublication
from services.observer.observer.models import TenantScope

pytestmark = pytest.mark.postgres_integration


def _connection() -> Any:
    if os.getenv("GBOS_RUN_CONTEXT_INTELLIGENCE_POSTGRES") != "1":
        pytest.skip("set GBOS_RUN_CONTEXT_INTELLIGENCE_POSTGRES=1 to run")
    return connect_postgres_components(
        host=os.environ["GBOS_GATE3_CONTEXT_HOST"],
        port=int(os.environ["GBOS_GATE3_CONTEXT_PORT"]),
        database=os.environ["GBOS_GATE3_CONTEXT_DATABASE"],
        user="gbos_context_app",
        password=os.environ["GBOS_GATE3_CONTEXT_PASSWORD"],
    )


def test_context_intelligence_publish_claim_heartbeat_and_site_fence() -> None:
    suffix = uuid4().hex
    scope = TenantScope(f"context-intelligence-{suffix}.localhost", "observation_processing")
    publication = ContextIntelligencePublication(
        site_id=scope.site_id,
        observation_id=f"observation-{suffix}",
        team_ref=f"team-{suffix}",
        evidence_refs=(f"evidence-{suffix}",),
        summary_zh="客户希望确认样品交期。",
        original_language="zh-CN",
        confidence=0.93,
        review_status="AI Draft",
        fact_proposals=(
            {
                "subject_ref": f"party-{suffix}",
                "predicate": "sample_delivery_intent",
                "value_display": "希望确认样品交期",
                "type": "text",
                "unit": None,
                "confidence": 0.91,
                "evidence_refs": [f"evidence-{suffix}"],
                "status": "proposed",
            },
        ),
        association_suggestions=(),
        model={"name": "deepseek-v4-flash", "version": "deepseek-v4-flash"},
        invocation_refs=(f"invocation-{suffix}",),
    )
    connection = _connection()
    try:
        repository = PostgresCommunicationIntelligenceRepository(
            connection,
            team_ref_resolver=lambda _scope, _observation_id: publication.team_ref,
        )
        repository.publish(
            scope,
            publication,
            idempotency_key=f"context-normalized:{publication.observation_id}",
        )
        repository.publish(
            scope,
            publication,
            idempotency_key=f"context-normalized:{publication.observation_id}",
        )
        now = datetime.now(UTC)
        claim = repository.claim_draft(
            scope.site_id,
            worker_id="communication-worker",
            now=now,
            lease_duration=timedelta(seconds=10),
        )
        assert claim is not None and claim.attempt == 1
        repository.heartbeat_draft(
            scope.site_id,
            claim.draft_id,
            worker_id="communication-worker",
            expected_attempt=claim.attempt,
            now=now + timedelta(seconds=1),
            lease_duration=timedelta(seconds=10),
        )
        other = TenantScope(f"other-{suffix}.localhost", scope.processing_purpose)
        assert (
            repository.claim_draft(
                other.site_id,
                worker_id="other-worker",
                now=now + timedelta(seconds=2),
                lease_duration=timedelta(seconds=10),
            )
            is None
        )
    finally:
        close = getattr(connection, "close", None)
        if callable(close):
            close()


def test_context_intelligence_migration_is_ledgered_and_forces_rls() -> None:
    connection = _connection()
    try:
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT migration_name, COUNT(*)
                FROM observer.schema_migrations
                WHERE migration_name = %s
                GROUP BY migration_name
                """,
                ("context/005_local_pilot_communication_intelligence.sql",),
            )
            assert cursor.fetchone() == (
                "context/005_local_pilot_communication_intelligence.sql",
                1,
            )
            cursor.execute(
                """
                SELECT count(*)
                FROM pg_class AS table_info
                JOIN pg_namespace AS namespace
                  ON namespace.oid = table_info.relnamespace
                WHERE namespace.nspname = 'context'
                  AND table_info.relname LIKE 'communication_%'
                  AND table_info.relrowsecurity
                  AND table_info.relforcerowsecurity
                """
            )
            assert cursor.fetchone() == (9,)
    finally:
        close = getattr(connection, "close", None)
        if callable(close):
            close()
