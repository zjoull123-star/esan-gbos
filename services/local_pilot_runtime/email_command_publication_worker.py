"""HTTP-only fenced relay from Frappe command publication to Gateway ingest."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import httpx

from services.agent_runtime.local_entrypoint import (
    LocalEntrypointDisabled,
    load_local_manifest,
    require_component_enabled,
)

from .runtime_support import reject_plaintext_secret_environment

FRAPPE_BASE_URL = "http://frappe-backend:8000"
GATEWAY_BASE_URL = "http://email-gateway-api:8004"
FRAPPE_METHOD = FRAPPE_BASE_URL + "/api/method/esan_gbos.api.internal.email_command_publication."
GATEWAY_ACCEPT = GATEWAY_BASE_URL + "/internal/v1/email-commands/accept"
DEFAULT_MANIFEST = Path("/config/local-pilot-manifest.json")


class PublicationRelayStatus(StrEnum):
    IDLE = "idle"
    DELIVERED = "delivered"
    RETRY = "retry"
    DEAD_LETTER = "dead_letter"


@dataclass(frozen=True, slots=True)
class PublicationRelayResult:
    status: PublicationRelayStatus
    publication_ref: str | None = None


class JsonTransport(Protocol):
    def post(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> tuple[int, dict[str, Any]]: ...


class HttpxJsonTransport:
    def post(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> tuple[int, dict[str, Any]]:
        if not (url.startswith(FRAPPE_METHOD) or url == GATEWAY_ACCEPT):
            raise RuntimeError("command relay URL rejected")
        try:
            with httpx.Client(timeout=timeout_seconds, trust_env=False) as client:
                response = client.post(url, headers=dict(headers), json=dict(payload))
            if len(response.content) > 262_144:
                raise RuntimeError("command relay response is unbounded")
            body = response.json()
        except (httpx.HTTPError, OSError, ValueError) as error:
            raise RuntimeError("command relay unavailable") from error
        if not isinstance(body, dict):
            raise RuntimeError("command relay response is invalid")
        return response.status_code, body


class FrappeCommandPublicationClient:
    def __init__(
        self,
        *,
        transport: JsonTransport,
        api_key: str,
        api_secret: str,
        timeout_seconds: float = 5.0,
    ) -> None:
        if not api_key or not api_secret or ":" in api_key or not 0 < timeout_seconds <= 10:
            raise ValueError("invalid Frappe publication client")
        self._transport = transport
        self._authorization = f"token {api_key}:{api_secret}"
        self._timeout = timeout_seconds

    def post(self, method: str, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        request_id = str(payload["request_id"])
        return self._transport.post(
            url=FRAPPE_METHOD + method,
            headers={
                "Accept": "application/json",
                "Authorization": self._authorization,
                "Content-Type": "application/json",
                "X-GBOS-Frappe-Auth-Ref": "email-command-publication-v1",
                "X-Processing-Purpose": "email_command_publication",
                "X-Request-ID": request_id,
                "X-Site-ID": str(payload["site_id"]),
            },
            payload=payload,
            timeout_seconds=self._timeout,
        )

    def __repr__(self) -> str:
        return "FrappeCommandPublicationClient(credentials=<redacted>)"


class GatewayCommandIngestClient:
    def __init__(
        self,
        *,
        transport: JsonTransport,
        bearer_token: str,
        timeout_seconds: float = 5.0,
    ) -> None:
        if not bearer_token or not 0 < timeout_seconds <= 10:
            raise ValueError("invalid Gateway command client")
        self._transport = transport
        self._bearer_token = bearer_token
        self._timeout = timeout_seconds

    def accept(
        self,
        *,
        site_id: str,
        processing_purpose: str,
        request_id: str,
        claim: Mapping[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        payload = {
            "publication_ref": claim["publication_ref"],
            "attempt": claim["attempt"],
            "generation": claim["generation"],
            "fence_token": claim["fence_token"],
            "payload_digest": claim["payload_digest"],
            "command": claim["command"],
        }
        return self._transport.post(
            url=GATEWAY_ACCEPT,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._bearer_token}",
                "Content-Type": "application/json",
                "X-Audience": "email-command-executor",
                "X-GBOS-Local-Auth-Ref": "email-command-ingest-v1",
                "X-GBOS-Scope": "email-send-execute",
                "X-Payload-Digest": str(claim["payload_digest"]),
                "X-Processing-Purpose": processing_purpose,
                "X-Request-ID": request_id,
                "X-Site-ID": site_id,
            },
            payload=payload,
            timeout_seconds=self._timeout,
        )

    def __repr__(self) -> str:
        return "GatewayCommandIngestClient(bearer_token=<redacted>)"


class CommandPublicationRelayWorker:
    def __init__(
        self,
        *,
        frappe: FrappeCommandPublicationClient,
        gateway: GatewayCommandIngestClient,
        site_id: str,
        worker_id: str,
        clock: Callable[[], datetime],
        lease_seconds: int,
    ) -> None:
        if not site_id or not worker_id or "@" in worker_id or not 10 <= lease_seconds <= 300:
            raise ValueError("invalid command publication worker")
        self._frappe = frappe
        self._gateway = gateway
        self._site_id = site_id
        self._worker_id = worker_id
        self._clock = clock
        self._lease_seconds = lease_seconds

    def run_once(self) -> PublicationRelayResult:
        claim_request = self._request_id("claim")
        status, body = self._frappe.post(
            "claim",
            {
                "site_id": self._site_id,
                "processing_purpose": "email_command_publication",
                "worker_id": self._worker_id,
                "lease_seconds": self._lease_seconds,
                "request_id": claim_request,
            },
        )
        if status != 200 or set(body) != {"publication"}:
            return PublicationRelayResult(PublicationRelayStatus.RETRY)
        claim = body["publication"]
        if claim is None:
            return PublicationRelayResult(PublicationRelayStatus.IDLE)
        if not _valid_claim(claim):
            return PublicationRelayResult(PublicationRelayStatus.DEAD_LETTER)
        identity = self._identity(claim)
        heartbeat_request = self._request_id("heartbeat", claim)
        heartbeat_status, _heartbeat = self._frappe.post(
            "heartbeat",
            {**identity, "lease_seconds": self._lease_seconds, "request_id": heartbeat_request},
        )
        if heartbeat_status != 200:
            return PublicationRelayResult(
                PublicationRelayStatus.RETRY, str(claim["publication_ref"])
            )
        gateway_request = self._request_id("gateway", claim)
        try:
            gateway_status, gateway_body = self._gateway.accept(
                site_id=self._site_id,
                processing_purpose=str(claim["command"]["processing_purpose"]),
                request_id=gateway_request,
                claim=claim,
            )
        except Exception:
            gateway_status, gateway_body = 503, {}
        if not _valid_gateway_receipt(gateway_status, gateway_body, claim):
            safe_code = (
                "gateway_unavailable"
                if gateway_status == 429 or gateway_status >= 500
                else "gateway_rejected_command"
            )
            release_status, release_body = self._frappe.post(
                "release",
                {
                    **identity,
                    "safe_code": safe_code,
                    "request_id": self._request_id("release", claim),
                },
            )
            released = release_body.get("release") if release_status == 200 else None
            state = released.get("status") if isinstance(released, Mapping) else None
            return PublicationRelayResult(
                PublicationRelayStatus.DEAD_LETTER
                if state == "Dead Letter"
                else PublicationRelayStatus.RETRY,
                str(claim["publication_ref"]),
            )
        acknowledge_status, _ack = self._frappe.post(
            "acknowledge",
            {
                **identity,
                "command_receipt_ref": gateway_body["command_receipt_ref"],
                "send_outbox_ref": gateway_body["send_outbox_ref"],
                "payload_digest": claim["payload_digest"],
                "request_id": self._request_id("acknowledge", claim),
            },
        )
        return PublicationRelayResult(
            PublicationRelayStatus.DELIVERED
            if acknowledge_status == 200
            else PublicationRelayStatus.RETRY,
            str(claim["publication_ref"]),
        )

    def _identity(self, claim: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "site_id": self._site_id,
            "processing_purpose": "email_command_publication",
            "worker_id": self._worker_id,
            "publication_ref": claim["publication_ref"],
            "attempt": claim["attempt"],
            "generation": claim["generation"],
            "fence_token": claim["fence_token"],
        }

    def _request_id(self, phase: str, claim: Mapping[str, Any] | None = None) -> str:
        identity = "none" if claim is None else str(claim["publication_ref"])
        value = self._clock().astimezone(UTC).isoformat().replace("+00:00", "Z")
        return f"{phase}:{self._worker_id}:{identity}:{value}"


def _valid_claim(value: object) -> bool:
    return bool(
        isinstance(value, Mapping)
        and set(value)
        == {
            "publication_ref",
            "attempt",
            "generation",
            "fence_token",
            "lease_expires_at",
            "command",
            "payload_digest",
        }
        and isinstance(value.get("command"), Mapping)
        and str(value.get("publication_ref", "")).startswith("PUB-")
        and str(value.get("fence_token", "")).startswith("FNC-")
        and str(value.get("payload_digest", "")).startswith("sha256:")
    )


def _valid_gateway_receipt(
    status: int,
    body: object,
    claim: Mapping[str, Any],
) -> bool:
    if not isinstance(body, Mapping):
        return False
    command = claim["command"]
    return bool(
        status == 200
        and set(body) == {"command_receipt_ref", "send_outbox_ref", "payload_digest"}
        and str(body.get("command_receipt_ref", "")).startswith("ECR-")
        and str(body.get("send_outbox_ref", "")).startswith("SOB-")
        and body.get("payload_digest") == command.get("payload_sha256")
    )


def main(
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    environ: Mapping[str, str] | None = None,
    transport_factory: Callable[[], JsonTransport] | None = None,
) -> int:
    """Fail closed before credentials or HTTP are constructed."""

    environment = os.environ if environ is None else environ
    try:
        reject_plaintext_secret_environment(environment)
        manifest = load_local_manifest(manifest_path)
        require_component_enabled(
            manifest,
            component="email-command-publication-worker",
            environ=environment,
        )
        gateway = manifest.get("email_gateway")
        if (
            environment.get("GBOS_EMAIL_COMMAND_PUBLICATION_KILL_SWITCH", "true") != "false"
            or not isinstance(gateway, Mapping)
            or gateway.get("command_publication_kill_switch") is not False
            or gateway.get("external_send") is not False
        ):
            raise LocalEntrypointDisabled("email command publication relay is disabled")
        if transport_factory is None:
            transport_factory = HttpxJsonTransport
        transport_factory()
        return 0
    except LocalEntrypointDisabled, ValueError, OSError:
        return 78


__all__ = [
    "CommandPublicationRelayWorker",
    "FrappeCommandPublicationClient",
    "GatewayCommandIngestClient",
    "PublicationRelayResult",
    "PublicationRelayStatus",
    "main",
]
