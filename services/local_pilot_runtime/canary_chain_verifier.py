"""Machine-verifiable, content-free local canary chain attestation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from .projection_config import ProjectionConfigError, load_projection_config
from .runtime_support import RuntimeSupportError, connect_postgres, load_secret_file

_MODEL = "deepseek-v4-flash"
_PURPOSE = "observation_processing"
_DOCTYPE = "GBOS Informal Observation"
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_HEX = re.compile(r"^[0-9a-f]{64}$")
_SITE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,139}$")
_MAX_JSON_BYTES = 1024 * 1024
_DEFERRED_STABILITY = {
    "continuous_runtime_required": False,
    "seventy_two_hour_run": "deferred_by_user",
}
_ATTESTATION_FIELDS = frozenset(
    {
        "schema_version",
        "attestation_type",
        "generated_at",
        "run_id",
        "site_id",
        "processing_purpose",
        "source_commit",
        "manifest_sha256",
        "activation_time",
        "observation_window",
        "chain",
        "payload_sha256",
    }
)
_WINDOW_FIELDS = frozenset({"started_at", "ended_at"})
_CHAIN_FIELDS = frozenset(
    {
        "agent_invocation_count",
        "invocation_ref_sha256",
        "requested_model",
        "response_reported_observed_model",
        "response_id_present",
        "network_call_count",
        "tool_call_count",
        "external_send_count",
        "fatal_or_mismatch_invocation_count",
        "context_chain_count",
        "intelligence_ref_sha256",
        "email_delivery_count",
        "delivery_ref_sha256",
        "raw_body_sha256",
        "observation_ref_sha256",
        "participant_ref_sha256",
        "identity_resolution_status",
        "identity_target_type",
        "observer_authority_active",
        "invocation_ordinal",
        "review_status",
        "model_name",
        "model_version",
        "context_state_bound",
        "draft_status",
        "receipt_doctype",
        "receipt_name_present",
        "receipt_revision",
        "receipt_request_present",
        "receipt_request_bound",
        "receipt_digest",
        "observer_fatal_latch_open",
    }
)


class CanaryChainVerificationError(RuntimeError):
    """A safe, low-cardinality canary verification rejection."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code) is None:
            raise ValueError("invalid canary verification error code")
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"CanaryChainVerificationError(code={self.code!r})"


@dataclass(frozen=True, slots=True, repr=False)
class AgentInvocationProjection:
    site_id: str
    invocation_ref_sha256: str
    requested_model: str
    response_reported_observed_model: str | None
    response_id_present: bool
    network_call_count: int
    tool_call_count: int
    external_send_count: int
    status: str
    error_code: str | None
    started_at: datetime
    completed_at: datetime | None

    def __repr__(self) -> str:
        return "AgentInvocationProjection(fields=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class ContextChainProjection:
    site_id: str
    processing_purpose: str
    invocation_ref_sha256: str
    intelligence_ref_sha256: str
    observation_ref_sha256: str
    invocation_ordinal: int
    review_status: str
    model_name: str
    model_version: str
    draft_state_bound: bool
    draft_status: str
    receipt_doctype: str | None
    receipt_name_present: bool
    receipt_revision: int | None
    receipt_request_present: bool
    receipt_request_bound: bool
    receipt_digest: str | None
    created_at: datetime
    updated_at: datetime

    def __repr__(self) -> str:
        return "ContextChainProjection(fields=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class ObserverChainProjection:
    site_id: str
    processing_purpose: str
    delivery_ref_sha256: str
    raw_body_sha256: str
    delivery_status: str
    observation_ref_sha256: str
    connector: str
    channel: str
    participant_ref_sha256: str
    identity_work_status: str
    resolution_status: str
    target_type: str
    authority_active: bool
    received_at: datetime
    ingested_at: datetime

    def __repr__(self) -> str:
        return "ObserverChainProjection(fields=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class ObserverLatchProjection:
    site_id: str
    processing_purpose: str
    is_open: bool

    def __repr__(self) -> str:
        return f"ObserverLatchProjection(is_open={self.is_open!r}, scope=<redacted>)"


class AgentProjectionRepository(Protocol):
    def bounded_window(
        self,
        *,
        site_id: str,
        window_start: datetime,
        window_end: datetime,
        limit: int,
    ) -> tuple[AgentInvocationProjection, ...]: ...


class ContextProjectionRepository(Protocol):
    def bounded_window(
        self,
        *,
        site_id: str,
        processing_purpose: str,
        window_start: datetime,
        window_end: datetime,
        limit: int,
    ) -> tuple[ContextChainProjection, ...]: ...


class ObserverProjectionRepository(Protocol):
    def bounded_window(
        self,
        *,
        site_id: str,
        processing_purpose: str,
        window_start: datetime,
        window_end: datetime,
        limit: int,
    ) -> tuple[ObserverChainProjection, ...]: ...

    def latch(
        self,
        *,
        site_id: str,
        processing_purpose: str,
    ) -> ObserverLatchProjection: ...


@dataclass(frozen=True, slots=True, repr=False)
class CanaryProjectionRepositories:
    agent: AgentProjectionRepository
    context: ContextProjectionRepository
    observer: ObserverProjectionRepository
    close: Callable[[], None]

    def __repr__(self) -> str:
        return "CanaryProjectionRepositories(repositories=<redacted>)"


class PostgresAgentCanaryRepository:
    """Bounded content-free Agent projection reader."""

    __slots__ = ("_connection",)

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def __repr__(self) -> str:
        return "PostgresAgentCanaryRepository(connection=<redacted>)"

    def bounded_window(
        self,
        *,
        site_id: str,
        window_start: datetime,
        window_end: datetime,
        limit: int,
    ) -> tuple[AgentInvocationProjection, ...]:
        _query_limit(limit)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            cursor.execute("SELECT set_config('app.site_id', %s, true)", (site_id,))
            cursor.execute(
                """
                SELECT
                    site_id,
                    invocation_id,
                    requested_model,
                    observed_model,
                    response_id IS NOT NULL AND btrim(response_id) <> '',
                    network_call_count,
                    tool_call_count,
                    external_send_count,
                    status,
                    error_code,
                    started_at,
                    completed_at
                FROM agent_runtime.model_invocations
                WHERE site_id = %s
                  AND started_at >= %s
                  AND started_at <= %s
                ORDER BY started_at, invocation_id
                LIMIT %s
                """,
                (site_id, window_start, window_end, limit),
            )
            rows = cursor.fetchall()
        return tuple(
            AgentInvocationProjection(
                site_id=str(row[0]),
                invocation_ref_sha256=_technical_ref_digest(row[1]),
                requested_model=str(row[2]),
                response_reported_observed_model=None if row[3] is None else str(row[3]),
                response_id_present=row[4] is True,
                network_call_count=int(row[5]),
                tool_call_count=int(row[6]),
                external_send_count=int(row[7]),
                status=str(row[8]),
                error_code=None if row[9] is None else str(row[9]),
                started_at=row[10],
                completed_at=row[11],
            )
            for row in rows
        )


class PostgresContextCanaryRepository:
    """Bounded content-free Context and Frappe-receipt projection reader."""

    __slots__ = ("_connection",)

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def __repr__(self) -> str:
        return "PostgresContextCanaryRepository(connection=<redacted>)"

    def bounded_window(
        self,
        *,
        site_id: str,
        processing_purpose: str,
        window_start: datetime,
        window_end: datetime,
        limit: int,
    ) -> tuple[ContextChainProjection, ...]:
        _query_limit(limit)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            cursor.execute("SELECT set_config('app.site_id', %s, true)", (site_id,))
            cursor.execute(
                "SELECT set_config('app.processing_purpose', %s, true)",
                (processing_purpose,),
            )
            cursor.execute(
                """
                SELECT
                    intelligence.site_id,
                    intelligence.processing_purpose,
                    invocation.invocation_ref,
                    intelligence.intelligence_id,
                    intelligence.observation_id,
                    invocation.ordinal,
                    intelligence.review_status,
                    intelligence.model_name,
                    intelligence.model_version,
                    draft.processing_purpose = intelligence.processing_purpose
                        AND draft.review_status = intelligence.review_status
                        AND draft.model_name = intelligence.model_name
                        AND draft.model_version = intelligence.model_version,
                    draft.status,
                    draft.receipt_doctype,
                    draft.receipt_name IS NOT NULL AND btrim(draft.receipt_name) <> '',
                    draft.receipt_revision,
                    draft.receipt_request_id IS NOT NULL
                        AND btrim(draft.receipt_request_id) <> '',
                    draft.receipt_request_id = draft.draft_id,
                    draft.receipt_digest,
                    intelligence.created_at,
                    draft.updated_at
                FROM context.communication_intelligence AS intelligence
                JOIN context.communication_intelligence_invocations AS invocation
                  ON invocation.site_id = intelligence.site_id
                 AND invocation.intelligence_id = intelligence.intelligence_id
                JOIN context.communication_draft_outbox AS draft
                  ON draft.site_id = intelligence.site_id
                 AND draft.intelligence_id = intelligence.intelligence_id
                WHERE intelligence.site_id = %s
                  AND intelligence.processing_purpose = %s
                  AND intelligence.created_at >= %s
                  AND intelligence.created_at <= %s
                ORDER BY intelligence.created_at, intelligence.intelligence_id,
                         invocation.ordinal
                LIMIT %s
                """,
                (site_id, processing_purpose, window_start, window_end, limit),
            )
            rows = cursor.fetchall()
        return tuple(
            ContextChainProjection(
                site_id=str(row[0]),
                processing_purpose=str(row[1]),
                invocation_ref_sha256=_technical_ref_digest(row[2]),
                intelligence_ref_sha256=_technical_ref_digest(row[3]),
                observation_ref_sha256=_technical_ref_digest(row[4]),
                invocation_ordinal=int(row[5]),
                review_status=str(row[6]),
                model_name=str(row[7]),
                model_version=str(row[8]),
                draft_state_bound=row[9] is True,
                draft_status=str(row[10]),
                receipt_doctype=None if row[11] is None else str(row[11]),
                receipt_name_present=row[12] is True,
                receipt_revision=None if row[13] is None else int(row[13]),
                receipt_request_present=row[14] is True,
                receipt_request_bound=row[15] is True,
                receipt_digest=None if row[16] is None else str(row[16]),
                created_at=row[17],
                updated_at=row[18],
            )
            for row in rows
        )


class PostgresObserverCanaryRepository:
    """Exact-scope content-free Email, identity, and fatal-latch reader."""

    __slots__ = ("_connection",)

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def __repr__(self) -> str:
        return "PostgresObserverCanaryRepository(connection=<redacted>)"

    def bounded_window(
        self,
        *,
        site_id: str,
        processing_purpose: str,
        window_start: datetime,
        window_end: datetime,
        limit: int,
    ) -> tuple[ObserverChainProjection, ...]:
        _query_limit(limit)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            cursor.execute("SELECT set_config('app.site_id', %s, true)", (site_id,))
            cursor.execute(
                "SELECT set_config('app.processing_purpose', %s, true)",
                (processing_purpose,),
            )
            cursor.execute(
                """
                SELECT
                    event.site_id,
                    event.processing_purpose,
                    delivery.delivery_id,
                    delivery.exact_body_sha256,
                    delivery.processing_status,
                    event.event_id,
                    event.connector,
                    event.channel,
                    participant.identity_ref,
                    work.last_resolution_status,
                    resolution.status,
                    resolution.target_type,
                    NOT EXISTS (
                        SELECT 1
                        FROM observer.identity_authority_denials AS denial
                        WHERE denial.site_id = resolution.site_id
                          AND denial.identity_provider = resolution.identity_provider
                          AND denial.identity_ref = resolution.external_subject_ref
                          AND denial.team_ref = resolution.team_ref
                          AND denial.mapping_ref = resolution.mapping_ref
                          AND denial.deny_through_revision >= resolution.mapping_revision
                    ),
                    delivery.received_at,
                    event.ingested_at
                FROM observer.observation_events AS event
                JOIN observer.inbound_deliveries AS delivery
                  ON delivery.site_id = event.site_id
                 AND delivery.connector = event.connector
                 AND delivery.connector_instance_id = event.connector_instance_id
                 AND delivery.delivery_id = event.delivery_id
                JOIN observer.participants AS participant
                  ON participant.site_id = event.site_id
                 AND participant.event_id = event.event_id
                JOIN observer.identity_resolution_work AS work
                  ON work.site_id = event.site_id
                 AND work.identity_ref = participant.identity_ref
                 AND work.team_ref = event.team_ref
                JOIN LATERAL (
                    SELECT candidate.*
                    FROM observer.participant_identity_resolutions AS candidate
                    WHERE candidate.site_id = event.site_id
                      AND candidate.external_subject_ref = participant.identity_ref
                      AND candidate.team_ref = event.team_ref
                    ORDER BY candidate.mapping_revision DESC
                    LIMIT 1
                ) AS resolution ON TRUE
                WHERE event.site_id = %s
                  AND event.processing_purpose = %s
                  AND event.connector = 'email'
                  AND event.channel = 'email'
                  AND event.ingested_at >= %s
                  AND event.ingested_at <= %s
                ORDER BY event.ingested_at, event.event_id, participant.participant_id
                LIMIT %s
                """,
                (site_id, processing_purpose, window_start, window_end, limit),
            )
            rows = cursor.fetchall()
        return tuple(
            ObserverChainProjection(
                site_id=str(row[0]),
                processing_purpose=str(row[1]),
                delivery_ref_sha256=_technical_ref_digest(row[2]),
                raw_body_sha256=str(row[3]),
                delivery_status=str(row[4]),
                observation_ref_sha256=_technical_ref_digest(row[5]),
                connector=str(row[6]),
                channel=str(row[7]),
                participant_ref_sha256=_technical_ref_digest(row[8]),
                identity_work_status=str(row[9]),
                resolution_status=str(row[10]),
                target_type=str(row[11]),
                authority_active=row[12] is True,
                received_at=row[13],
                ingested_at=row[14],
            )
            for row in rows
        )

    def latch(
        self,
        *,
        site_id: str,
        processing_purpose: str,
    ) -> ObserverLatchProjection:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            cursor.execute("SELECT set_config('app.site_id', %s, true)", (site_id,))
            cursor.execute(
                "SELECT set_config('app.processing_purpose', %s, true)",
                (processing_purpose,),
            )
            cursor.execute(
                """
                SELECT NOT EXISTS (
                    SELECT 1
                    FROM observer.model_fatal_latches
                    WHERE site_id = %s AND processing_purpose = %s
                )
                """,
                (site_id, processing_purpose),
            )
            row = cursor.fetchone()
        if row is None or len(row) != 1 or not isinstance(row[0], bool):
            raise CanaryChainVerificationError("projection_read_failed")
        return ObserverLatchProjection(
            site_id=site_id,
            processing_purpose=processing_purpose,
            is_open=row[0],
        )


def create_projection_repositories(
    *,
    projection_config_path: Path,
    expected_site_id: str,
    connector: Callable[..., object] | None = None,
) -> CanaryProjectionRepositories:
    """Preflight all three least-privilege roles before opening any connection."""

    try:
        projection = load_projection_config(
            projection_config_path,
            expected_site_id=expected_site_id,
        )
        if not projection.controlled_egress:
            raise CanaryChainVerificationError("projection_configuration_invalid")
        for settings in projection.connections.values():
            load_secret_file(settings.password_file)
    except CanaryChainVerificationError:
        raise
    except (OSError, ProjectionConfigError, RuntimeSupportError) as exc:
        raise CanaryChainVerificationError("projection_configuration_invalid") from exc

    connections: list[Any] = []
    try:
        for role in ("observer", "context", "agent"):
            connections.append(connect_postgres(projection.connections[role], connector=connector))
    except Exception as exc:
        for connection in reversed(connections):
            with suppress(Exception):
                connection.close()
        raise CanaryChainVerificationError("projection_connection_failed") from exc
    observer_connection, context_connection, agent_connection = connections

    def close() -> None:
        for connection in reversed(connections):
            with suppress(Exception):
                connection.close()

    return CanaryProjectionRepositories(
        agent=PostgresAgentCanaryRepository(agent_connection),
        context=PostgresContextCanaryRepository(context_connection),
        observer=PostgresObserverCanaryRepository(observer_connection),
        close=close,
    )


def verify_canary_chain(
    *,
    canary_dir: Path,
    output_path: Path,
    window_start: datetime,
    window_end: datetime,
    expected_source_commit: str,
    repositories: CanaryProjectionRepositories | None = None,
    projection_config_path: Path | None = None,
    connector: Callable[..., object] | None = None,
    clock: Callable[[], datetime] | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Validate the exact projection chain and atomically create its attestation."""

    active_clock = clock or (lambda: datetime.now(UTC))
    root = (repo_root or Path(__file__).resolve().parents[2]).resolve(strict=True)
    directory = _external_private_directory(canary_dir, root, "canary_directory_invalid")
    output = _new_external_output(output_path, root, directory)
    manifest_path = directory / "pilot-manifest.json"
    control_path = directory / "canary-run.json"
    manifest, manifest_bytes = _read_private_json_document(manifest_path, "manifest_invalid")
    control = _read_private_json(control_path, "control_invalid")
    bindings = _validate_bindings(
        actual_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        manifest=manifest,
        control=control,
        expected_source_commit=expected_source_commit,
    )
    activation = _timestamp(bindings["activation_time"], "control_invalid")
    start = _aware(window_start, "observation_window_invalid")
    end = _aware(window_end, "observation_window_invalid")
    generated_at = _aware(active_clock(), "clock_invalid")
    if not activation <= start < end <= generated_at:
        raise CanaryChainVerificationError("observation_window_invalid")

    if repositories is None:
        if projection_config_path is None:
            raise CanaryChainVerificationError("projection_configuration_invalid")
        repositories = create_projection_repositories(
            projection_config_path=projection_config_path,
            expected_site_id=bindings["site_id"],
            connector=connector,
        )
    elif projection_config_path is not None or connector is not None:
        raise CanaryChainVerificationError("projection_configuration_invalid")

    try:
        agent_rows = repositories.agent.bounded_window(
            site_id=bindings["site_id"],
            window_start=start,
            window_end=end,
            limit=2,
        )
        if len(agent_rows) != 1:
            raise CanaryChainVerificationError("agent_invocation_count_invalid")
        agent = agent_rows[0]
        _validate_agent(agent, site_id=bindings["site_id"], start=start, end=end)

        context_rows = repositories.context.bounded_window(
            site_id=bindings["site_id"],
            processing_purpose=_PURPOSE,
            window_start=start,
            window_end=end,
            limit=2,
        )
        if len(context_rows) != 1:
            raise CanaryChainVerificationError("context_chain_count_invalid")
        context = context_rows[0]
        _validate_context(context, site_id=bindings["site_id"], start=start, end=end)
        if context.invocation_ref_sha256 != agent.invocation_ref_sha256:
            raise CanaryChainVerificationError("cross_projection_binding_invalid")

        observer_rows = repositories.observer.bounded_window(
            site_id=bindings["site_id"],
            processing_purpose=_PURPOSE,
            window_start=start,
            window_end=end,
            limit=2,
        )
        if len(observer_rows) != 1:
            raise CanaryChainVerificationError("observer_chain_count_invalid")
        observer = observer_rows[0]
        _validate_observer(observer, site_id=bindings["site_id"], start=start, end=end)
        if context.observation_ref_sha256 != observer.observation_ref_sha256:
            raise CanaryChainVerificationError("cross_projection_binding_invalid")

        latch = repositories.observer.latch(
            site_id=bindings["site_id"],
            processing_purpose=_PURPOSE,
        )
        if (
            latch.site_id != bindings["site_id"]
            or latch.processing_purpose != _PURPOSE
            or not latch.is_open
        ):
            raise CanaryChainVerificationError("model_fatal_latch_closed")
    except CanaryChainVerificationError:
        raise
    except Exception as exc:
        raise CanaryChainVerificationError("projection_read_failed") from exc
    finally:
        with suppress(Exception):
            repositories.close()

    body: dict[str, Any] = {
        "schema_version": "1.0",
        "attestation_type": "local_email_deepseek_context_frappe_chain",
        "generated_at": _format_time(generated_at),
        "run_id": bindings["run_id"],
        "site_id": bindings["site_id"],
        "processing_purpose": _PURPOSE,
        "source_commit": bindings["source_commit"],
        "manifest_sha256": bindings["manifest_sha256"],
        "activation_time": bindings["activation_time"],
        "observation_window": {
            "started_at": _format_time(start),
            "ended_at": _format_time(end),
        },
        "chain": {
            "agent_invocation_count": 1,
            "invocation_ref_sha256": agent.invocation_ref_sha256,
            "requested_model": agent.requested_model,
            "response_reported_observed_model": agent.response_reported_observed_model,
            "response_id_present": agent.response_id_present,
            "network_call_count": agent.network_call_count,
            "tool_call_count": agent.tool_call_count,
            "external_send_count": agent.external_send_count,
            "fatal_or_mismatch_invocation_count": 0,
            "context_chain_count": 1,
            "intelligence_ref_sha256": context.intelligence_ref_sha256,
            "email_delivery_count": 1,
            "delivery_ref_sha256": observer.delivery_ref_sha256,
            "raw_body_sha256": observer.raw_body_sha256,
            "observation_ref_sha256": observer.observation_ref_sha256,
            "participant_ref_sha256": observer.participant_ref_sha256,
            "identity_resolution_status": observer.resolution_status,
            "identity_target_type": observer.target_type,
            "observer_authority_active": observer.authority_active,
            "invocation_ordinal": context.invocation_ordinal,
            "review_status": context.review_status,
            "model_name": context.model_name,
            "model_version": context.model_version,
            "context_state_bound": context.draft_state_bound,
            "draft_status": context.draft_status,
            "receipt_doctype": context.receipt_doctype,
            "receipt_name_present": context.receipt_name_present,
            "receipt_revision": context.receipt_revision,
            "receipt_request_present": context.receipt_request_present,
            "receipt_request_bound": context.receipt_request_bound,
            "receipt_digest": context.receipt_digest,
            "observer_fatal_latch_open": True,
        },
    }
    attestation = {
        **body,
        "payload_sha256": hashlib.sha256(_canonical(body)).hexdigest(),
    }
    validate_canary_chain_attestation(
        attestation,
        expected_run_id=bindings["run_id"],
        expected_site_id=bindings["site_id"],
        expected_source_commit=bindings["source_commit"],
        expected_manifest_sha256=bindings["manifest_sha256"],
        expected_activation_time=bindings["activation_time"],
    )
    _atomic_new_private(output, json.dumps(attestation, indent=2, sort_keys=True).encode() + b"\n")
    return attestation


def validate_canary_chain_attestation(
    value: object,
    *,
    expected_run_id: str,
    expected_site_id: str,
    expected_source_commit: str,
    expected_manifest_sha256: str,
    expected_activation_time: str,
) -> dict[str, Any]:
    """Revalidate a closed receipt without trusting its file name or caller metadata."""

    if not isinstance(value, dict) or set(value) != _ATTESTATION_FIELDS:
        raise CanaryChainVerificationError("chain_attestation_schema_invalid")
    window = value.get("observation_window")
    chain = value.get("chain")
    if (
        not isinstance(window, dict)
        or set(window) != _WINDOW_FIELDS
        or not isinstance(chain, dict)
        or set(chain) != _CHAIN_FIELDS
    ):
        raise CanaryChainVerificationError("chain_attestation_schema_invalid")
    payload_sha256 = value.get("payload_sha256")
    body = {key: item for key, item in value.items() if key != "payload_sha256"}
    if (
        value.get("schema_version") != "1.0"
        or value.get("attestation_type") != "local_email_deepseek_context_frappe_chain"
        or value.get("run_id") != expected_run_id
        or value.get("site_id") != expected_site_id
        or value.get("processing_purpose") != _PURPOSE
        or value.get("source_commit") != expected_source_commit
        or value.get("manifest_sha256") != expected_manifest_sha256
        or value.get("activation_time") != expected_activation_time
        or not isinstance(payload_sha256, str)
        or _HEX.fullmatch(payload_sha256) is None
        or hashlib.sha256(_canonical(body)).hexdigest() != payload_sha256
    ):
        raise CanaryChainVerificationError("chain_attestation_binding_invalid")
    activation = _timestamp(value.get("activation_time"), "chain_attestation_window_invalid")
    started_at = _timestamp(window.get("started_at"), "chain_attestation_window_invalid")
    ended_at = _timestamp(window.get("ended_at"), "chain_attestation_window_invalid")
    generated_at = _timestamp(value.get("generated_at"), "chain_attestation_window_invalid")
    if not activation <= started_at < ended_at <= generated_at:
        raise CanaryChainVerificationError("chain_attestation_window_invalid")
    if (
        chain.get("agent_invocation_count") != 1
        or not _nonzero_hex(chain.get("invocation_ref_sha256"))
        or chain.get("requested_model") != _MODEL
        or chain.get("response_reported_observed_model") != _MODEL
        or chain.get("response_id_present") is not True
        or not isinstance(chain.get("network_call_count"), int)
        or isinstance(chain.get("network_call_count"), bool)
        or int(chain["network_call_count"]) <= 0
        or chain.get("tool_call_count") != 0
        or chain.get("external_send_count") != 0
        or chain.get("fatal_or_mismatch_invocation_count") != 0
        or chain.get("context_chain_count") != 1
        or not _nonzero_hex(chain.get("intelligence_ref_sha256"))
        or chain.get("email_delivery_count") != 1
        or not _nonzero_hex(chain.get("delivery_ref_sha256"))
        or not _nonzero_hex(chain.get("raw_body_sha256"))
        or not _nonzero_hex(chain.get("observation_ref_sha256"))
        or not _nonzero_hex(chain.get("participant_ref_sha256"))
        or chain.get("identity_resolution_status") != "confirmed"
        or chain.get("identity_target_type") not in {"User", "Party"}
        or chain.get("observer_authority_active") is not True
        or chain.get("invocation_ordinal") != 1
        or chain.get("review_status") != "AI Draft"
        or chain.get("model_name") != _MODEL
        or chain.get("model_version") != _MODEL
        or chain.get("context_state_bound") is not True
        or chain.get("draft_status") != "succeeded"
        or chain.get("receipt_doctype") != _DOCTYPE
        or chain.get("receipt_name_present") is not True
        or not isinstance(chain.get("receipt_revision"), int)
        or isinstance(chain.get("receipt_revision"), bool)
        or int(chain["receipt_revision"]) <= 0
        or chain.get("receipt_request_present") is not True
        or chain.get("receipt_request_bound") is not True
        or not _nonzero_hex(chain.get("receipt_digest"))
        or chain.get("observer_fatal_latch_open") is not True
    ):
        raise CanaryChainVerificationError("chain_attestation_facts_invalid")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify one deliberately narrow Email-to-Frappe canary observation window. "
            "Zero or multiple projection rows fail closed."
        )
    )
    parser.add_argument("--canary-dir", required=True, type=Path)
    parser.add_argument("--projection-config", required=True, type=Path)
    parser.add_argument("--window-start", required=True)
    parser.add_argument("--window-end", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def cli_main(
    argv: Sequence[str] | None = None,
    *,
    repo_root: Path,
) -> int:
    """Run the verifier with the launcher-resolved repository root."""

    args = _parser().parse_args(argv)
    try:
        source_commit = _repository_source_commit(repo_root)
        verify_canary_chain(
            canary_dir=args.canary_dir,
            projection_config_path=args.projection_config,
            output_path=args.output,
            window_start=_timestamp(args.window_start, "observation_window_invalid"),
            window_end=_timestamp(args.window_end, "observation_window_invalid"),
            expected_source_commit=source_commit,
            repo_root=repo_root,
        )
    except CanaryChainVerificationError as exc:
        print(f"CANARY CHAIN VERIFICATION FAILED: {exc.code}", file=sys.stderr)
        return 78
    print(f"Created private canary chain attestation at {args.output}.")
    return 0


def _repository_source_commit(repo_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CanaryChainVerificationError("source_commit_unavailable") from exc
    value = result.stdout.strip()
    if result.returncode != 0 or _COMMIT.fullmatch(value) is None:
        raise CanaryChainVerificationError("source_commit_unavailable")
    return value


def _validate_bindings(
    *,
    actual_manifest_sha256: str,
    manifest: Mapping[str, Any],
    control: Mapping[str, Any],
    expected_source_commit: str,
) -> dict[str, str]:
    site_id = manifest.get("site_id")
    source_commit = control.get("source_commit")
    manifest_sha256 = control.get("manifest_sha256")
    run_id = control.get("run_id")
    activation_time = control.get("activation_time")
    if (
        manifest.get("schema_version") != "1.0"
        or not isinstance(site_id, str)
        or _SITE.fullmatch(site_id) is None
        or manifest.get("local_pilot_go") is not True
        or manifest.get("production_go") is not False
        or control.get("schema_version") != "1.1"
        or control.get("state") != "prepared"
        or control.get("stability_assessment") != _DEFERRED_STABILITY
        or not isinstance(run_id, str)
        or not _valid_run_id(run_id)
        or not isinstance(source_commit, str)
        or _COMMIT.fullmatch(source_commit) is None
        or source_commit != expected_source_commit
        or not isinstance(manifest_sha256, str)
        or _HEX.fullmatch(manifest_sha256) is None
        or actual_manifest_sha256 != manifest_sha256
        or not isinstance(activation_time, str)
    ):
        raise CanaryChainVerificationError("canary_binding_invalid")
    scope = control.get("scope")
    channels = manifest.get("channels")
    deepseek = manifest.get("deepseek")
    capabilities = manifest.get("capabilities")
    if (
        scope
        != {
            "channels": ["email"],
            "model": _MODEL,
            "external_send": False,
            "formal_commands": False,
        }
        or not isinstance(channels, Mapping)
        or not isinstance(channels.get("email"), Mapping)
        or channels["email"].get("enabled") is not True
        or channels["email"].get("activation_time") != activation_time
        or not isinstance(deepseek, Mapping)
        or deepseek.get("enabled") is not True
        or deepseek.get("kill_switch") is not False
        or deepseek.get("model") != _MODEL
        or not isinstance(capabilities, Mapping)
        or capabilities.get("external_send") is not False
        or capabilities.get("formal_business_commands") is not False
    ):
        raise CanaryChainVerificationError("canary_scope_invalid")
    _timestamp(activation_time, "control_invalid")
    return {
        "site_id": site_id,
        "run_id": run_id,
        "source_commit": source_commit,
        "manifest_sha256": manifest_sha256,
        "activation_time": activation_time,
    }


def _validate_agent(
    row: AgentInvocationProjection,
    *,
    site_id: str,
    start: datetime,
    end: datetime,
) -> None:
    started_at = _aware(row.started_at, "agent_invocation_invalid")
    completed_at = (
        None if row.completed_at is None else _aware(row.completed_at, "agent_invocation_invalid")
    )
    if (
        row.site_id != site_id
        or _HEX.fullmatch(row.invocation_ref_sha256) is None
        or row.requested_model != _MODEL
        or row.response_reported_observed_model != _MODEL
        or row.response_id_present is not True
        or not isinstance(row.network_call_count, int)
        or isinstance(row.network_call_count, bool)
        or row.network_call_count <= 0
        or row.tool_call_count != 0
        or row.external_send_count != 0
        or row.status != "succeeded"
        or row.error_code is not None
        or completed_at is None
        or not start <= started_at <= completed_at <= end
    ):
        raise CanaryChainVerificationError("agent_invocation_invalid")


def _validate_context(
    row: ContextChainProjection,
    *,
    site_id: str,
    start: datetime,
    end: datetime,
) -> None:
    created_at = _aware(row.created_at, "context_chain_invalid")
    updated_at = _aware(row.updated_at, "context_chain_invalid")
    if (
        row.site_id != site_id
        or row.processing_purpose != _PURPOSE
        or _HEX.fullmatch(row.invocation_ref_sha256) is None
        or _HEX.fullmatch(row.intelligence_ref_sha256) is None
        or _HEX.fullmatch(row.observation_ref_sha256) is None
        or row.invocation_ordinal != 1
        or row.review_status != "AI Draft"
        or row.model_name != _MODEL
        or row.model_version != _MODEL
        or row.draft_state_bound is not True
        or row.draft_status != "succeeded"
        or row.receipt_doctype != _DOCTYPE
        or row.receipt_name_present is not True
        or not isinstance(row.receipt_revision, int)
        or isinstance(row.receipt_revision, bool)
        or row.receipt_revision <= 0
        or row.receipt_request_present is not True
        or row.receipt_request_bound is not True
        or not isinstance(row.receipt_digest, str)
        or _HEX.fullmatch(row.receipt_digest) is None
        or not start <= created_at <= updated_at <= end
    ):
        raise CanaryChainVerificationError("context_chain_invalid")


def _validate_observer(
    row: ObserverChainProjection,
    *,
    site_id: str,
    start: datetime,
    end: datetime,
) -> None:
    received_at = _aware(row.received_at, "observer_chain_invalid")
    ingested_at = _aware(row.ingested_at, "observer_chain_invalid")
    if (
        row.site_id != site_id
        or row.processing_purpose != _PURPOSE
        or not _nonzero_hex(row.delivery_ref_sha256)
        or not _nonzero_hex(row.raw_body_sha256)
        or row.delivery_status != "succeeded"
        or not _nonzero_hex(row.observation_ref_sha256)
        or row.connector != "email"
        or row.channel != "email"
        or not _nonzero_hex(row.participant_ref_sha256)
        or row.identity_work_status != "confirmed"
        or row.resolution_status != "confirmed"
        or row.target_type not in {"User", "Party"}
        or row.authority_active is not True
        or not start <= received_at <= ingested_at <= end
    ):
        raise CanaryChainVerificationError("observer_chain_invalid")


def _external_private_directory(path: Path, repo_root: Path, code: str) -> Path:
    try:
        details = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise CanaryChainVerificationError(code) from exc
    if (
        path.is_symlink()
        or not stat.S_ISDIR(details.st_mode)
        or stat.S_IMODE(details.st_mode) != 0o700
        or _is_within(resolved, repo_root)
    ):
        raise CanaryChainVerificationError(code)
    return resolved


def _new_external_output(path: Path, repo_root: Path, canary_dir: Path) -> Path:
    requested_parent = path.parent
    try:
        parent_details = requested_parent.lstat()
        parent = requested_parent.resolve(strict=True)
        requested_parent_absolute = requested_parent.absolute()
    except OSError as exc:
        raise CanaryChainVerificationError("attestation_output_invalid") from exc
    output = parent / path.name
    if (
        path.exists()
        or path.is_symlink()
        or requested_parent.is_symlink()
        or not stat.S_ISDIR(parent_details.st_mode)
        or requested_parent_absolute != parent
        or path.name in {"", ".", ".."}
        or _is_within(output, repo_root)
        or _is_within(output, canary_dir)
    ):
        raise CanaryChainVerificationError("attestation_output_invalid")
    return output


def _read_private_json(path: Path, code: str) -> dict[str, Any]:
    value, _raw = _read_private_json_document(path, code)
    return value


def _read_private_json_document(path: Path, code: str) -> tuple[dict[str, Any], bytes]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CanaryChainVerificationError(code) from exc
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o600
            or not 0 < details.st_size <= _MAX_JSON_BYTES
        ):
            raise CanaryChainVerificationError(code)
        chunks: list[bytes] = []
        remaining = details.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise CanaryChainVerificationError(code)
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(raw) != details.st_size:
        raise CanaryChainVerificationError(code)
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CanaryChainVerificationError(code) from exc
    if not isinstance(value, dict):
        raise CanaryChainVerificationError(code)
    return value, raw


def _atomic_new_private(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    published = False
    linked = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        try:
            os.link(temporary, path, follow_symlinks=False)
            linked = True
        except OSError as exc:
            raise CanaryChainVerificationError("attestation_output_invalid") from exc
        parent_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
        published = True
    finally:
        if linked and not published:
            path.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)


def _timestamp(value: object, code: str) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise CanaryChainVerificationError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CanaryChainVerificationError(code) from exc
    return _aware(parsed, code)


def _aware(value: datetime, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CanaryChainVerificationError(code)
    return value.astimezone(UTC)


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _query_limit(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 2:
        raise CanaryChainVerificationError("projection_query_invalid")


def _nonzero_hex(value: object) -> bool:
    return isinstance(value, str) and _HEX.fullmatch(value) is not None and value != "0" * 64


def _valid_run_id(value: str) -> bool:
    try:
        parsed = UUID(value)
    except ValueError:
        return False
    return parsed.version == 4 and str(parsed) == value


def _technical_ref_digest(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        raise CanaryChainVerificationError("projection_read_failed")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_within(path: Path, directory: Path) -> bool:
    try:
        return os.path.commonpath((str(path), str(directory))) == str(directory)
    except ValueError:
        return False


__all__ = [
    "AgentInvocationProjection",
    "CanaryChainVerificationError",
    "CanaryProjectionRepositories",
    "ContextChainProjection",
    "ObserverChainProjection",
    "ObserverLatchProjection",
    "PostgresAgentCanaryRepository",
    "PostgresContextCanaryRepository",
    "PostgresObserverCanaryRepository",
    "cli_main",
    "create_projection_repositories",
    "validate_canary_chain_attestation",
    "verify_canary_chain",
]
