from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum


class PipelineStatus(StrEnum):
    READY = "ready"
    QUARANTINED = "quarantined"
    RETRY = "retry"
    DEAD_LETTER = "dead_letter"


@dataclass(frozen=True, slots=True)
class StageIdempotencyKey:
    stage: str
    key: str


def stage_idempotency_key(
    *,
    site_id: str,
    request_id: str,
    immutable_checksum: str,
    stage: str,
) -> StageIdempotencyKey:
    document = "\x1f".join(
        ("media-runtime-stage-v1", site_id, request_id, immutable_checksum, stage)
    )
    digest = hashlib.sha256(document.encode("utf-8")).hexdigest()
    return StageIdempotencyKey(stage=stage, key=f"{stage}:{digest}")
