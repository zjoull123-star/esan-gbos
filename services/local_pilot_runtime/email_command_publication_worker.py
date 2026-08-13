"""HTTP-only fenced relay from Frappe command publication to Gateway ingest."""

from __future__ import annotations

import json
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

from .runtime_support import (
    RuntimeSupportError,
    TextSecretProvider,
    reject_plaintext_secret_environment,
)
from .secret_provider import MountedFileSecretProvider, SecretSpec

FRAPPE_BASE_URL = "http://frappe-backend:8000"
GATEWAY_BASE_URL = "http://email-gateway-api:8004"
FRAPPE_METHOD = FRAPPE_BASE_URL + "/api/method/esan_gbos.api.internal.email_command_publication."
GATEWAY_ACCEPT = GATEWAY_BASE_URL + "/internal/v1/email-commands/accept"
DEFAULT_MANIFEST = Path("/config/local-pilot-manifest.json")
DEFAULT_CONFIG = Path("/config/runtime-email-command-publication-worker.json")
DEFAULT_EMERGENCY_STOP = Path("/run/gbos/EMERGENCY_STOP")
_MAX_CONFIG_BYTES = 65_536
_SECRET_ROOT = Path("/run/secrets")
_FRAPPE_KEY = "frappe_email_command_publication_api_key"
_FRAPPE_SECRET = "frappe_email_command_publication_api_secret"
_GATEWAY_BEARER = "email_gateway_command_ingest_bearer"
_AUTH_PATHS = {
    "frappe_api_key_file": f"/run/secrets/{_FRAPPE_KEY}",
    "frappe_api_secret_file": f"/run/secrets/{_FRAPPE_SECRET}",
    "gateway_bearer_file": f"/run/secrets/{_GATEWAY_BEARER}",
}


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
        response_field = {
            "claim": "publication",
            "heartbeat": "lease",
            "acknowledge": "acknowledgement",
            "release": "release",
        }.get(method)
        if response_field is None:
            raise RuntimeError("Frappe publication request rejected")
        request_id = str(payload["request_id"])
        status, body = self._transport.post(
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
            payload={"payload": dict(payload)},
            timeout_seconds=self._timeout,
        )
        if (
            status != 200
            or set(body) != {"message"}
            or not isinstance(body.get("message"), dict)
            or set(body["message"]) != {response_field}
            or not _valid_frappe_response(method, body["message"][response_field])
        ):
            raise RuntimeError("Frappe publication response rejected")
        return status, body["message"]

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
        runtime_stop_reader: Callable[[], str | None] | None = None,
    ) -> None:
        if not site_id or not worker_id or "@" in worker_id or not 10 <= lease_seconds <= 300:
            raise ValueError("invalid command publication worker")
        self._frappe = frappe
        self._gateway = gateway
        self._site_id = site_id
        self._worker_id = worker_id
        self._clock = clock
        self._lease_seconds = lease_seconds
        self._runtime_stop_reader = runtime_stop_reader or (lambda: None)

    def run_once(self) -> PublicationRelayResult:
        if self._runtime_stop_reader() is not None:
            return PublicationRelayResult(PublicationRelayStatus.IDLE)
        claim_request = self._request_id("claim")
        try:
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
        except Exception:
            return PublicationRelayResult(PublicationRelayStatus.RETRY)
        if status != 200 or set(body) != {"publication"}:
            return PublicationRelayResult(PublicationRelayStatus.RETRY)
        claim = body["publication"]
        if claim is None:
            return PublicationRelayResult(PublicationRelayStatus.IDLE)
        if not _valid_claim(claim):
            return PublicationRelayResult(PublicationRelayStatus.DEAD_LETTER)
        identity = self._identity(claim)
        heartbeat_request = self._request_id("heartbeat", claim)
        try:
            heartbeat_status, _heartbeat = self._frappe.post(
                "heartbeat",
                {
                    **identity,
                    "lease_seconds": self._lease_seconds,
                    "request_id": heartbeat_request,
                },
            )
        except Exception:
            return PublicationRelayResult(
                PublicationRelayStatus.RETRY, str(claim["publication_ref"])
            )
        if heartbeat_status != 200:
            return PublicationRelayResult(
                PublicationRelayStatus.RETRY, str(claim["publication_ref"])
            )
        if self._runtime_stop_reader() is not None:
            return self._release_claim(claim, safe_code="worker_shutdown")
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
            return self._release_claim(claim, safe_code=safe_code)
        try:
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
        except Exception:
            acknowledge_status = 503
        return PublicationRelayResult(
            PublicationRelayStatus.DELIVERED
            if acknowledge_status == 200
            else PublicationRelayStatus.RETRY,
            str(claim["publication_ref"]),
        )

    def _release_claim(
        self,
        claim: Mapping[str, Any],
        *,
        safe_code: str,
    ) -> PublicationRelayResult:
        try:
            release_status, release_body = self._frappe.post(
                "release",
                {
                    **self._identity(claim),
                    "safe_code": safe_code,
                    "request_id": self._request_id("release", claim),
                },
            )
        except Exception:
            release_status, release_body = 503, {}
        released = release_body.get("release") if release_status == 200 else None
        state = released.get("status") if isinstance(released, Mapping) else None
        return PublicationRelayResult(
            PublicationRelayStatus.DEAD_LETTER
            if state == "dead_letter"
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


def _valid_frappe_response(method: str, value: object) -> bool:
    if method == "claim":
        return value is None or _valid_claim(value)
    if not isinstance(value, Mapping):
        return False
    if method == "heartbeat":
        return bool(
            set(value)
            == {
                "publication_ref",
                "attempt",
                "generation",
                "fence_token",
                "lease_expires_at",
            }
            and str(value.get("publication_ref", "")).startswith("PUB-")
            and isinstance(value.get("attempt"), int)
            and isinstance(value.get("generation"), int)
            and str(value.get("fence_token", "")).startswith("FNC-")
            and isinstance(value.get("lease_expires_at"), str)
        )
    if method == "acknowledge":
        return bool(
            set(value)
            == {
                "publication_ref",
                "command_receipt_ref",
                "send_outbox_ref",
                "payload_digest",
                "status",
            }
            and str(value.get("publication_ref", "")).startswith("PUB-")
            and str(value.get("command_receipt_ref", "")).startswith("ECR-")
            and str(value.get("send_outbox_ref", "")).startswith("SOB-")
            and str(value.get("payload_digest", "")).startswith("sha256:")
            and value.get("status") == "acknowledged"
        )
    if method == "release":
        return bool(
            set(value) == {"publication_ref", "status", "safe_code"}
            and str(value.get("publication_ref", "")).startswith("PUB-")
            and value.get("status") in {"retry", "dead_letter"}
            and value.get("safe_code")
            in {
                "gateway_unavailable",
                "gateway_rate_limited",
                "gateway_rejected_command",
                "authority_recheck_failed",
                "worker_shutdown",
            }
        )
    return False


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
    config_path: Path = DEFAULT_CONFIG,
    emergency_stop_path: Path = DEFAULT_EMERGENCY_STOP,
    environ: Mapping[str, str] | None = None,
    transport_factory: Callable[[], JsonTransport] | None = None,
    worker_runner: Callable[[CommandPublicationRelayWorker], None] | None = None,
    secret_provider: TextSecretProvider | None = None,
    clock: Callable[[], datetime] | None = None,
) -> int:
    """Preflight closed local-only dependencies, then run one bounded relay pass."""

    environment = os.environ if environ is None else environ
    try:
        if _emergency_stop_active(emergency_stop_path):
            raise LocalEntrypointDisabled("email command publication emergency stop is active")
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
            or environment.get("GBOS_EXTERNAL_SEND_ENABLED", "false") != "false"
        ):
            raise LocalEntrypointDisabled("email command publication relay is disabled")
        config = _load_config(config_path)
        if (
            config["site_id"] != manifest.get("site_id")
            or config["enabled"] is not True
            or config["kill_switch"] is not False
            or config["external_send"] is not False
        ):
            raise LocalEntrypointDisabled("email command publication config is closed")
        worker_config = config["worker"]
        if not isinstance(worker_config, Mapping):
            raise LocalEntrypointDisabled("email command publication config is invalid")
        active_secrets = secret_provider or _publication_secret_provider()
        api_key = _read_secret(active_secrets, _FRAPPE_KEY, forbid_colon=True)
        api_secret = _read_secret(active_secrets, _FRAPPE_SECRET)
        gateway_bearer = _read_secret(active_secrets, _GATEWAY_BEARER)
        transport = (transport_factory or HttpxJsonTransport)()
        worker = CommandPublicationRelayWorker(
            frappe=FrappeCommandPublicationClient(
                transport=transport,
                api_key=api_key,
                api_secret=api_secret,
            ),
            gateway=GatewayCommandIngestClient(
                transport=transport,
                bearer_token=gateway_bearer,
            ),
            site_id=str(config["site_id"]),
            worker_id=str(worker_config["worker_id"]),
            clock=clock or (lambda: datetime.now(UTC)),
            lease_seconds=int(worker_config["lease_seconds"]),
            runtime_stop_reader=lambda: (
                "worker_shutdown" if _emergency_stop_active(emergency_stop_path) else None
            ),
        )
        (worker_runner or _run_once)(worker)
        return 0
    except LocalEntrypointDisabled, RuntimeSupportError, ValueError, OSError:
        return 78


def _emergency_stop_active(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _load_config(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > _MAX_CONFIG_BYTES:
        raise LocalEntrypointDisabled("email command publication config is unavailable")
    value = json.loads(path.read_bytes())
    required = {
        "schema_version",
        "site_id",
        "enabled",
        "kill_switch",
        "external_send",
        "endpoints",
        "auth",
        "worker",
    }
    if not isinstance(value, dict) or set(value) != required or value["schema_version"] != "1.0":
        raise LocalEntrypointDisabled("email command publication config is invalid")
    endpoints = value["endpoints"]
    auth = value["auth"]
    worker = value["worker"]
    if (
        not isinstance(value["site_id"], str)
        or not isinstance(value["enabled"], bool)
        or not isinstance(value["kill_switch"], bool)
        or not isinstance(value["external_send"], bool)
        or not isinstance(endpoints, dict)
        or endpoints != {"frappe": FRAPPE_BASE_URL, "gateway": GATEWAY_BASE_URL}
        or not isinstance(auth, dict)
        or auth != _AUTH_PATHS
        or not isinstance(worker, dict)
        or set(worker) != {"worker_id", "lease_seconds", "idle_delay_seconds"}
        or not isinstance(worker["worker_id"], str)
        or not worker["worker_id"]
        or "@" in worker["worker_id"]
        or not isinstance(worker["lease_seconds"], int)
        or isinstance(worker["lease_seconds"], bool)
        or not 10 <= worker["lease_seconds"] <= 300
        or not isinstance(worker["idle_delay_seconds"], int | float)
        or isinstance(worker["idle_delay_seconds"], bool)
        or not 0 < worker["idle_delay_seconds"] <= 60
    ):
        raise LocalEntrypointDisabled("email command publication config is invalid")
    return value


def _read_secret(
    provider: TextSecretProvider,
    logical_name: str,
    *,
    forbid_colon: bool = False,
) -> str:
    secret = provider.read_text(logical_name)
    if secret is None:
        raise RuntimeSupportError("email command publication secret is unavailable")
    value = secret.reveal()
    if (
        not 16 <= len(value) <= 128
        or value != value.strip()
        or any(character in value for character in "\x00\r\n")
        or (forbid_colon and ":" in value)
    ):
        raise RuntimeSupportError("email command publication secret is invalid")
    return value


def _publication_secret_provider() -> MountedFileSecretProvider:
    return MountedFileSecretProvider(
        _SECRET_ROOT,
        tuple(
            SecretSpec(name, name, "text", 16, 128)
            for name in (_FRAPPE_KEY, _FRAPPE_SECRET, _GATEWAY_BEARER)
        ),
    )


def _run_once(worker: CommandPublicationRelayWorker) -> None:
    worker.run_once()


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CommandPublicationRelayWorker",
    "FrappeCommandPublicationClient",
    "GatewayCommandIngestClient",
    "PublicationRelayResult",
    "PublicationRelayStatus",
    "main",
]
