"""Import-safe Observer local API with explicit PostgreSQL composition."""

from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI

from services.agent_runtime.local_entrypoint import (
    LocalEntrypointDisabled,
    load_local_manifest,
    require_component_enabled,
)
from services.observer.observer.email_address_match import (
    EMAIL_ADDRESS_MATCH_PURPOSE,
    EmailAddressMatchService,
)
from services.observer.observer.email_draft_material import EmailDraftMaterialService
from services.observer.observer.email_draft_material_repository import (
    PostgresEmailDraftMaterialRepository,
)
from services.observer.observer.email_mailbox_identity import EmailMailboxIdentityService
from services.observer.observer.email_participant_authority import (
    EmailParticipantAuthorityResolver,
    PostgresEmailParticipantAuthorityRepository,
)
from services.observer.observer.evidence_store import ContentAddressedEvidenceStore
from services.observer.observer.identity_resolution_work import (
    PostgresIdentityResolutionWorkRepository,
)
from services.observer.observer.identity_tokens import HmacSha256IdentityTokenResolver
from services.observer.observer.local_pilot_api import LocalPilotAPIConfig
from services.observer.observer.local_pilot_storage import PostgresLocalPilotStorage
from services.observer.observer.models import TenantScope
from services.observer.observer.read_service import (
    EvidenceRevealService,
    PostgresEvidenceBindingResolver,
)
from services.observer.observer.runtime import (
    LocalPilotRuntimeGuard,
    PostgresLocalPilotRuntime,
    compose_postgres_local_pilot_runtime,
)

from .email_gateway_config import MAILBOX_PROJECTION_AUTH_REF
from .runtime_support import (
    RuntimeSupportError,
    SecretValue,
    close_connection,
    connect_postgres,
    load_runtime_config,
    load_secret_file,
    reject_plaintext_secret_environment,
    validate_manifest_binding,
)
from .secret_provider import MountedFileSecretProvider, SecretBytes, SecretSpec
from .server import ServerBindingError, run_server, validate_server_binding

DEFAULT_MANIFEST = Path("/config/local-pilot-manifest.json")
DEFAULT_RUNTIME_CONFIG = Path("/config/local-pilot-runtime.json")
DEFAULT_CURSOR_SECRET = Path("/run/secrets/cursor_hmac_key")
DEFAULT_MAILBOX_PROJECTION_BEARER = Path("/run/secrets/mailbox_projection_bearer")
DEFAULT_DRAFT_MATERIAL_BEARER = Path("/run/secrets/observer_email_draft_material_bearer")
DEFAULT_IDENTITY_HMAC_KEY = Path("/run/secrets/identity_hmac_key")
DEFAULT_EVIDENCE_CAS_ROOT = Path("/var/lib/gbos/evidence")
DEFAULT_OBSERVER_PORT = 8003
ServerRunner = Callable[..., None]
ADDRESS_MATCH_CALLER_REF = "frappe-identity-command"
_ADDRESS_MATCH_SIGNING_CONTEXT = b"gbos:observer:email-address-match:v1"
_MAX_ADDRESS_MATCH_MESSAGE_BYTES = 10_000_000


class PostgresEmailAddressMatchEvidenceReader:
    """Load one delivered publication's RFC 822 object without exposing its address."""

    def __init__(self, connection: object, store: ContentAddressedEvidenceStore) -> None:
        self._connection = cast(Any, connection)
        self._store = store

    def read_authorized(
        self,
        scope: TenantScope,
        evidence_ref: str,
        *,
        caller_ref: str,
        purpose: str,
    ) -> bytes:
        if (
            caller_ref != ADDRESS_MATCH_CALLER_REF
            or purpose != EMAIL_ADDRESS_MATCH_PURPOSE
            or scope.processing_purpose != EMAIL_ADDRESS_MATCH_PURPOSE
        ):
            raise PermissionError("email address match evidence authority rejected")
        with self._connection.transaction(), self._connection.cursor() as cursor:
            cursor.execute("SELECT set_config('app.site_id', %s, true)", (scope.site_id,))
            cursor.execute(
                """
                SELECT delivery.object_ref, delivery.exact_body_sha256,
                       delivery.byte_size, delivery.media_type
                  FROM observer.email_message_publication_outbox AS publication
                  JOIN observer.inbound_deliveries AS delivery
                    ON delivery.site_id = publication.site_id
                   AND delivery.connector = publication.connector
                   AND delivery.connector_instance_id = publication.connector_instance_id
                   AND delivery.delivery_id = publication.observer_delivery_ref
                 WHERE publication.site_id = %s
                   AND publication.payload->>'site_id' = %s
                   AND publication.relay_status = 'delivered'
                   AND %s IN (
                       SELECT jsonb_array_elements_text(
                           publication.payload->'evidence_refs'
                       )
                   )
                 ORDER BY publication.publication_id
                 LIMIT 2
                """,
                (scope.site_id, scope.site_id, evidence_ref),
            )
            row = cursor.fetchone()
            duplicate = cursor.fetchone()
        if row is None or duplicate is not None:
            raise LookupError("email address match evidence is unavailable")
        object_ref, expected_digest, byte_size, media_type = row
        if (
            not isinstance(object_ref, str)
            or not isinstance(expected_digest, str)
            or isinstance(byte_size, bool)
            or not isinstance(byte_size, int)
            or not 1 <= byte_size <= _MAX_ADDRESS_MATCH_MESSAGE_BYTES
            or str(media_type).casefold() != "message/rfc822"
        ):
            raise LookupError("email address match evidence is unavailable")
        content = self._store.read(scope, object_ref)
        if len(content) != byte_size or not hmac.compare_digest(
            hashlib.sha256(content).hexdigest(), expected_digest
        ):
            raise LookupError("email address match evidence is unavailable")
        return content

    def __repr__(self) -> str:
        return "PostgresEmailAddressMatchEvidenceReader(connection=<redacted>, store=<redacted>)"


def build_postgres_runtime(
    *,
    connection: object,
    bearer_token: SecretValue,
    auth_ref: str,
    cursor_secret: SecretValue,
    mailbox_projection_bearer_token: SecretValue | None = None,
    mailbox_projection_auth_ref: str | None = None,
    draft_material_bearer_token: SecretValue | None = None,
    draft_material_auth_ref: str | None = None,
    identity_resolver: HmacSha256IdentityTokenResolver | None = None,
    identity_hmac_key: bytes | None = None,
    evidence_cas_root: Path = DEFAULT_EVIDENCE_CAS_ROOT,
    bind_host: str,
    network_mode: str,
    enabled: bool = True,
    kill_switch: bool = False,
    clock: Callable[[], datetime] | None = None,
) -> PostgresLocalPilotRuntime:
    """Compose the real storage-backed API without starting its outbox worker."""

    api_config = LocalPilotAPIConfig(
        bind_host=bind_host,
        network_mode=network_mode,  # type: ignore[arg-type]
        bearer_token=bearer_token.reveal(),
        auth_ref=auth_ref,
        mailbox_projection_bearer_token=(
            mailbox_projection_bearer_token.reveal()
            if mailbox_projection_bearer_token is not None
            else None
        ),
        mailbox_projection_auth_ref=mailbox_projection_auth_ref,
        draft_material_bearer_token=(
            draft_material_bearer_token.reveal()
            if draft_material_bearer_token is not None
            else None
        ),
        draft_material_auth_ref=draft_material_auth_ref,
    )
    storage = PostgresLocalPilotStorage(connection)  # type: ignore[arg-type]
    active_clock = clock or _utc_now
    evidence_reveal = None
    email_draft_material_repository = None
    email_draft_material = None
    email_mailbox_identity = None
    email_address_match = None
    if mailbox_projection_bearer_token is not None:
        evidence_store = ContentAddressedEvidenceStore(evidence_cas_root)
        evidence_reveal = EvidenceRevealService(
            binding_resolver=PostgresEvidenceBindingResolver(cast(Any, connection)).resolve,
            content_loader=evidence_store.read,
            clock=active_clock,
        )
        if draft_material_bearer_token is not None:
            if identity_resolver is None or identity_hmac_key is None:
                raise ValueError("email Gateway identity resolver is unavailable")
            email_mailbox_identity = EmailMailboxIdentityService(
                identity_resolver=identity_resolver
            )
            email_draft_material_repository = PostgresEmailDraftMaterialRepository(connection)
            email_draft_material = EmailDraftMaterialService(
                store=evidence_store,
                repository=email_draft_material_repository,
                participant_resolver=EmailParticipantAuthorityResolver(
                    repository=PostgresEmailParticipantAuthorityRepository(cast(Any, connection)),
                    store=evidence_store,
                    identity_resolver=identity_resolver,
                ),
                clock=active_clock,
            )
            address_match_signing_key = hmac.new(
                identity_hmac_key,
                _ADDRESS_MATCH_SIGNING_CONTEXT,
                hashlib.sha256,
            ).digest()
            email_address_match = EmailAddressMatchService(
                evidence_reader=PostgresEmailAddressMatchEvidenceReader(
                    connection,
                    evidence_store,
                ),
                signing_key=address_match_signing_key,
                allowed_caller_ref=ADDRESS_MATCH_CALLER_REF,
                clock=active_clock,
            )
    return compose_postgres_local_pilot_runtime(
        connection=connection,
        storage=storage,
        api_config=api_config,
        cursor_secret=cursor_secret.reveal().encode("utf-8"),
        publisher=_reject_outbox_publication,
        clock=active_clock,
        outbox_worker_id="observer-api-outbox-disabled",
        enabled=enabled,
        kill_switch=kill_switch,
        identity_resolution_metrics=PostgresIdentityResolutionWorkRepository(
            connection  # type: ignore[arg-type]
        ),
        evidence_reveal=evidence_reveal,
        email_draft_material_repository=email_draft_material_repository,
        email_draft_material=email_draft_material,
        email_mailbox_identity=email_mailbox_identity,
        email_address_match=email_address_match,
    )


def main(
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    runtime_config_path: Path = DEFAULT_RUNTIME_CONFIG,
    environ: Mapping[str, str] | None = None,
    observer_bearer_file: Path | None = None,
    observer_auth_ref: str | None = None,
    cursor_secret_file: Path = DEFAULT_CURSOR_SECRET,
    mailbox_projection_bearer_file: Path = DEFAULT_MAILBOX_PROJECTION_BEARER,
    draft_material_bearer_file: Path = DEFAULT_DRAFT_MATERIAL_BEARER,
    identity_hmac_key_file: Path = DEFAULT_IDENTITY_HMAC_KEY,
    observer_port: int = DEFAULT_OBSERVER_PORT,
    internal_network: bool = False,
    connector: Callable[..., object] | None = None,
    server_runner: ServerRunner | None = None,
    clock: Callable[[], datetime] | None = None,
) -> int:
    environment = os.environ if environ is None else environ
    connection: object | None = None
    try:
        reject_plaintext_secret_environment(environment)
        manifest = load_local_manifest(manifest_path)
        require_component_enabled(
            manifest,
            component="observer-api",
            environ=environment,
        )
        config = load_runtime_config(runtime_config_path)
        validate_manifest_binding(manifest, config)

        unix_socket_value = environment.get("GBOS_LISTEN_UNIX_SOCKET")
        network_mode = (
            "unix_socket"
            if unix_socket_value is not None
            else "internal_network"
            if internal_network
            else "loopback"
        )
        unix_socket = validate_server_binding(
            host=config.listen.host,
            port=observer_port,
            unix_socket=unix_socket_value,
            network_mode=network_mode,
        )
        bind_host = str(unix_socket) if unix_socket is not None else config.listen.host

        bearer_path = observer_bearer_file or config.auth.agent_api_bearer_file
        auth_ref = observer_auth_ref or config.auth.context_auth_ref
        bearer_token = load_secret_file(bearer_path)
        cursor_secret = load_secret_file(cursor_secret_file)
        gateway = manifest.get("email_gateway")
        mailbox_projection_bearer = None
        mailbox_projection_auth_ref = None
        draft_material_bearer = None
        draft_material_auth_ref = None
        identity_resolver = None
        identity_hmac_key = None
        if isinstance(gateway, Mapping) and gateway.get("kill_switch") is False:
            mailbox_projection_bearer = load_secret_file(mailbox_projection_bearer_file)
            mailbox_projection_auth_ref = MAILBOX_PROJECTION_AUTH_REF
            draft_material_bearer = load_secret_file(draft_material_bearer_file)
            draft_material_auth_ref = "observer-email-draft-material-v1"
            identity_provider = MountedFileSecretProvider(
                identity_hmac_key_file.absolute().parent,
                (
                    SecretSpec(
                        "identity_hmac_key",
                        identity_hmac_key_file.name,
                        "bytes",
                        32,
                        32,
                        exact_bytes=32,
                    ),
                ),
            )
            identity_secret = identity_provider.read_bytes("identity_hmac_key")
            if not isinstance(identity_secret, SecretBytes):
                raise ValueError("email Gateway identity resolver is unavailable")
            identity_hmac_key = identity_secret.reveal()
            identity_resolver = HmacSha256IdentityTokenResolver(identity_hmac_key)
        connection = connect_postgres(config.postgres, connector=connector)
        if draft_material_bearer is not None:
            PostgresEmailDraftMaterialRepository(connection).preflight()
        runtime = build_postgres_runtime(
            connection=connection,
            bearer_token=bearer_token,
            auth_ref=auth_ref,
            cursor_secret=cursor_secret,
            mailbox_projection_bearer_token=mailbox_projection_bearer,
            mailbox_projection_auth_ref=mailbox_projection_auth_ref,
            draft_material_bearer_token=draft_material_bearer,
            draft_material_auth_ref=draft_material_auth_ref,
            identity_resolver=identity_resolver,
            identity_hmac_key=identity_hmac_key,
            bind_host=bind_host,
            network_mode=network_mode,
            clock=clock,
        )
        active_runner = server_runner or _run_server
        active_runner(
            runtime.app,
            host=config.listen.host,
            port=observer_port,
            unix_socket=unix_socket,
            network_mode=network_mode,
        )
        return 0
    except LocalEntrypointDisabled, RuntimeSupportError, ServerBindingError, ValueError:
        return 78
    finally:
        if connection is not None:
            close_connection(connection)


def _reject_outbox_publication(event: Any, event_id: str, idempotency_key: str) -> None:
    del event, event_id, idempotency_key
    raise RuntimeError("Observer API outbox publication is disabled")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _run_server(
    application: FastAPI,
    *,
    host: str,
    port: int,
    unix_socket: Path | None,
    network_mode: str,
) -> None:
    run_server(
        application,
        host=host,
        port=port,
        unix_socket=unix_socket,
        network_mode=network_mode,
    )


def _disabled_app() -> FastAPI:
    application = FastAPI(
        title="ESAN GBOS Observer Local Pilot",
        version="1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    guard = LocalPilotRuntimeGuard(enabled=False, kill_switch=True)

    @application.get("/health")
    def health() -> dict[str, object]:
        return {
            **guard.health(),
            "network_mode": "disabled",
            "authenticated_internal_api": False,
        }

    return application


app = _disabled_app()

if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "app",
    "build_postgres_runtime",
    "main",
]
