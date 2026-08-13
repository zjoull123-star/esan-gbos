"""Gateway-only mailbox configuration relay to the Observer HTTP boundary."""

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
from services.email_gateway.models import PROCESSING_PURPOSES, TenantScope, ValidationError
from services.email_gateway.repositories.mailboxes import (
    MailboxConfigOutboxClaim as GatewayClaim,
)
from services.email_gateway.repositories.mailboxes import (
    PostgresMailboxConfigOutboxRepository,
)

from .email_gateway_config import (
    MAILBOX_PROJECTION_AUTH_REF,
    OBSERVER_CONFIG_API_URL,
    EmailGatewayConfigError,
    load_email_gateway_config,
    require_gateway_component,
)
from .email_gateway_worker import FencedHttpRelayWorker, RelayClaim, RelayStatus
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


class MailboxConfigProjectionWorker(FencedHttpRelayWorker):
    @property
    def endpoint(self) -> str:
        return OBSERVER_CONFIG_API_URL + "/internal/v1/email-connectors/apply-config"

    @property
    def auth_ref(self) -> str:
        return MAILBOX_PROJECTION_AUTH_REF

    @property
    def purpose(self) -> str:
        return "observation_processing"

    @property
    def identity_field(self) -> str:
        return "config_publication_ref"


@dataclass(frozen=True, slots=True)
class ConfigProjectionRelayClaim:
    site_id: str
    item_ref: str
    request_id: str
    payload: Mapping[str, Any] | None
    payload_digest: str
    attempt: int
    max_attempts: int
    generation: int
    fence_token: str
    source: GatewayClaim = field(repr=False)


class GatewayConfigOutboxAdapter:
    """Expose only the Gateway worker-role configuration outbox operations."""

    def __init__(
        self,
        repository: PostgresMailboxConfigOutboxRepository,
        *,
        site_id: str,
    ) -> None:
        self._repository = repository
        self._site_id = site_id

    def claim(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> RelayClaim | None:
        for purpose in sorted(PROCESSING_PURPOSES):
            source = self._repository.claim(
                TenantScope(self._site_id, purpose),
                worker_id=worker_id,
                now=now,
                lease_duration=lease_duration,
            )
            if source is not None:
                return self._wrap(source)
        return None

    def heartbeat(
        self,
        claim: RelayClaim,
        *,
        now: datetime,
        lease_duration: timedelta,
    ) -> None:
        wrapped = self._claim(claim)
        self._repository.heartbeat(
            TenantScope(wrapped.site_id, wrapped.source.processing_purpose),
            wrapped.item_ref,
            worker_id=wrapped.source.lease_owner,
            expected_attempt=wrapped.attempt,
            fence_token=wrapped.fence_token,
            now=now,
            lease_duration=lease_duration,
        )

    def mark_delivered(
        self,
        claim: RelayClaim,
        *,
        receipt: Mapping[str, object],
        now: datetime,
    ) -> None:
        wrapped = self._claim(claim)
        receipt_ref = receipt.get("receipt_ref")
        if not isinstance(receipt_ref, str):
            raise ValueError("configuration receipt is invalid")
        self._repository.mark_delivered(
            TenantScope(wrapped.site_id, wrapped.source.processing_purpose),
            wrapped.item_ref,
            worker_id=wrapped.source.lease_owner,
            expected_attempt=wrapped.attempt,
            fence_token=wrapped.fence_token,
            receipt_ref=receipt_ref,
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
        wrapped = self._claim(claim)
        return self._repository.mark_failed(
            TenantScope(wrapped.site_id, wrapped.source.processing_purpose),
            wrapped.item_ref,
            worker_id=wrapped.source.lease_owner,
            expected_attempt=wrapped.attempt,
            fence_token=wrapped.fence_token,
            now=now,
            retry_at=retry_at,
            error_code=error_code,
        )

    @staticmethod
    def _wrap(source: GatewayClaim) -> ConfigProjectionRelayClaim:
        try:
            payload = source.to_connector_projection_wire()
        except ValidationError:
            payload = None
            payload_digest = source.payload_digest
        else:
            if payload is None:
                raise TypeError("mailbox projection payload is unavailable")
            payload_digest = str(payload["projection_digest"])
        return ConfigProjectionRelayClaim(
            site_id=source.site_id,
            item_ref=source.config_publication_ref,
            request_id=source.config_publication_ref,
            payload=payload,
            payload_digest=payload_digest,
            attempt=source.attempt,
            max_attempts=5,
            generation=source.lease_generation,
            fence_token=source.fence_token,
            source=source,
        )

    @staticmethod
    def _claim(claim: RelayClaim) -> ConfigProjectionRelayClaim:
        if not isinstance(claim, ConfigProjectionRelayClaim):
            raise TypeError("Gateway config relay claim is invalid")
        return claim


class HttpxMailboxConfigTransport:
    def post(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> tuple[int, dict[str, Any]]:
        if url != OBSERVER_CONFIG_API_URL + "/internal/v1/email-connectors/apply-config":
            raise RuntimeError("mailbox configuration URL rejected")
        try:
            with httpx.Client(timeout=timeout_seconds, trust_env=False) as client:
                response = client.post(url, headers=dict(headers), json=dict(payload))
            if len(response.content) > 16_384:
                raise RuntimeError("mailbox configuration response is unbounded")
            body = response.json()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise RuntimeError("mailbox configuration relay unavailable") from exc
        if not isinstance(body, dict):
            raise RuntimeError("mailbox configuration response is invalid")
        return response.status_code, body


def run_relay_daemon(
    worker: MailboxConfigProjectionWorker,
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
    transport_factory: Callable[[], HttpxMailboxConfigTransport] | None = None,
    daemon_runner: Callable[..., None] | None = None,
    stop_event: Event | None = None,
) -> int:
    """Compose the Gateway worker role only after every static guard passes."""

    environment = os.environ if environ is None else environ
    connection: object | None = None
    try:
        reject_plaintext_secret_environment(environment)
        manifest = load_local_manifest(manifest_path)
        require_component_enabled(
            manifest,
            component="mailbox-config-projection-worker",
            environ=environment,
        )
        gateway = manifest.get("email_gateway")
        if (
            environment.get("GBOS_EMAIL_GATEWAY_KILL_SWITCH", "true") != "false"
            or environment.get("GBOS_EXTERNAL_SEND_ENABLED", "false") != "false"
            or emergency_stop_path.exists()
            or not isinstance(gateway, Mapping)
            or gateway.get("kill_switch") is not False
            or gateway.get("external_send") is not False
        ):
            raise LocalEntrypointDisabled("mailbox configuration relay is disabled")
        config = load_email_gateway_config(config_path)
        require_gateway_component(config, "mailbox_config_projection_worker")
        if (
            config.site_id != manifest.get("site_id")
            or config.postgres.user != "gbos_email_gateway_worker"
        ):
            raise EmailGatewayConfigError("Gateway worker role binding is invalid")
        secret_provider = _secret_provider()
        bearer = load_secret_file(
            config.auth.mailbox_projection_bearer_file,
            secret_provider=secret_provider,
            logical_name="mailbox_projection_bearer",
        )
        connection = connect_postgres(
            config.postgres,
            connector=connector,
            secret_provider=secret_provider,
            environ=environment,
        )
        outbox = GatewayConfigOutboxAdapter(
            PostgresMailboxConfigOutboxRepository(connection),  # type: ignore[arg-type]
            site_id=config.site_id,
        )
        worker = MailboxConfigProjectionWorker(
            outbox=outbox,
            transport=(transport_factory or HttpxMailboxConfigTransport)(),
            bearer_token=bearer.reveal(),
            worker_id=config.worker.worker_id,
            clock=lambda: datetime.now(UTC),
            lease_duration=timedelta(seconds=30),
        )
        (daemon_runner or run_relay_daemon)(
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
                "postgres_email_gateway_password",
                "postgres_email_gateway_password",
                "text",
                16,
                128,
            ),
            SecretSpec(
                "mailbox_projection_bearer",
                "mailbox_projection_bearer",
                "text",
                16,
                4096,
            ),
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ConfigProjectionRelayClaim",
    "GatewayConfigOutboxAdapter",
    "HttpxMailboxConfigTransport",
    "MailboxConfigProjectionWorker",
    "RelayStatus",
    "main",
    "run_relay_daemon",
]
