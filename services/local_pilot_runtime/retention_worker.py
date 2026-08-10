"""Default-off, dry-run-first local retention worker entrypoint."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from services.model_gateway.tokenization import EncryptedFileMappingVault
from services.observer.observer.evidence_store import ContentAddressedEvidenceStore
from services.observer.observer.models import TenantScope
from services.observer.observer.retention import (
    PostgresRetentionStorage,
    RetentionError,
    RetentionResult,
    RetentionService,
)

from .projection_config import ProjectionConfigError, load_projection_config
from .runtime_support import (
    RuntimeSupportError,
    close_connection,
    connect_postgres,
    load_runtime_config,
    reject_plaintext_secret_environment,
)

DEFAULT_RUNTIME_CONFIG = Path("/config/local-pilot-runtime.json")
DEFAULT_PROJECTION_CONFIG = Path("/config/local-pilot-projection.json")
DEFAULT_MAPPING_VAULT_KEY_FILE = Path("/run/secrets/mapping_vault_key")


class RetentionRunner(Protocol):
    def __call__(
        self,
        *,
        site_id: str,
        worker_id: str,
        batch_size: int,
        dry_run: bool,
        now: datetime,
    ) -> object: ...


def main(
    *,
    runtime_config_path: Path = DEFAULT_RUNTIME_CONFIG,
    projection_config_path: Path = DEFAULT_PROJECTION_CONFIG,
    mapping_vault_key_file: Path = DEFAULT_MAPPING_VAULT_KEY_FILE,
    environ: Mapping[str, str] | None = None,
    runner: RetentionRunner | None = None,
    connector: Callable[..., object] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> int:
    """Run one bounded pass; mutation needs two exact environment opt-ins."""

    environment = os.environ if environ is None else environ
    try:
        reject_plaintext_secret_environment(environment)
        if environment.get("GBOS_RETENTION_ENABLED") != "true":
            raise RuntimeSupportError("retention worker is disabled")
        dry_run_value = environment.get("GBOS_RETENTION_DRY_RUN", "true")
        if dry_run_value not in {"true", "false"}:
            raise RuntimeSupportError("retention dry-run flag is invalid")
        dry_run = dry_run_value == "true"
        batch_size = _batch_size(environment.get("GBOS_RETENTION_BATCH_SIZE", "100"))
        runtime = load_runtime_config(runtime_config_path)
        now = _aware_utc((clock or _utc_now)())
        if runner is not None:
            runner(
                site_id=runtime.site_id,
                worker_id=runtime.worker.worker_id,
                batch_size=batch_size,
                dry_run=dry_run,
                now=now,
            )
        else:
            _run_default(
                runtime_site_id=runtime.site_id,
                worker_id=runtime.worker.worker_id,
                projection_config_path=projection_config_path,
                mapping_vault_key_file=mapping_vault_key_file,
                batch_size=batch_size,
                dry_run=dry_run,
                now=now,
                connector=connector,
            )
        return 0
    except (
        OSError,
        ProjectionConfigError,
        RetentionError,
        RuntimeSupportError,
        ValueError,
    ):
        return 78


def _run_default(
    *,
    runtime_site_id: str,
    worker_id: str,
    projection_config_path: Path,
    mapping_vault_key_file: Path,
    batch_size: int,
    dry_run: bool,
    now: datetime,
    connector: Callable[..., object] | None,
) -> RetentionResult:
    projection = load_projection_config(
        projection_config_path,
        expected_site_id=runtime_site_id,
    )
    connection: object | None = None
    try:
        connection = connect_postgres(
            projection.connections["observer"],
            connector=connector,
        )
        cas = ContentAddressedEvidenceStore(projection.evidence_cas_root)
        vault = EncryptedFileMappingVault.from_key_file(
            root=projection.tokenizer_vault_root,
            key_file=mapping_vault_key_file,
            clock=lambda: now,
        )
        service = RetentionService(
            storage=PostgresRetentionStorage(connection),  # type: ignore[arg-type]
            cas=cas,
            vault=vault,
            worker_id=worker_id,
            clock=lambda: now,
            lease_duration=timedelta(minutes=5),
        )
        return service.run(
            TenantScope(runtime_site_id, "audit_compliance"),
            batch_size=batch_size,
            dry_run=dry_run,
        )
    finally:
        if connection is not None:
            close_connection(connection)


def _batch_size(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeSupportError("retention batch size is invalid") from exc
    if str(parsed) != value or not 1 <= parsed <= 1_000:
        raise RuntimeSupportError("retention batch size is invalid")
    return parsed


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeSupportError("retention clock must be timezone-aware")
    return value.astimezone(UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
