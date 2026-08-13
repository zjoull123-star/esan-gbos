"""Observer-only publication relay to the Email Gateway HTTP boundary."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any

import httpx

from services.agent_runtime.local_entrypoint import (
    LocalEntrypointDisabled,
    load_local_manifest,
    require_component_enabled,
)
from services.email_gateway.security import validate_participant_authority_binding
from services.observer.observer.email_publication_outbox import (
    EmailPublicationRelayClaim as ObserverClaim,
)
from services.observer.observer.email_publication_outbox import (
    PostgresEmailPublicationRelay,
)
from services.observer.observer.models import TenantScope

from .email_gateway_config import (
    EMAIL_GATEWAY_API_URL,
    EMAIL_PUBLICATION_AUTH_REF,
    EmailGatewayConfigError,
    load_email_gateway_config,
    require_gateway_component,
)
from .email_gateway_worker import FencedHttpRelayWorker, RelayClaim, RelayResult, RelayStatus
from .runtime_support import (
    RuntimeSupportError,
    close_connection,
    connect_postgres,
    load_secret_file,
    reject_plaintext_secret_environment,
)
from .secret_provider import MountedFileSecretProvider, SecretSpec

DEFAULT_MANIFEST = Path("/config/local-pilot-manifest.json")
DEFAULT_CONFIG = Path("/config/email-gateway-runtime.json")
DEFAULT_EMERGENCY_STOP = Path("/run/gbos/EMERGENCY_STOP")


class EmailPublicationRelayWorker(FencedHttpRelayWorker):
    @property
    def endpoint(self) -> str:
        return EMAIL_GATEWAY_API_URL + "/internal/v1/email-publications/accept"

    @property
    def auth_ref(self) -> str:
        return EMAIL_PUBLICATION_AUTH_REF

    @property
    def purpose(self) -> str:
        return "observation_processing"

    @property
    def identity_field(self) -> str:
        return "publication_id"

    def _receipt(
        self,
        status: int,
        response: object,
        claim: RelayClaim,
    ) -> str | None:
        if (
            status != 200
            or not isinstance(response, dict)
            or set(response) != {"schema_version", "binding"}
            or response.get("schema_version") != "1.0"
            or not isinstance(response.get("binding"), Mapping)
        ):
            return None
        raw_binding = response["binding"]
        inbox_item_ref = raw_binding.get("inbox_item_ref")
        if not isinstance(inbox_item_ref, str):
            return None
        try:
            binding = validate_participant_authority_binding(
                raw_binding,
                inbox_item_ref=inbox_item_ref,
            )
        except ValueError:
            return None
        if (
            binding["publication_ref"] != claim.item_ref
            or binding["payload_digest"] != claim.payload_digest
        ):
            return None
        return str(binding["gateway_receipt_ref"])

    def _delivery_receipt(self, response: dict[str, Any]) -> Mapping[str, object]:
        binding = response.get("binding")
        if not isinstance(binding, Mapping):  # pragma: no cover - guarded by _receipt
            raise ValueError("publication authority binding is unavailable")
        return dict(binding)


@dataclass(frozen=True, slots=True)
class PublicationRelayClaim:
    site_id: str
    item_ref: str
    request_id: str
    payload: Mapping[str, Any]
    payload_digest: str
    attempt: int
    max_attempts: int
    generation: int
    fence_token: str
    source: ObserverClaim = field(repr=False)


class ObserverPublicationOutboxAdapter:
    """Expose the Observer-owned fenced relay without a Gateway credential."""

    def __init__(
        self,
        relay: PostgresEmailPublicationRelay,
        *,
        site_id: str,
        purpose: str = "observation_processing",
    ) -> None:
        self._relay = relay
        self._scope = TenantScope(site_id, purpose)

    def claim(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> RelayClaim | None:
        claim = self._relay.claim(
            site_id=self._scope.site_id,
            worker_id=worker_id,
            now=now,
            lease_seconds=int(lease_duration.total_seconds()),
        )
        return None if claim is None else self._wrap(claim)

    def heartbeat(
        self,
        claim: RelayClaim,
        *,
        now: datetime,
        lease_duration: timedelta,
    ) -> None:
        publication_claim = self._publication_claim(claim)
        self._relay.heartbeat(
            site_id=self._scope.site_id,
            publication_id=publication_claim.item_ref,
            worker_id=publication_claim.source.lease_owner,
            expected_generation=publication_claim.generation,
            now=now,
            lease_seconds=int(lease_duration.total_seconds()),
        )

    def mark_delivered(
        self,
        claim: RelayClaim,
        *,
        receipt: Mapping[str, object],
        now: datetime,
    ) -> None:
        publication_claim = self._publication_claim(claim)
        self._relay.acknowledge(
            site_id=self._scope.site_id,
            publication_id=publication_claim.item_ref,
            worker_id=publication_claim.source.lease_owner,
            expected_generation=publication_claim.generation,
            receipt=receipt,
            now=now,
        )

    def mark_failed(
        self,
        claim: RelayClaim,
        *,
        retry_at: datetime,
        error_code: str,
        now: datetime,
    ) -> str:
        publication_claim = self._publication_claim(claim)
        state = self._relay.release(
            site_id=self._scope.site_id,
            publication_id=publication_claim.item_ref,
            worker_id=publication_claim.source.lease_owner,
            expected_generation=publication_claim.generation,
            now=now,
            next_attempt_at=retry_at,
            error_code=error_code,
        )
        return "dead_letter" if state.status == "dead_letter" else "retry"

    @staticmethod
    def _wrap(claim: ObserverClaim) -> PublicationRelayClaim:
        return PublicationRelayClaim(
            site_id=claim.site_id,
            item_ref=claim.publication_id,
            request_id=f"email-publication:{claim.publication_id}",
            payload=claim.payload,
            payload_digest=claim.transport_payload_digest,
            attempt=claim.attempt_count,
            max_attempts=claim.max_attempts,
            generation=claim.generation,
            fence_token=f"generation:{claim.generation}",
            source=claim,
        )

    @staticmethod
    def _publication_claim(claim: RelayClaim) -> PublicationRelayClaim:
        if not isinstance(claim, PublicationRelayClaim):
            raise TypeError("Observer publication relay claim is invalid")
        return claim


class HttpxPublicationTransport:
    def post(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> tuple[int, dict[str, Any]]:
        if url != EMAIL_GATEWAY_API_URL + "/internal/v1/email-publications/accept":
            raise RuntimeError("publication relay URL rejected")
        try:
            with httpx.Client(timeout=timeout_seconds, trust_env=False) as client:
                response = client.post(url, headers=dict(headers), json=dict(payload))
            if len(response.content) > 16_384:
                raise RuntimeError("publication relay response is unbounded")
            body = response.json()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise RuntimeError("publication relay unavailable") from exc
        if not isinstance(body, dict):
            raise RuntimeError("publication relay response is invalid")
        return response.status_code, body


def run_relay_daemon(
    worker: EmailPublicationRelayWorker,
    *,
    stop_event: Event,
    idle_delay_seconds: float,
) -> None:
    while not stop_event.is_set():
        result = worker.run_once()
        if result.status != RelayStatus.DELIVERED:
            stop_event.wait(idle_delay_seconds)


def main(
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    config_path: Path = DEFAULT_CONFIG,
    emergency_stop_path: Path = DEFAULT_EMERGENCY_STOP,
    environ: Mapping[str, str] | None = None,
    connector: Callable[..., object] | None = None,
    transport_factory: Callable[[], HttpxPublicationTransport] | None = None,
    daemon_runner: Callable[..., None] | None = None,
    stop_event: Event | None = None,
) -> int:
    """Compose the Observer publisher role only after every static guard passes."""

    environment = os.environ if environ is None else environ
    connection: object | None = None
    try:
        reject_plaintext_secret_environment(environment)
        manifest = load_local_manifest(manifest_path)
        require_component_enabled(
            manifest, component="email-publication-worker", environ=environment
        )
        gateway = manifest.get("email_gateway")
        if (
            environment.get("GBOS_EMAIL_PUBLICATION_KILL_SWITCH", "true") != "false"
            or environment.get("GBOS_EXTERNAL_SEND_ENABLED", "false") != "false"
            or emergency_stop_path.exists()
            or not isinstance(gateway, Mapping)
            or gateway.get("publication_kill_switch") is not False
            or gateway.get("external_send") is not False
        ):
            raise LocalEntrypointDisabled("email publication relay is disabled")
        config = load_email_gateway_config(config_path)
        require_gateway_component(config, "email_publication_worker")
        if (
            config.site_id != manifest.get("site_id")
            or config.postgres.user != "gbos_observer_publisher"
        ):
            raise EmailGatewayConfigError("Observer publisher role binding is invalid")
        secret_provider = _secret_provider()
        bearer = load_secret_file(
            config.auth.email_publication_bearer_file,
            secret_provider=secret_provider,
            logical_name="email_publication_bearer",
        )
        connection = connect_postgres(
            config.postgres,
            connector=connector,
            secret_provider=secret_provider,
            environ=environment,
        )
        outbox = ObserverPublicationOutboxAdapter(
            PostgresEmailPublicationRelay(connection),  # type: ignore[arg-type]
            site_id=config.site_id,
        )
        transport = (transport_factory or HttpxPublicationTransport)()
        worker = EmailPublicationRelayWorker(
            outbox=outbox,
            transport=transport,
            bearer_token=bearer.reveal(),
            worker_id=config.worker.worker_id,
            clock=lambda: datetime.now(UTC),
            lease_duration=timedelta(seconds=30),
        )
        active_runner = daemon_runner or run_relay_daemon
        active_runner(
            worker,
            stop_event=stop_event or Event(),
            idle_delay_seconds=config.worker.idle_delay_seconds,
        )
        return 0
    except (
        EmailGatewayConfigError,
        LocalEntrypointDisabled,
        RuntimeSupportError,
        TypeError,
        ValueError,
    ):
        return 78
    finally:
        if connection is not None:
            close_connection(connection)


def _secret_provider() -> MountedFileSecretProvider:
    return MountedFileSecretProvider(
        Path("/run/secrets"),
        (
            SecretSpec(
                "postgres_observer_publisher_password",
                "postgres_observer_publisher_password",
                "text",
                16,
                128,
            ),
            SecretSpec(
                "email_publication_bearer",
                "email_publication_bearer",
                "text",
                16,
                4096,
            ),
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EmailPublicationRelayWorker",
    "HttpxPublicationTransport",
    "ObserverPublicationOutboxAdapter",
    "PublicationRelayClaim",
    "RelayResult",
    "RelayStatus",
    "main",
    "run_relay_daemon",
]
