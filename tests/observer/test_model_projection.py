from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from observer.context_outbox import ContextOutboxPublisherWorker
from observer.evidence_store import ContentAddressedEvidenceStore
from observer.local_pilot_api import LocalPilotAPIConfig
from observer.local_pilot_storage import ContextOutboxMetadata
from observer.model_projection import (
    CommunicationIntelligenceResponse,
    ContentAddressedEvidenceTextLoader,
    ContextIntelligencePublication,
    EvidenceLineage,
    LoadedEvidenceText,
    LocalTokenizationResult,
    ObservationModelRequest,
    ObservationProjectionPublisher,
    ObservationProjectionSource,
    ProjectionFailure,
)
from observer.models import TenantScope
from observer.read_service import (
    CommunicationDetail,
    CommunicationSummary,
    PostgresCommunicationRepository,
)
from observer.runtime import compose_postgres_local_pilot_runtime

NOW = datetime(2026, 8, 8, 9, tzinfo=UTC)
SCOPE = TenantScope("alpha.example", "observation_processing")
SOURCE = ObservationProjectionSource(
    site_id=SCOPE.site_id,
    observation_id="event-001",
    channel="email",
    occurred_at=NOW,
    classification="Restricted",
    team_ref="team-sales",
    party_ref="party-001",
    participant_refs=("party-001",),
    evidence=(
        EvidenceLineage(
            evidence_ref="evidence-001",
            content_object_ref="obs:v1:alpha:sha256:" + ("a" * 64),
            media_type="message/rfc822",
            raw_sha256="a" * 64,
        ),
    ),
)


def _output() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "site_id": SCOPE.site_id,
        "observation_id": SOURCE.observation_id,
        "evidence_refs": ["evidence-001"],
        "summary_zh": "客户希望确认样品交期。",
        "original_language": "zh-CN",
        "confidence": 0.93,
        "review_status": "AI Draft",
        "fact_proposals": [
            {
                "subject_ref": "party-001",
                "predicate": "sample_delivery_intent",
                "value_display": "希望确认样品交期",
                "type": "text",
                "unit": None,
                "confidence": 0.91,
                "evidence_refs": ["evidence-001"],
                "status": "proposed",
            }
        ],
        "association_suggestions": [
            {
                "type": "party",
                "target_ref": "party-001",
                "confidence": 0.88,
                "evidence_refs": ["evidence-001"],
            }
        ],
    }


class FakeRepository:
    def __init__(self, source: ObservationProjectionSource = SOURCE) -> None:
        self.source = source
        self.stored: list[object] = []
        self.order: list[str] = []

    def load_projection_source(
        self,
        scope: TenantScope,
        observation_id: str,
    ) -> ObservationProjectionSource:
        assert scope == SCOPE and observation_id == SOURCE.observation_id
        return self.source

    def store_projection(
        self,
        scope: TenantScope,
        detail: object,
        *,
        projected_at: datetime,
    ) -> None:
        assert scope == SCOPE and projected_at == NOW
        self.order.append("store")
        self.stored.append(detail)


class FakeProvider:
    def __init__(
        self,
        *,
        output: dict[str, Any] | None = None,
        model_name: str = "deepseek-v4-flash",
    ) -> None:
        self.requests: list[ObservationModelRequest] = []
        self.response = CommunicationIntelligenceResponse(
            output=_output() if output is None else output,
            model_name=model_name,
            model_version="2026-08-08",
            invocation_refs=("invocation-001",),
        )

    def project(
        self,
        request: ObservationModelRequest,
    ) -> CommunicationIntelligenceResponse:
        self.requests.append(request)
        return self.response


class FakeContextPublisher:
    def __init__(
        self,
        repository: FakeRepository,
        *,
        fail: bool = False,
    ) -> None:
        self.repository = repository
        self.fail = fail
        self.calls: list[tuple[str, object]] = []

    def publish(
        self,
        scope: TenantScope,
        publication: object,
        *,
        idempotency_key: str,
    ) -> None:
        assert scope == SCOPE
        self.repository.order.append("context")
        self.calls.append((idempotency_key, publication))
        if self.fail:
            raise RuntimeError("context unavailable with secret detail")


def _tokenize(
    scope: TenantScope,
    observation_id: str,
    raw_text: str,
) -> LocalTokenizationResult:
    assert scope == SCOPE and observation_id == SOURCE.observation_id
    assert "alice@example.com" in raw_text
    return LocalTokenizationResult(
        text="客户 <EMAIL_1> 希望确认样品交期",
        receipt_ref="tokenization-001",
        tokenizer_version="stable-tokenizer-v1",
        mapping_digest="b" * 64,
    )


def _publisher(
    *,
    repository: FakeRepository | None = None,
    provider: FakeProvider | None = None,
    context: FakeContextPublisher | None = None,
    tokenizer: object = _tokenize,
) -> tuple[
    ObservationProjectionPublisher,
    FakeRepository,
    FakeProvider,
    FakeContextPublisher,
]:
    active_repository = repository or FakeRepository()
    active_provider = provider or FakeProvider()
    active_context = context or FakeContextPublisher(active_repository)
    return (
        ObservationProjectionPublisher(
            repository=active_repository,
            raw_loader=lambda scope, evidence: LoadedEvidenceText(
                evidence_ref=evidence.evidence_ref,
                text="客户 alice@example.com 希望确认样品交期",
            ),
            tokenizer=tokenizer,
            provider=active_provider,
            context_publisher=active_context,
            clock=lambda: NOW,
            restricted_policy="local_tokenized",
        ),
        active_repository,
        active_provider,
        active_context,
    )


def test_restricted_observation_is_tokenized_validated_and_published_before_projection() -> None:
    publisher, repository, provider, context = _publisher()

    result = publisher(
        SCOPE,
        SOURCE.observation_id,
        "context-normalized:event-001",
    )

    assert result.status == "projected"
    request = provider.requests[0]
    assert request.input_mode == "local_tokenized"
    assert request.processing_purpose == SCOPE.processing_purpose
    assert request.evidence_refs == ("evidence-001",)
    assert request.tokenization_refs == ("tokenization-001",)
    assert request.mapping_digest == "b" * 64
    assert request.idempotency_key == "context-normalized:event-001"
    rendered = repr(request)
    assert "alice@example.com" not in rendered
    assert SCOPE.processing_purpose not in rendered
    assert "<EMAIL_1>" not in rendered
    assert "evidence-001" not in rendered
    assert "<redacted>" in rendered
    with pytest.raises(ValueError, match="processing purpose"):
        replace(request, processing_purpose="model_controlled_purpose")
    assert repository.order == ["context", "store"]
    assert context.calls[0][0] == "context-normalized:event-001"
    publication = context.calls[0][1]
    assert publication.site_id == SCOPE.site_id
    assert publication.team_ref == "team-sales"
    assert publication.review_status == "AI Draft"
    assert publication.fact_proposals[0]["subject_ref"] == "party-001"
    assert publication.fact_proposals[0]["evidence_refs"] == ["evidence-001"]
    detail = repository.stored[0]
    assert detail.summary.review_status == "AI Draft"
    assert detail.original_text is None
    assert detail.fact_proposals == (
        {
            "status": "proposed",
            "confidence": 0.91,
            "type": "text",
            "value_display": "希望确认样品交期",
        },
    )
    assert detail.association_suggestions == (
        {
            "type": "party",
            "target_ref": "party-001",
            "confidence": 0.88,
        },
    )


def test_context_publication_requires_explicit_site_and_proposed_draft_binding() -> None:
    publication = ContextIntelligencePublication(
        site_id=SCOPE.site_id,
        observation_id=SOURCE.observation_id,
        team_ref="team-sales",
        evidence_refs=("evidence-001",),
        summary_zh="客户希望确认样品交期。",
        original_language="zh-CN",
        confidence=0.93,
        review_status="AI Draft",
        fact_proposals=tuple(_output()["fact_proposals"]),
        association_suggestions=tuple(_output()["association_suggestions"]),
        model={"name": "deepseek-v4-flash", "version": "2026-08-08"},
        invocation_refs=("invocation-001",),
    )

    assert publication.site_id == SCOPE.site_id
    assert publication.team_ref == "team-sales"
    assert SCOPE.site_id not in repr(publication)
    assert "team-sales" not in repr(publication)
    assert replace(publication, team_ref=None).team_ref is None
    with pytest.raises(ValueError, match="site binding"):
        replace(publication, site_id="")
    with pytest.raises(ValueError, match="team binding"):
        replace(publication, team_ref="team\nsales")
    with pytest.raises(ValueError, match="proposed"):
        replace(
            publication,
            fact_proposals=(
                {
                    **publication.fact_proposals[0],
                    "status": "accepted",
                },
            ),
        )


def test_evidence_lineage_repr_redacts_identity_and_digest() -> None:
    rendered = repr(SOURCE.evidence[0])

    assert SOURCE.evidence[0].evidence_ref not in rendered
    assert SOURCE.evidence[0].content_object_ref not in rendered
    assert SOURCE.evidence[0].raw_sha256 not in rendered
    assert "<redacted>" in rendered


def test_unconfigured_provider_and_tokenization_failure_write_nothing() -> None:
    repository = FakeRepository()
    context = FakeContextPublisher(repository)
    unavailable = ObservationProjectionPublisher(
        repository=repository,
        raw_loader=lambda _scope, evidence: LoadedEvidenceText(
            evidence_ref=evidence.evidence_ref,
            text="secret",
        ),
        tokenizer=_tokenize,
        provider=None,
        context_publisher=context,
        clock=lambda: NOW,
        restricted_policy="local_tokenized",
    )

    with pytest.raises(ProjectionFailure, match="provider_unconfigured"):
        unavailable(SCOPE, SOURCE.observation_id, "context-normalized:event-001")
    assert repository.stored == [] and context.calls == []


@pytest.mark.parametrize("classification", ("Internal", "Confidential"))
def test_nonrestricted_input_is_also_tokenized_before_provider(
    classification: str,
) -> None:
    repository = FakeRepository(source=replace(SOURCE, classification=classification))
    publisher, _, provider, _ = _publisher(repository=repository)

    publisher(SCOPE, SOURCE.observation_id, "context-normalized:event-001")

    request = provider.requests[0]
    assert request.input_mode == "local_tokenized"
    assert request.tokenization_refs == ("tokenization-001",)
    assert request.mapping_digest == "b" * 64
    assert "alice@example.com" not in repr(request)


def test_nonrestricted_input_without_tokenizer_fails_closed() -> None:
    repository = FakeRepository(source=replace(SOURCE, classification="Internal"))
    publisher, repository, provider, context = _publisher(
        repository=repository,
        tokenizer=None,
    )

    with pytest.raises(ProjectionFailure, match="pii_tokenization_failed"):
        publisher(SCOPE, SOURCE.observation_id, "context-normalized:event-001")

    assert provider.requests == []
    assert context.calls == []
    assert repository.stored == []


def test_tokenization_mapping_digest_must_be_sha256() -> None:
    with pytest.raises(ValueError, match="mapping digest"):
        LocalTokenizationResult(
            text="tokenized",
            receipt_ref="tokenization-001",
            tokenizer_version="stable-tokenizer-v1",
            mapping_digest="not-a-digest",
        )


def test_binary_evidence_is_skipped_and_model_binding_uses_only_loaded_text() -> None:
    binary = EvidenceLineage(
        evidence_ref="evidence-binary",
        content_object_ref="obs:v1:alpha:sha256:" + ("c" * 64),
        media_type="application/pdf",
        raw_sha256="c" * 64,
    )
    source = replace(SOURCE, evidence=(*SOURCE.evidence, binary))
    output = _output()
    repository = FakeRepository(source=source)
    provider = FakeProvider(output=output)
    publisher = ObservationProjectionPublisher(
        repository=repository,
        raw_loader=lambda _scope, evidence: (
            None
            if evidence.media_type == "application/pdf"
            else LoadedEvidenceText(
                evidence_ref=evidence.evidence_ref,
                text="客户 alice@example.com 希望确认样品交期",
            )
        ),
        tokenizer=_tokenize,
        provider=provider,
        context_publisher=FakeContextPublisher(repository),
        clock=lambda: NOW,
        restricted_policy="local_tokenized",
    )

    publisher(SCOPE, SOURCE.observation_id, "context-normalized:event-001")

    assert provider.requests[0].evidence_refs == ("evidence-001",)
    detail = repository.stored[0]
    assert detail.summary.evidence_count == 2
    assert tuple(item["ref"] for item in detail.evidence) == (
        "evidence-001",
        "evidence-binary",
    )


def test_projection_requires_at_least_one_text_evidence() -> None:
    repository = FakeRepository()
    provider = FakeProvider()
    context = FakeContextPublisher(repository)
    publisher = ObservationProjectionPublisher(
        repository=repository,
        raw_loader=lambda _scope, _evidence: None,
        tokenizer=_tokenize,
        provider=provider,
        context_publisher=context,
        clock=lambda: NOW,
        restricted_policy="local_tokenized",
    )

    with pytest.raises(ProjectionFailure, match="raw_input_unavailable"):
        publisher(SCOPE, SOURCE.observation_id, "context-normalized:event-001")

    assert provider.requests == []
    assert context.calls == []
    assert repository.stored == []


def test_filesystem_loader_verifies_hash_and_skips_binary(tmp_path: Path) -> None:
    store = ContentAddressedEvidenceStore(tmp_path / "cas")
    text = b"hello <b>Alice</b>"
    stored = store.put(SCOPE, text, media_type="text/html")
    loader = ContentAddressedEvidenceTextLoader(store)
    lineage = EvidenceLineage(
        evidence_ref="evidence-html",
        content_object_ref=stored.object_ref,
        media_type="text/html",
        raw_sha256=stored.sha256,
    )

    loaded = loader(SCOPE, lineage)

    assert loaded is not None
    assert loaded.evidence_ref == "evidence-html"
    assert loaded.text == "hello Alice"
    with pytest.raises(ValueError, match="digest"):
        loader(SCOPE, replace(lineage, raw_sha256="0" * 64))
    assert (
        loader(
            SCOPE,
            replace(lineage, media_type="application/octet-stream"),
        )
        is None
    )
    assert "Alice" not in repr(loaded)


def test_filesystem_loader_rejects_mime_bomb_and_complex_email(tmp_path: Path) -> None:
    store = ContentAddressedEvidenceStore(tmp_path / "cas")
    oversized = b"Content-Type: text/plain; charset=utf-8\r\n\r\n" + b"A" * 300
    stored = store.put(SCOPE, oversized, media_type="message/rfc822")
    loader = ContentAddressedEvidenceTextLoader(
        store,
        max_object_bytes=256,
        max_text_characters=128,
    )
    lineage = EvidenceLineage(
        evidence_ref="evidence-email",
        content_object_ref=stored.object_ref,
        media_type="message/rfc822",
        raw_sha256=stored.sha256,
    )

    with pytest.raises(ValueError, match="bounded"):
        loader(SCOPE, lineage)

    complex_message = (
        b"Content-Type: multipart/mixed; boundary=x\r\n\r\n"
        b"--x\r\nContent-Type: message/rfc822\r\n\r\n"
        b"Content-Type: text/plain\r\n\r\nsecret\r\n--x--\r\n"
    )
    stored_complex = store.put(SCOPE, complex_message, media_type="message/rfc822")
    with pytest.raises(ValueError, match="complex"):
        ContentAddressedEvidenceTextLoader(store)(
            SCOPE,
            replace(
                lineage,
                content_object_ref=stored_complex.object_ref,
                raw_sha256=stored_complex.sha256,
            ),
        )


def test_restricted_policy_defaults_to_deny_before_provider_call() -> None:
    repository = FakeRepository()
    provider = FakeProvider()
    context = FakeContextPublisher(repository)
    publisher = ObservationProjectionPublisher(
        repository=repository,
        raw_loader=lambda _scope, _ref: "restricted content",
        tokenizer=_tokenize,
        provider=provider,
        context_publisher=context,
        clock=lambda: NOW,
    )

    with pytest.raises(ProjectionFailure, match="restricted_input_denied"):
        publisher(SCOPE, SOURCE.observation_id, "context-normalized:event-001")

    assert provider.requests == []
    assert context.calls == []
    assert repository.stored == []

    def fail_tokenization(*_args: object) -> LocalTokenizationResult:
        raise RuntimeError("PII mapping and secret")

    publisher, repository, provider, context = _publisher(tokenizer=fail_tokenization)
    with pytest.raises(ProjectionFailure, match="pii_tokenization_failed"):
        publisher(SCOPE, SOURCE.observation_id, "context-normalized:event-001")
    assert provider.requests == []
    assert repository.stored == [] and context.calls == []


@pytest.mark.parametrize(
    ("response", "code"),
    (
        (
            CommunicationIntelligenceResponse(
                output={**_output(), "raw_text": "forbidden"},
                model_name="deepseek-v4-flash",
                model_version="2026-08-08",
                invocation_refs=("invocation-001",),
            ),
            "invalid_model_output",
        ),
        (
            CommunicationIntelligenceResponse(
                output={**_output(), "site_id": "other.example"},
                model_name="deepseek-v4-flash",
                model_version="2026-08-08",
                invocation_refs=("invocation-001",),
            ),
            "model_binding_mismatch",
        ),
        (
            CommunicationIntelligenceResponse(
                output={
                    **_output(),
                    "evidence_refs": ["evidence-other"],
                },
                model_name="deepseek-v4-flash",
                model_version="2026-08-08",
                invocation_refs=("invocation-001",),
            ),
            "model_binding_mismatch",
        ),
        (
            CommunicationIntelligenceResponse(
                output=_output(),
                model_name="other-model",
                model_version="2026-08-08",
                invocation_refs=("invocation-001",),
            ),
            "model_mismatch",
        ),
    ),
)
def test_invalid_model_response_fails_closed(
    response: CommunicationIntelligenceResponse,
    code: str,
) -> None:
    repository = FakeRepository()
    provider = FakeProvider()
    provider.response = response
    context = FakeContextPublisher(repository)
    publisher, _, _, _ = _publisher(
        repository=repository,
        provider=provider,
        context=context,
    )

    with pytest.raises(ProjectionFailure, match=code):
        publisher(SCOPE, SOURCE.observation_id, "context-normalized:event-001")

    assert repository.stored == []
    assert context.calls == []


def test_context_failure_prevents_projection_and_preserves_stable_retry_key() -> None:
    repository = FakeRepository()
    provider = FakeProvider()
    context = FakeContextPublisher(repository, fail=True)
    publisher, _, _, _ = _publisher(
        repository=repository,
        provider=provider,
        context=context,
    )

    with pytest.raises(ProjectionFailure, match="context_publication_failed"):
        publisher(SCOPE, SOURCE.observation_id, "context-normalized:event-001")

    assert repository.stored == []
    assert context.calls[0][0] == "context-normalized:event-001"
    assert provider.requests[0].idempotency_key == "context-normalized:event-001"


def test_context_failure_keeps_durable_outbox_unpublished() -> None:
    repository = FakeRepository()
    context = FakeContextPublisher(repository, fail=True)
    projection, _, _, _ = _publisher(
        repository=repository,
        context=context,
    )

    class OutboxStorage:
        marked: list[tuple[bool, str | None]] = []

        def claim_context_outbox(
            self,
            scope: TenantScope,
            *,
            worker_id: str,
            now: datetime,
            lease_seconds: int,
        ) -> ContextOutboxMetadata:
            assert scope == SCOPE and worker_id == "projection-worker-1"
            return ContextOutboxMetadata(
                site_id=scope.site_id,
                outbox_id="outbox-001",
                observation_event_id=SOURCE.observation_id,
                idempotency_key="context-normalized:event-001",
                payload_digest="a" * 64,
                status="leased",
                attempt_count=1,
                max_attempts=3,
                next_retry_at=now,
                lease_owner=worker_id,
                lease_expires_at=now + timedelta(minutes=1),
                last_error_code=None,
                created_at=now,
                updated_at=now,
            )

        def mark_context_outbox(
            self,
            scope: TenantScope,
            *,
            outbox_id: str,
            worker_id: str,
            now: datetime,
            published: bool,
            error_code: str | None = None,
            next_retry_at: datetime | None = None,
        ) -> ContextOutboxMetadata:
            self.marked.append((published, error_code))
            claimed = self.claim_context_outbox(
                scope,
                worker_id=worker_id,
                now=now,
                lease_seconds=60,
            )
            return replace(
                claimed,
                status="retry_wait",
                lease_owner=None,
                lease_expires_at=None,
                last_error_code=error_code,
                next_retry_at=next_retry_at or now,
            )

    storage = OutboxStorage()
    worker = ContextOutboxPublisherWorker(
        storage=storage,
        publisher=projection,
        worker_id="projection-worker-1",
        clock=lambda: NOW,
    )

    result = worker.run_once(SCOPE)

    assert result is not None and result.status == "retry_wait"
    assert storage.marked == [(False, "context_publication_failed")]
    assert repository.stored == []


def test_nested_fact_and_association_evidence_must_be_bound_to_observation() -> None:
    output = _output()
    output["fact_proposals"] = [
        {
            **output["fact_proposals"][0],
            "evidence_refs": ["evidence-other"],
        }
    ]
    provider = FakeProvider(output=output)
    publisher, repository, _, context = _publisher(provider=provider)

    with pytest.raises(ProjectionFailure, match="model_binding_mismatch"):
        publisher(SCOPE, SOURCE.observation_id, "context-normalized:event-001")

    assert repository.stored == [] and context.calls == []


def test_nullable_database_team_scope_is_never_synthesized_by_model_output() -> None:
    repository = FakeRepository(source=replace(SOURCE, team_ref=None))
    publisher, _, _, context = _publisher(repository=repository)

    publisher(SCOPE, SOURCE.observation_id, "context-normalized:event-001")

    assert context.calls[0][1].team_ref is None
    assert repository.stored[0].summary.team_ref is None


def test_projection_store_rejects_non_ai_draft_or_non_proposed_state() -> None:
    repository = PostgresCommunicationRepository(connection=object())
    invalid = CommunicationDetail(
        summary=CommunicationSummary(
            observation_id=SOURCE.observation_id,
            channel=SOURCE.channel,
            occurred_at=NOW,
            summary_zh="不得进入正式状态",
            original_language="zh-CN",
            classification="Restricted",
            review_status="Approved",
            team_ref=None,
            party_ref=None,
            evidence_count=1,
        ),
        evidence=(),
        fact_proposals=(
            {
                "status": "accepted",
                "confidence": 0.9,
                "type": "text",
                "value_display": "invalid",
            },
        ),
        association_suggestions=(),
        model={"name": "deepseek-v4-flash", "version": "2026-08-08"},
        original_text=None,
    )

    with pytest.raises(ValueError, match="AI Draft"):
        repository.store_projection(SCOPE, invalid, projected_at=NOW)


def test_runtime_composition_routes_outbox_through_model_projection_callback() -> None:
    provider = FakeProvider()
    context_calls: list[object] = []

    class ContextPublisher:
        def publish(
            self,
            _scope: TenantScope,
            publication: object,
            *,
            idempotency_key: str,
        ) -> None:
            context_calls.append((idempotency_key, publication))

    runtime = compose_postgres_local_pilot_runtime(
        connection=object(),
        storage=object(),
        api_config=LocalPilotAPIConfig(
            bind_host="127.0.0.1",
            network_mode="loopback",
            bearer_token="synthetic-local-token",
            auth_ref="observer-token-v1",
        ),
        cursor_secret=b"x" * 32,
        publisher=lambda _scope, _event_id, _idempotency_key: None,
        clock=lambda: NOW,
        outbox_worker_id="projection-worker-1",
        enabled=True,
        kill_switch=False,
        model_provider=provider,
        model_raw_loader=lambda _scope, _ref: "local raw input",
        model_tokenizer=_tokenize,
        intelligence_publisher=ContextPublisher(),
        restricted_model_policy="local_tokenized",
    )

    assert runtime.projection_repository is not None
    assert runtime.projection_publisher is not None
    assert runtime.outbox._publisher is runtime.projection_publisher
