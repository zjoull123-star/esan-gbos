from __future__ import annotations

import re
from datetime import datetime
from types import MappingProxyType

from ..models import (
    CONNECTOR_NAMES,
    ConnectorItem,
    ConnectorKey,
    NormalizedObservationInput,
    TenantScope,
    _require_aware,
)

CONNECTOR_CHANNELS = MappingProxyType(
    {
        "email": "email",
        "wecom": "chat",
        "whatsapp": "chat",
        "phone": "call",
        "meeting": "meeting",
        "file": "document",
        "manual_import": "manual_import",
        "wechat_workphone": "chat",
    }
)
_ULID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")

if frozenset(CONNECTOR_CHANNELS) != CONNECTOR_NAMES:
    raise RuntimeError("connector/channel map must cover the frozen v1 connector enum")


def channel_for_connector(connector: str) -> str:
    try:
        return CONNECTOR_CHANNELS[connector]
    except KeyError as exc:
        raise ValueError("invalid connector") from exc


def _wire_timestamp(value: datetime, field_name: str) -> str:
    _require_aware(value, field_name)
    return value.isoformat().replace("+00:00", "Z")


def canonical_observation_event_v11(
    *,
    scope: TenantScope,
    connector_key: ConnectorKey,
    item: ConnectorItem,
    normalized: NormalizedObservationInput,
    event_id: str,
    evidence_ids: tuple[str, ...],
    ingested_at: datetime,
) -> dict[str, object]:
    """Serialize normalized connector output into the closed v1.1 wire shape."""

    expected_channel = channel_for_connector(connector_key.connector)
    if normalized.channel != expected_channel:
        raise ValueError("normalized channel does not match connector")
    if not _ULID.fullmatch(event_id):
        raise ValueError("invalid event_id")
    if (
        not evidence_ids
        or len(evidence_ids) != len(normalized.evidence)
        or len(evidence_ids) != len(set(evidence_ids))
        or any(not evidence_id or len(evidence_id) > 256 for evidence_id in evidence_ids)
    ):
        raise ValueError("evidence_ids must uniquely identify every normalized artifact")

    participants: list[dict[str, str]] = []
    for participant in normalized.participants:
        wire_participant = {
            "role": participant.role,
            "identity_ref": participant.identity_ref,
        }
        if participant.display_name is not None:
            wire_participant["display_name"] = participant.display_name
        participants.append(wire_participant)

    return {
        "schema_version": "1.1",
        "event_id": event_id,
        "site_id": scope.site_id,
        "connector": connector_key.connector,
        "connector_instance_id": connector_key.instance_id,
        "channel": normalized.channel,
        "provider_event_id": item.provider_event_id,
        "occurred_at": _wire_timestamp(item.occurred_at, "occurred_at"),
        "ingested_at": _wire_timestamp(ingested_at, "ingested_at"),
        "original_language": normalized.original_language,
        "participants": participants,
        "evidence_refs": list(evidence_ids),
        "consent_basis": normalized.consent_basis,
        "data_classification": normalized.data_classification,
        "retention_class": normalized.retention_class,
        "correlation_id": normalized.correlation_id,
    }
