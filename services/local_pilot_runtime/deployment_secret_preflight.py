"""Fail-closed deployment secret projection preflight."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Never, Protocol, cast

from jsonschema import Draft202012Validator

from services.local_pilot_runtime.secret_provider import (
    MountedFileSecretProvider,
    SecretBytes,
    SecretProviderError,
    SecretSpec,
    SecretText,
)

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[2]
SCHEMA_PATH: Final = (
    REPOSITORY_ROOT / "contracts" / "gate6" / "deployment-secret-projection-v1.0.schema.json"
)
DEFAULT_SECRET_ROOT: Final = Path("/run/secrets")
_MAX_METADATA_BYTES: Final = 256 * 1024

PREFLIGHT_OK: Final = "DEPLOYMENT_SECRET_PREFLIGHT_OK"
PREFLIGHT_ERROR_CONTRACT: Final = "DEPLOYMENT_SECRET_PREFLIGHT_CONTRACT_INVALID"
PREFLIGHT_ERROR_BINDING: Final = "DEPLOYMENT_SECRET_PREFLIGHT_BINDING_MISMATCH"
PREFLIGHT_ERROR_LOCAL_INPUT: Final = "DEPLOYMENT_SECRET_PREFLIGHT_LOCAL_INPUT_FORBIDDEN"
PREFLIGHT_ERROR_ROOT: Final = "DEPLOYMENT_SECRET_PREFLIGHT_SECRET_ROOT_FORBIDDEN"
PREFLIGHT_ERROR_MOUNT: Final = "DEPLOYMENT_SECRET_PREFLIGHT_MOUNT_INVALID"
PREFLIGHT_ERROR_RUNTIME: Final = "DEPLOYMENT_SECRET_PREFLIGHT_RUNTIME_UNAVAILABLE"

_FORBIDDEN_METADATA_KEYS: Final = frozenset(
    {
        "api_key",
        "credential",
        "credentials",
        "keychain_ref",
        "password",
        "private_key",
        "provider_payload",
        "secret_hash",
        "secret_uri",
        "secret_value",
        "sha256",
        "token",
        "tool",
        "uri",
        "value",
    }
)
_LOCAL_ONLY_MARKERS: Final = (
    "keychain://",
    "/usr/bin/security",
    "security find-generic-password",
    "find-generic-password",
)


class DeploymentSecretPreflightError(RuntimeError):
    """A stable, value-free deployment preflight failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _StableArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> Never:
        _reject(PREFLIGHT_ERROR_CONTRACT)


class _SecretProvider(Protocol):
    def read_text(self, name: str) -> SecretText | None: ...

    def read_bytes(self, name: str) -> SecretBytes | None: ...

    def read_json_bytes(self, name: str) -> SecretBytes | None: ...


ProviderFactory = Callable[[Path, Sequence[SecretSpec]], _SecretProvider]


def _reject(code: str) -> Never:
    raise DeploymentSecretPreflightError(code)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _reject(PREFLIGHT_ERROR_CONTRACT)
        result[key] = value
    return result


def _load_object(path: Path) -> dict[str, Any]:
    try:
        details = path.lstat()
        if path.is_symlink() or not 0 < details.st_size <= _MAX_METADATA_BYTES:
            _reject(PREFLIGHT_ERROR_CONTRACT)
        value: object = json.loads(path.read_bytes(), object_pairs_hook=_unique_object)
    except DeploymentSecretPreflightError:
        raise
    except OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError:
        _reject(PREFLIGHT_ERROR_CONTRACT)
    if not isinstance(value, dict):
        _reject(PREFLIGHT_ERROR_CONTRACT)
    return cast(dict[str, Any], value)


def _contains_local_only_input(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str) or key.lower() in _FORBIDDEN_METADATA_KEYS:
                return True
            if _contains_local_only_input(child):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_local_only_input(item) for item in value)
    if isinstance(value, str):
        lowered = value.lower()
        return any(marker in lowered for marker in _LOCAL_ONLY_MARKERS)
    return False


def _is_plaintext_secret_name(name: str) -> bool:
    normalized = name.upper()
    return (
        "PASSWORD" in normalized
        or "SECRET" in normalized
        or normalized.endswith("TOKEN")
        or normalized.endswith("BEARER")
        or normalized.endswith("API_KEY")
        or "PROVIDER_PAYLOAD" in normalized
    )


def _is_mounted_secret_file_reference(value: str) -> bool:
    try:
        path = Path(value)
        if str(path) != value or path.parent != DEFAULT_SECRET_ROOT:
            return False
        SecretSpec(
            name="environment-file-reference",
            filename=path.name,
            kind="bytes",
            minimum_bytes=1,
            maximum_bytes=1,
        )
    except SecretProviderError, TypeError, ValueError:
        return False
    return True


def _reject_local_environment(environ: Mapping[str, str]) -> None:
    for name, value in environ.items():
        if not isinstance(name, str) or not isinstance(value, str):
            _reject(PREFLIGHT_ERROR_LOCAL_INPUT)
        normalized = name.upper()
        lowered = value.lower()
        if value and "PROVIDER_PAYLOAD" in normalized:
            _reject(PREFLIGHT_ERROR_LOCAL_INPUT)
        if value and normalized.endswith("_FILE") and _is_plaintext_secret_name(normalized[:-5]):
            if _is_mounted_secret_file_reference(value):
                continue
            _reject(PREFLIGHT_ERROR_LOCAL_INPUT)
        if value and (
            _is_plaintext_secret_name(normalized)
            or any(marker in lowered for marker in _LOCAL_ONLY_MARKERS)
        ):
            _reject(PREFLIGHT_ERROR_LOCAL_INPUT)


def _validate_contract(contract: dict[str, Any]) -> None:
    schema = _load_object(SCHEMA_PATH)
    try:
        Draft202012Validator.check_schema(schema)
        if next(Draft202012Validator(schema).iter_errors(contract), None) is not None:
            _reject(PREFLIGHT_ERROR_CONTRACT)
    except DeploymentSecretPreflightError:
        raise
    except Exception:
        _reject(PREFLIGHT_ERROR_CONTRACT)


def _validate_root(secret_root: Path) -> Path:
    if not secret_root.is_absolute():
        _reject(PREFLIGHT_ERROR_ROOT)
    try:
        resolved = secret_root.resolve(strict=False)
    except OSError:
        _reject(PREFLIGHT_ERROR_ROOT)
    if resolved == REPOSITORY_ROOT or resolved.is_relative_to(REPOSITORY_ROOT):
        _reject(PREFLIGHT_ERROR_ROOT)
    return resolved


def _specs(contract: Mapping[str, Any]) -> tuple[SecretSpec, ...]:
    projections: object = contract.get("projections")
    if not isinstance(projections, list):
        _reject(PREFLIGHT_ERROR_CONTRACT)
    specs: list[SecretSpec] = []
    try:
        for projection in projections:
            if not isinstance(projection, Mapping):
                _reject(PREFLIGHT_ERROR_CONTRACT)
            logical_name = projection["logical_name"]
            target = Path(projection["target_filename"])
            if not isinstance(logical_name, str) or target != DEFAULT_SECRET_ROOT / logical_name:
                _reject(PREFLIGHT_ERROR_CONTRACT)
            specs.append(
                SecretSpec(
                    name=logical_name,
                    filename=target.name,
                    kind=projection["kind"],
                    minimum_bytes=projection["minimum_bytes"],
                    maximum_bytes=projection["maximum_bytes"],
                    exact_bytes=projection.get("exact_bytes"),
                    required=projection["required"],
                )
            )
    except DeploymentSecretPreflightError:
        raise
    except KeyError, TypeError, ValueError, SecretProviderError:
        _reject(PREFLIGHT_ERROR_CONTRACT)
    return tuple(specs)


def _read_and_prove(provider: _SecretProvider, spec: SecretSpec) -> None:
    if spec.kind == "text":
        text_value = provider.read_text(spec.name)
        if text_value is None:
            if spec.required:
                _reject(PREFLIGHT_ERROR_MOUNT)
            return
        revealed_bytes = len(text_value.reveal())
    elif spec.kind == "bytes":
        bytes_value = provider.read_bytes(spec.name)
        if bytes_value is None:
            if spec.required:
                _reject(PREFLIGHT_ERROR_MOUNT)
            return
        revealed_bytes = len(bytes_value.reveal())
    else:
        json_value = provider.read_json_bytes(spec.name)
        if json_value is None:
            if spec.required:
                _reject(PREFLIGHT_ERROR_MOUNT)
            return
        revealed_bytes = len(json_value.reveal())
    if revealed_bytes < 1:
        _reject(PREFLIGHT_ERROR_MOUNT)


def run_deployment_secret_preflight(
    contract_path: Path,
    *,
    site_id: str,
    environment: str,
    secret_root: Path = DEFAULT_SECRET_ROOT,
    environ: Mapping[str, str] | None = None,
    provider_factory: ProviderFactory = MountedFileSecretProvider,
) -> str:
    """Validate one bound metadata contract and every projected mount."""

    active_environment = os.environ if environ is None else environ
    _reject_local_environment(active_environment)
    contract = _load_object(Path(contract_path))
    if _contains_local_only_input(contract):
        _reject(PREFLIGHT_ERROR_LOCAL_INPUT)
    _validate_contract(contract)
    if contract.get("site_id") != site_id or contract.get("environment") != environment:
        _reject(PREFLIGHT_ERROR_BINDING)
    root = _validate_root(Path(secret_root))
    specs = _specs(contract)
    try:
        provider = provider_factory(root, specs)
        for spec in specs:
            _read_and_prove(provider, spec)
    except DeploymentSecretPreflightError:
        raise
    except Exception:
        _reject(PREFLIGHT_ERROR_MOUNT)
    return PREFLIGHT_OK


def preflight_then_start[Started](
    startup_factory: Callable[[], Started],
    *,
    contract_path: Path,
    site_id: str,
    environment: str,
    secret_root: Path = DEFAULT_SECRET_ROOT,
    environ: Mapping[str, str] | None = None,
    provider_factory: ProviderFactory = MountedFileSecretProvider,
) -> Started:
    """Invoke a runtime factory only after the deployment preflight succeeds."""

    result = run_deployment_secret_preflight(
        contract_path,
        site_id=site_id,
        environment=environment,
        secret_root=secret_root,
        environ=environ,
        provider_factory=provider_factory,
    )
    if result != PREFLIGHT_OK:
        _reject(PREFLIGHT_ERROR_MOUNT)
    return startup_factory()


def _parser() -> argparse.ArgumentParser:
    parser = _StableArgumentParser(description="Validate mounted deployment secrets.")
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--site-id", required=True)
    parser.add_argument("--environment", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        result = run_deployment_secret_preflight(
            arguments.contract,
            site_id=arguments.site_id,
            environment=arguments.environment,
        )
    except DeploymentSecretPreflightError as error:
        print(error.code, file=sys.stderr)
        return 78
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
