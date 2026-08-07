from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import get_type_hints

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from observer.connectors import (
    CONNECTOR_CHANNELS,
    canonical_observation_event_v11,
)
from observer.models import (
    ConnectorBatch,
    ConnectorItem,
    ConnectorKey,
    EvidenceArtifact,
    NormalizedObservationInput,
    Participant,
    RawDelivery,
    TenantScope,
    TranscriptSegments,
)
from observer.protocols import (
    DeliveryAuthenticator,
    ObservationNormalizer,
    PullConnector,
    SpeechProvider,
)

NOW = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)
SCOPE = TenantScope(site_id="alpha.example", processing_purpose="observation_processing")
SCHEMA_PATH = (
    Path(__file__).parents[2]
    / "contracts"
    / "local_pilot"
    / "canonical-observation-event-v1.1.schema.json"
)


def _normalized(channel: str) -> NormalizedObservationInput:
    return NormalizedObservationInput(
        channel=channel,
        participants=(Participant(role="external", identity_ref="party:synthetic-001"),),
        evidence=(
            EvidenceArtifact(
                content=b"exact evidence",
                media_type="text/plain",
                locator="message:0",
                role="primary",
            ),
        ),
        consent_basis="pilot_deferred_review",
        data_classification="Restricted",
        retention_class="R1-operational",
        original_language="zh-CN",
        correlation_id="corr-001",
    )


def test_raw_delivery_preserves_exact_bytes_and_requires_aware_time() -> None:
    exact = b"\x00\xff\r\nbody"
    delivery = RawDelivery(
        delivery_id="delivery-001",
        exact_bytes=exact,
        media_type="application/octet-stream",
        received_at=NOW,
    )

    assert delivery.exact_bytes is exact
    assert delivery.exact_bytes == b"\x00\xff\r\nbody"
    with pytest.raises(TypeError, match="bytes"):
        RawDelivery("delivery-002", bytearray(exact), "application/octet-stream", NOW)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timezone-aware"):
        RawDelivery("delivery-003", exact, "application/octet-stream", NOW.replace(tzinfo=None))


def test_connector_domain_types_enforce_identifiers_and_evidence_choice() -> None:
    assert ConnectorKey("wecom", "sales-team").instance_id == "sales-team"
    with pytest.raises(ValueError, match="connector"):
        ConnectorKey("unknown", "sales-team")
    with pytest.raises(ValueError, match="instance_id"):
        ConnectorKey("wecom", "")
    with pytest.raises(ValueError, match="instance_id"):
        ConnectorKey("wecom", " sales-team ")

    referenced = EvidenceArtifact(
        reference="evidence-object-001",
        media_type="audio/ogg",
        locator="seconds:0-5",
        role="primary",
    )
    assert referenced.reference == "evidence-object-001"
    with pytest.raises(ValueError, match="exactly one"):
        EvidenceArtifact(
            content=b"x",
            reference="object-001",
            media_type="text/plain",
            locator="message:0",
            role="primary",
        )
    with pytest.raises(TypeError, match="bytes"):
        EvidenceArtifact(
            content="plaintext",  # type: ignore[arg-type]
            media_type="text/plain",
            locator="message:0",
            role="primary",
        )


def test_connector_items_and_batches_enforce_timestamps_cursors_and_uniqueness() -> None:
    item = ConnectorItem(
        provider_event_id="provider-001",
        occurred_at=NOW,
        source_cursor="cursor-001",
        payload={"kind": "message"},
    )
    batch = ConnectorBatch(
        expected_cursor="cursor-000",
        next_cursor="cursor-001",
        items=(item,),
    )
    assert batch.items == (item,)

    with pytest.raises(ValueError, match="timezone-aware"):
        ConnectorItem("provider-002", NOW.replace(tzinfo=None), "cursor-002", {})
    with pytest.raises(ValueError, match="duplicate"):
        ConnectorBatch("cursor-000", "cursor-002", (item, item))
    with pytest.raises(ValueError, match="next_cursor"):
        ConnectorBatch("cursor-000", None, (item,))
    with pytest.raises(TypeError, match="tuple"):
        ConnectorBatch("cursor-000", "cursor-001", [item])  # type: ignore[arg-type]


def test_normalized_input_is_frozen_reuses_participants_and_validates_policy_fields() -> None:
    normalized = _normalized("chat")
    assert isinstance(normalized.participants[0], Participant)
    assert normalized.evidence[0].content == b"exact evidence"

    with pytest.raises(ValueError, match="consent_basis"):
        NormalizedObservationInput(
            channel="chat",
            participants=normalized.participants,
            evidence=normalized.evidence,
            consent_basis="invented",
            data_classification="Restricted",
            retention_class="R1",
            original_language="zh-CN",
            correlation_id="corr-001",
        )
    with pytest.raises(ValueError, match="participants"):
        NormalizedObservationInput(
            channel="chat",
            participants=(),
            evidence=normalized.evidence,
            consent_basis="consent",
            data_classification="Restricted",
            retention_class="R1",
            original_language="zh-CN",
            correlation_id="corr-001",
        )
    with pytest.raises(TypeError, match="tuple"):
        NormalizedObservationInput(
            channel="chat",
            participants=list(normalized.participants),  # type: ignore[arg-type]
            evidence=normalized.evidence,
            consent_basis="consent",
            data_classification="Restricted",
            retention_class="R1",
            original_language="zh-CN",
            correlation_id="corr-001",
        )


@pytest.mark.parametrize(("connector", "channel"), tuple(CONNECTOR_CHANNELS.items()))
def test_all_connector_channel_mappings_serialize_to_wire_valid_v11(
    connector: str,
    channel: str,
) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    item = ConnectorItem(
        provider_event_id=f"{connector}-event-001",
        occurred_at=NOW,
        source_cursor="cursor-001",
        payload={"provider_only": "not allowed on wire"},
    )

    wire = canonical_observation_event_v11(
        scope=SCOPE,
        connector_key=ConnectorKey(connector, f"{connector}-primary"),
        item=item,
        normalized=_normalized(channel),
        event_id="01K20B8BV5C6P4YFAT8YQ3D4S5",
        evidence_ids=("evidence-SYNTH-001",),
        ingested_at=NOW,
    )

    validator.validate(wire)
    assert wire["connector_instance_id"] == f"{connector}-primary"
    assert "payload" not in wire
    assert "evidence" not in wire
    assert "processing_purpose" not in wire


def test_serializer_rejects_mismatched_channel_and_evidence_ids() -> None:
    item = ConnectorItem("provider-001", NOW, "cursor-001", {})

    with pytest.raises(ValueError, match="channel"):
        canonical_observation_event_v11(
            scope=SCOPE,
            connector_key=ConnectorKey("email", "primary"),
            item=item,
            normalized=_normalized("chat"),
            event_id="01K20B8BV5C6P4YFAT8YQ3D4S5",
            evidence_ids=("evidence-001",),
            ingested_at=NOW,
        )
    with pytest.raises(ValueError, match="evidence"):
        canonical_observation_event_v11(
            scope=SCOPE,
            connector_key=ConnectorKey("email", "primary"),
            item=item,
            normalized=_normalized("email"),
            event_id="01K20B8BV5C6P4YFAT8YQ3D4S5",
            evidence_ids=(),
            ingested_at=NOW,
        )


def test_provider_neutral_protocols_have_the_frozen_boundary_and_exclude_model_provider() -> None:
    fetch = get_type_hints(PullConnector.fetch)
    verify = get_type_hints(DeliveryAuthenticator.verify)
    normalize = get_type_hints(ObservationNormalizer.normalize)
    transcribe = get_type_hints(SpeechProvider.transcribe)

    assert fetch == {
        "checkpoint": str | None,
        "limit": int,
        "return": ConnectorBatch,
    }
    assert verify == {"exact_request": bytes, "return": RawDelivery}
    assert normalize == {"item": ConnectorItem, "return": NormalizedObservationInput}
    assert transcribe == {"evidence_ref": str, "return": TranscriptSegments}

    import observer.protocols as protocols

    assert not hasattr(protocols, "ModelProvider")
