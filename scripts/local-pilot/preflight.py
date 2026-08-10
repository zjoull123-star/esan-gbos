#!/usr/bin/env python3
"""Fail-closed static and host preflight for the isolated local pilot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from canary_attestation import attest_required_images, repository_attestation
from jsonschema import Draft202012Validator, FormatChecker

SCHEMA_RELATIVE_PATH = Path("contracts/local_pilot/local-pilot-manifest-v1.0.schema.json")
ENTRYPOINTS_RELATIVE_PATH = Path("infra/local/runtime-entrypoints.json")
DEFAULT_MANIFEST_RELATIVE_PATH = Path("infra/local/local-pilot-manifest.json")
IMAGE_LOCK_RELATIVE_PATH = Path("infra/local/images.lock.json")
PLACEHOLDER_HASHES = frozenset({character * 64 for character in ("0", "1", "a", "f")})
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
REMOTE_REFERENCE_PATTERN = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
REPO_DIGEST_PATTERN = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
PLATFORM_PATTERN = re.compile(r"^linux/(?:arm64|amd64)$")
FROZEN_ARM64_BUILD_INPUTS = frozenset(
    {"python-runtime-base", "uv-builder", "local-runtime", "frappe-pwa"}
)
CONTAINERFILE_BUILD_INPUTS = {
    "PYTHON_BASE_IMAGE": "python-runtime-base",
    "UV_IMAGE": "uv-builder",
}


def _load_object(path: Path, label: str, issues: list[str]) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        issues.append(f"{label} is missing: {path}")
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        issues.append(f"{label} is unreadable: {exc}")
        return {}
    if not isinstance(payload, dict):
        issues.append(f"{label} must be a JSON object")
        return {}
    return payload


def _validate_schema(
    manifest: Mapping[str, Any],
    schema: Mapping[str, Any],
    issues: list[str],
) -> None:
    if not manifest or not schema:
        return
    try:
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        errors = sorted(validator.iter_errors(manifest), key=lambda error: list(error.path))
    except Exception as exc:
        issues.append(f"manifest schema could not be evaluated: {exc}")
        return
    for error in errors:
        location = ".".join(str(part) for part in error.absolute_path) or "$"
        issues.append(f"manifest schema violation at {location}: {error.message}")


def _placeholder_hash_issues(manifest: Mapping[str, Any]) -> list[str]:
    channels = manifest.get("channels")
    media = channels.get("media") if isinstance(channels, Mapping) else None
    if not isinstance(media, Mapping) or media.get("enabled") is not True:
        return []
    issues: list[str] = []
    for field in ("ffmpeg_sha256", "whisper_model_sha256"):
        value = media.get(field)
        if not isinstance(value, str) or value.lower() in PLACEHOLDER_HASHES:
            issues.append(f"media {field} is missing or uses a placeholder SHA-256")
    return issues


def _governance_issues(manifest: Mapping[str, Any], require_go: bool) -> list[str]:
    issues: list[str] = []
    if require_go and manifest.get("local_pilot_go") is not True:
        issues.append("local_pilot_go must be true before start")
    if manifest.get("production_go") is not False:
        issues.append("production_go must remain false")
    capabilities = manifest.get("capabilities")
    if not isinstance(capabilities, Mapping):
        issues.append("capabilities must be present")
    else:
        for capability in (
            "kingdee",
            "cloud_server",
            "cloud_business_storage",
            "external_send",
            "formal_business_commands",
        ):
            if capabilities.get(capability) is not False:
                issues.append(f"capability {capability} must remain false")
    return issues


def _required_entrypoints(
    manifest: Mapping[str, Any],
    configuration: Mapping[str, Any],
) -> dict[str, str]:
    required: dict[str, str] = {}
    always = configuration.get("required_always")
    if isinstance(always, Mapping):
        required.update((str(service), str(path)) for service, path in always.items())
    conditional = configuration.get("required_when_enabled")
    if not isinstance(conditional, Mapping):
        return required
    channels = manifest.get("channels")
    if isinstance(channels, Mapping):
        for channel in ("email", "wecom", "whatsapp", "media"):
            value = channels.get(channel)
            if isinstance(value, Mapping) and value.get("enabled") is True:
                entries = conditional.get(channel)
                if isinstance(entries, Mapping):
                    required.update((str(service), str(path)) for service, path in entries.items())
    deepseek = manifest.get("deepseek")
    if isinstance(deepseek, Mapping) and deepseek.get("enabled") is True:
        entries = conditional.get("deepseek")
        if isinstance(entries, Mapping):
            required.update((str(service), str(path)) for service, path in entries.items())
    return required


def _composition_issues(configuration: Mapping[str, Any]) -> list[str]:
    composition = configuration.get("composition")
    if not isinstance(composition, Mapping):
        return ["local pilot 未组合，不可启动: composition declaration is missing"]
    if (
        composition.get("status") != "composed"
        or composition.get("frappe_pwa") != "composed"
        or not isinstance(composition.get("runtime_containerfile"), str)
    ):
        return ["local pilot 未组合，不可启动: declared composition is not runtime verified"]
    return []


def _runtime_issues(
    repo_root: Path,
    manifest: Mapping[str, Any],
    configuration: Mapping[str, Any],
    *,
    synthetic: bool,
) -> list[str]:
    issues: list[str] = []
    if not synthetic:
        issues.extend(_composition_issues(configuration))
    declared_services = configuration.get("services")
    if not isinstance(declared_services, Mapping):
        issues.append("runtime service status declaration is missing")
        declared_services = {}
    if synthetic:
        for service, declaration in declared_services.items():
            if not isinstance(declaration, Mapping) or declaration.get("status") != "executable":
                continue
            relative_path = declaration.get("path")
            if not isinstance(relative_path, str) or not (repo_root / relative_path).is_file():
                issues.append(f"declared executable is unavailable for {service}: {relative_path}")
    else:
        for service in _required_entrypoints(manifest, configuration):
            declaration = declared_services.get(service)
            if not isinstance(declaration, Mapping) or declaration.get("status") != "executable":
                issues.append(f"runtime entrypoint remains blocked for {service}")
    for service, relative_path in _required_entrypoints(manifest, configuration).items():
        if synthetic:
            continue
        path = repo_root / relative_path
        if not path.is_file():
            issues.append(f"runtime entrypoint unavailable for {service}: {relative_path}")
    image = configuration.get("runtime_image")
    if not isinstance(image, str) or not image or image.endswith(":latest"):
        issues.append("runtime image must use an explicit non-latest tag")
    containerfile = repo_root / "infra/local/Containerfile.runtime"
    if not containerfile.is_file():
        issues.append("runtime Containerfile is unavailable")
    return issues


def _required_image_services(manifest: Mapping[str, Any]) -> set[str]:
    required = {
        "postgres",
        "mariadb",
        "redis",
        "frappe-pwa",
        "local-runtime",
        "python-runtime-base",
        "uv-builder",
    }
    channels = manifest.get("channels")
    if isinstance(channels, Mapping):
        whatsapp = channels.get("whatsapp")
        if isinstance(whatsapp, Mapping) and whatsapp.get("enabled") is True:
            required.add("cloudflared")
    return required


def _inspect_image(reference: str) -> tuple[str, tuple[str, ...], str] | None:
    result = subprocess.run(
        ["docker", "image", "inspect", reference],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
        item = payload[0]
        image_id = item["Id"]
        repo_digests = item.get("RepoDigests") or []
        platform = f"{item['Os']}/{item['Architecture']}"
    except (IndexError, KeyError, TypeError, json.JSONDecodeError):  # fmt: skip
        return None
    if not isinstance(image_id, str) or not isinstance(repo_digests, list):
        return None
    return (
        image_id,
        tuple(value for value in repo_digests if isinstance(value, str)),
        platform,
    )


def _image_issues(
    repo_root: Path,
    configuration: Mapping[str, Any],
    manifest: Mapping[str, Any],
    image_lock: Mapping[str, Any],
    *,
    skip_image_check: bool,
    verify_source_attestation: bool,
) -> list[str]:
    images = image_lock.get("images")
    if not isinstance(images, list) or not images:
        return ["image lock must contain at least one image"]
    issues: list[str] = []
    references: set[str] = set()
    normalized: dict[str, tuple[str, str | None, str | None, str | None]] = {}
    for item in images:
        if not isinstance(item, Mapping):
            issues.append("image lock entries must be objects")
            continue
        service = item.get("service")
        source = item.get("source")
        reference = item.get("reference")
        platform = item.get("platform")
        if (
            not isinstance(service, str)
            or source not in {"remote", "local-build"}
            or not isinstance(reference, str)
        ):
            issues.append("image lock entries require service, source, and reference")
            continue
        if service in normalized:
            issues.append(f"duplicate image lock service: {service}")
            continue
        if reference.endswith(":latest"):
            issues.append(f"image {service} uses forbidden latest tag")
        if not isinstance(platform, str) or not PLATFORM_PATTERN.fullmatch(platform):
            issues.append(f"image {service} platform is invalid")
        if service in FROZEN_ARM64_BUILD_INPUTS and platform != "linux/arm64":
            issues.append(f"image {service} platform must be linux/arm64")
        if source == "remote" and not REMOTE_REFERENCE_PATTERN.fullmatch(reference):
            issues.append(f"remote image {service} reference must include @sha256")
        references.add(reference)
        local_inspect_digest = item.get("local_inspect_digest")
        local_repo_digest = item.get("local_repo_digest")
        normalized[service] = (
            reference,
            local_inspect_digest if isinstance(local_inspect_digest, str) else None,
            local_repo_digest if isinstance(local_repo_digest, str) else None,
            platform if isinstance(platform, str) else None,
        )
    runtime_image = configuration.get("runtime_image")
    if isinstance(runtime_image, str) and runtime_image not in references:
        issues.append("runtime image is absent from the image lock")
    required_services = _required_image_services(manifest)
    for service in sorted(required_services):
        locked = normalized.get(service)
        if locked is None:
            issues.append(f"required image is absent from lock: {service}")
            continue
        _, expected_id, expected_repo_digest, _expected_platform = locked
        if expected_id is None:
            issues.append(f"image {service} local_inspect_digest is required")
        elif not SHA256_PATTERN.fullmatch(expected_id):
            issues.append(f"image {service} local_inspect_digest is invalid")
        source = next(
            (
                item.get("source")
                for item in images
                if isinstance(item, Mapping) and item.get("service") == service
            ),
            None,
        )
        if source == "remote":
            if expected_repo_digest is None:
                issues.append(f"image {service} local_repo_digest is required")
            elif not REPO_DIGEST_PATTERN.fullmatch(expected_repo_digest):
                issues.append(f"image {service} local_repo_digest is invalid")
        elif expected_repo_digest is not None:
            issues.append(f"local-build image {service} must not invent a RepoDigest")
    if skip_image_check:
        return issues
    if not shutil.which("docker"):
        issues.append("docker is unavailable for image inspection")
        return issues
    for service in sorted(required_services):
        locked = normalized.get(service)
        if locked is None:
            continue
        reference, expected_id, expected_repo_digest, expected_platform = locked
        inspected = _inspect_image(reference)
        if inspected is None:
            issues.append(f"required image {service} is unavailable locally: {reference}")
            continue
        actual_id, actual_repo_digests, actual_platform = inspected
        if expected_id is not None and actual_id != expected_id:
            issues.append(
                f"local inspect ID mismatch for {service}: locked {expected_id}, actual {actual_id}"
            )
        if expected_repo_digest is not None and expected_repo_digest not in actual_repo_digests:
            issues.append(
                f"local RepoDigest mismatch for {service}: "
                f"locked {expected_repo_digest}, actual {list(actual_repo_digests)}"
            )
        if expected_platform is not None and actual_platform != expected_platform:
            issues.append(
                f"local platform mismatch for {service}: "
                f"locked {expected_platform}, actual {actual_platform}"
            )
    if verify_source_attestation:
        try:
            repository = repository_attestation(repo_root)
            _attestations, attestation_issues = attest_required_images(
                repo_root,
                image_lock,
                required_services,
                repository=repository,
            )
        except ValueError as exc:
            issues.append(str(exc))
        else:
            issues.extend(attestation_issues)
    return issues


def _containerfile_lock_issues(
    repo_root: Path,
    image_lock: Mapping[str, Any],
) -> list[str]:
    containerfile = repo_root / "infra/local/Containerfile.runtime"
    try:
        lines = containerfile.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return [f"runtime Containerfile is unreadable: {exc}"]
    arguments: dict[str, str] = {}
    for line in lines:
        if not line.startswith("ARG ") or "=" not in line:
            continue
        name, value = line.removeprefix("ARG ").split("=", 1)
        arguments[name] = value
    images = image_lock.get("images")
    if not isinstance(images, list):
        return ["image lock entries are unavailable for Containerfile binding"]
    references = {
        item.get("service"): item.get("reference") for item in images if isinstance(item, Mapping)
    }
    issues: list[str] = []
    for argument, service in CONTAINERFILE_BUILD_INPUTS.items():
        if arguments.get(argument) != references.get(service):
            issues.append(f"Containerfile {argument} does not match image lock")
    return issues


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _media_host_issues(manifest: Mapping[str, Any]) -> list[str]:
    channels = manifest.get("channels")
    media = channels.get("media") if isinstance(channels, Mapping) else None
    if not isinstance(media, Mapping) or media.get("enabled") is not True:
        return []
    issues: list[str] = []
    model_dir_value = os.environ.get("GBOS_MEDIA_MODEL_DIR")
    if not model_dir_value:
        return ["GBOS_MEDIA_MODEL_DIR is required when media is enabled"]
    model_path = Path(model_dir_value) / "whisper-model.bin"
    if not model_path.is_file():
        issues.append(f"local whisper model is missing: {model_path}")
    else:
        expected = media.get("whisper_model_sha256")
        if _sha256(model_path) != expected:
            issues.append("local whisper model SHA-256 does not match the manifest")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        issues.append("ffmpeg is unavailable")
    elif _sha256(Path(ffmpeg)) != media.get("ffmpeg_sha256"):
        issues.append("local ffmpeg SHA-256 does not match the manifest")
    return issues


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--image-lock", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--require-go", action="store_true")
    mode.add_argument(
        "--synthetic",
        action="store_true",
        help="Validate disabled synthetic configuration without image or composition claims.",
    )
    parser.add_argument(
        "--require-runtime-images",
        action="store_true",
        help="With --synthetic, also require recorded and locally inspectable runtime images.",
    )
    parser.add_argument(
        "--skip-runtime-image-check",
        action="store_true",
        help="Static validation only; start never uses this option.",
    )
    return parser.parse_args(argv)


def _synthetic_issues(manifest: Mapping[str, Any], synthetic: bool) -> list[str]:
    if not synthetic:
        return []
    issues: list[str] = []
    if manifest.get("local_pilot_go") is not False:
        issues.append("synthetic preflight requires local_pilot_go=false")
    deepseek = manifest.get("deepseek")
    if not isinstance(deepseek, Mapping) or deepseek.get("enabled") is not False:
        issues.append("synthetic preflight requires DeepSeek disabled")
    channels = manifest.get("channels")
    if not isinstance(channels, Mapping) or any(
        not isinstance(item, Mapping) or item.get("enabled") is not False
        for item in channels.values()
    ):
        issues.append("synthetic preflight requires every channel disabled")
    return issues


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    bound_repo_root = Path(__file__).resolve().parents[2]
    requested_repo_root = args.repo_root.resolve() if args.repo_root else bound_repo_root
    repo_root = (
        bound_repo_root
        if args.require_go and requested_repo_root != bound_repo_root
        else requested_repo_root
    )
    manifest_path = (
        args.manifest.resolve() if args.manifest else repo_root / DEFAULT_MANIFEST_RELATIVE_PATH
    )
    image_lock_path = (
        args.image_lock.resolve() if args.image_lock else repo_root / IMAGE_LOCK_RELATIVE_PATH
    )
    issues: list[str] = []
    if args.require_go and requested_repo_root != bound_repo_root:
        issues.append("formal preflight repository root must match the bound repository")
    manifest = _load_object(manifest_path, "local-pilot manifest", issues)
    schema = _load_object(repo_root / SCHEMA_RELATIVE_PATH, "local-pilot schema", issues)
    configuration = _load_object(
        repo_root / ENTRYPOINTS_RELATIVE_PATH,
        "runtime entrypoint declaration",
        issues,
    )
    image_lock = _load_object(
        image_lock_path,
        "local image lock",
        issues,
    )

    issues.extend(_placeholder_hash_issues(manifest))
    _validate_schema(manifest, schema, issues)
    issues.extend(_governance_issues(manifest, args.require_go))
    issues.extend(_synthetic_issues(manifest, args.synthetic))
    issues.extend(
        _runtime_issues(
            repo_root,
            manifest,
            configuration,
            synthetic=args.synthetic,
        )
    )
    if not args.synthetic or args.require_runtime_images:
        issues.extend(_containerfile_lock_issues(repo_root, image_lock))
        issues.extend(
            _image_issues(
                repo_root,
                configuration,
                manifest,
                image_lock,
                skip_image_check=args.skip_runtime_image_check,
                verify_source_attestation=args.require_go and not args.synthetic,
            )
        )
    issues.extend(_media_host_issues(manifest))

    if issues:
        for issue in issues:
            print(f"PRECHECK FAILED: {issue}", file=sys.stderr)
        return 78
    print("Local-pilot preflight passed without enabling any capability.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
