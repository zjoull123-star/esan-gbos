"""Machine-derived repository, image, and running-container attestations."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_SOURCE_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RUNTIME_SOURCE_PATHS = (
    "contracts",
    "services",
    "pyproject.toml",
    "uv.lock",
    "infra/local/Containerfile.runtime",
    "scripts/local-pilot/build-runtime-image",
)
_FRAPPE_SOURCE_PATHS = (
    "apps/esan_gbos",
    "fixtures/gate1",
    "infra/dev/nginx",
    "infra/dev/realtime-runtime",
    "infra/dev/Containerfile.final",
    "infra/dev/apps.upstream.json",
    "scripts/dev/build-custom-image",
)
_SOURCE_GROUPS = {
    "local-runtime": _RUNTIME_SOURCE_PATHS,
    "frappe-pwa": _FRAPPE_SOURCE_PATHS,
}
_SOURCE_LABELS = {
    "local-runtime": "com.esan.gbos.runtime-source-sha256",
    "frappe-pwa": "com.esan.gbos.app-source-sha256",
}


def _git(
    repo_root: Path,
    arguments: Sequence[str],
    *,
    binary: bool = False,
) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=not binary,
    )


def _source_digest(repo_root: Path, paths: Sequence[str]) -> str:
    tracked = _git(repo_root, ["ls-files", "-z", "--", *paths], binary=True)
    if tracked.returncode != 0:
        raise ValueError("repository source inventory is unavailable")
    names = sorted(name for name in tracked.stdout.split(b"\0") if name)
    digest = hashlib.sha256()
    for raw_name in names:
        relative = raw_name.decode("utf-8")
        path = repo_root / relative
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise ValueError("repository source input is unavailable") from exc
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
    return digest.hexdigest()


def repository_attestation(repo_root: Path) -> dict[str, Any]:
    """Capture HEAD plus full and build-input-specific dirty/source state."""

    root = repo_root.resolve(strict=True)
    head_result = _git(root, ["rev-parse", "--verify", "HEAD"])
    head = head_result.stdout.strip()
    if head_result.returncode != 0 or _COMMIT.fullmatch(head) is None:
        raise ValueError("repository HEAD is unavailable")
    dirty_result = _git(root, ["status", "--porcelain=v1", "--untracked-files=all"])
    if dirty_result.returncode != 0:
        raise ValueError("repository dirty state is unavailable")
    source_groups: dict[str, dict[str, Any]] = {}
    for service, paths in _SOURCE_GROUPS.items():
        status = _git(
            root,
            ["status", "--porcelain=v1", "--untracked-files=all", "--", *paths],
        )
        if status.returncode != 0:
            raise ValueError(f"{service} source dirty state is unavailable")
        source_groups[service] = {
            "dirty": bool(status.stdout.strip()),
            "sha256": _source_digest(root, paths),
        }
    return {
        "head": head,
        "dirty": bool(dirty_result.stdout.strip()),
        "source_groups": source_groups,
    }


def inspect_image(reference: str) -> Mapping[str, Any]:
    result = subprocess.run(
        ["docker", "image", "inspect", reference],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(f"image inspection is unavailable: {reference}")
    try:
        decoded = json.loads(result.stdout)
        item = decoded[0]
    except (IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"image inspection is invalid: {reference}") from exc
    if not isinstance(item, Mapping):
        raise ValueError(f"image inspection is invalid: {reference}")
    return item


def _revision_matches_source(repo_root: Path, revision: str, paths: Sequence[str]) -> bool:
    ancestor = _git(repo_root, ["merge-base", "--is-ancestor", revision, "HEAD"])
    if ancestor.returncode != 0:
        return False
    difference = _git(repo_root, ["diff", "--quiet", revision, "--", *paths])
    return difference.returncode == 0


def attest_required_images(
    repo_root: Path,
    image_lock: Mapping[str, Any],
    required_services: set[str],
    *,
    repository: Mapping[str, Any] | None = None,
    inspector: Callable[[str], Mapping[str, Any]] = inspect_image,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Inspect required images and bind local builds to clean current source."""

    repo_state = dict(repository or repository_attestation(repo_root))
    values = image_lock.get("images")
    locked = (
        {
            item.get("service"): item
            for item in values
            if isinstance(values, list) and isinstance(item, Mapping)
        }
        if isinstance(values, list)
        else {}
    )
    attestations: list[dict[str, Any]] = []
    issues: list[str] = []
    if repo_state.get("dirty") is not False:
        issues.append("repository worktree is dirty")
    for service in sorted(required_services):
        item = locked.get(service)
        if not isinstance(item, Mapping):
            issues.append(f"required image {service} is absent from lock")
            continue
        reference = item.get("reference")
        if not isinstance(reference, str):
            issues.append(f"required image {service} reference is invalid")
            continue
        try:
            inspected = inspector(reference)
        except (OSError, ValueError) as exc:
            issues.append(str(exc))
            continue
        image_id = inspected.get("Id")
        repo_digests = inspected.get("RepoDigests") or []
        actual_platform = f"{inspected.get('Os')}/{inspected.get('Architecture')}"
        expected_id = item.get("local_inspect_digest")
        expected_repo_digest = item.get("local_repo_digest")
        expected_platform = item.get("platform")
        identity_verified = (
            isinstance(image_id, str)
            and _IMAGE_ID.fullmatch(image_id) is not None
            and image_id == expected_id
            and actual_platform == expected_platform
            and (
                item.get("source") != "remote"
                or isinstance(repo_digests, list)
                and expected_repo_digest in repo_digests
            )
        )
        if not identity_verified:
            issues.append(f"required image {service} actual identity does not match lock")

        config = inspected.get("Config")
        labels = config.get("Labels") if isinstance(config, Mapping) else None
        labels = labels if isinstance(labels, Mapping) else {}
        revision = labels.get("org.opencontainers.image.revision")
        source_sha256: object = None
        source_verified = item.get("source") != "local-build"
        if service in _SOURCE_GROUPS:
            source_group = repo_state.get("source_groups")
            source = source_group.get(service) if isinstance(source_group, Mapping) else None
            expected_source = source.get("sha256") if isinstance(source, Mapping) else None
            source_dirty = source.get("dirty") if isinstance(source, Mapping) else None
            source_sha256 = labels.get(_SOURCE_LABELS[service])
            revision_matches = (
                isinstance(revision, str)
                and _COMMIT.fullmatch(revision) is not None
                and _revision_matches_source(repo_root, revision, _SOURCE_GROUPS[service])
            )
            digest_matches = (
                isinstance(source_sha256, str)
                and _SOURCE_SHA256.fullmatch(source_sha256) is not None
                and source_sha256 == expected_source
            )
            if source_dirty is True:
                issues.append(f"{service} source inputs are dirty")
            if not revision_matches:
                issues.append(f"{service} revision label does not bind current source")
            if not digest_matches:
                issues.append(f"{service} source digest label does not bind current source")
            source_verified = source_dirty is False and revision_matches and digest_matches
        attestations.append(
            {
                "service": service,
                "reference": reference,
                "actual_inspect_digest": image_id,
                "actual_repo_digests": repo_digests,
                "platform": actual_platform,
                "revision": revision,
                "source_sha256": source_sha256,
                "identity_verified": identity_verified,
                "source_verified": source_verified,
            }
        )
    return attestations, issues


def image_service_for_runtime_service(service: str) -> str:
    if service in {"redis-cache", "redis-queue"}:
        return "redis"
    if service.startswith("frappe-") or service == "pwa":
        return "frappe-pwa"
    if service in {"postgres", "mariadb", "prometheus", "cloudflared"}:
        return service
    return "local-runtime"


def inspect_container(container_id: str) -> Mapping[str, Any]:
    result = subprocess.run(
        ["docker", "inspect", container_id],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(f"running container inspection is unavailable: {container_id}")
    try:
        decoded = json.loads(result.stdout)
        item = decoded[0]
    except (IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"running container inspection is invalid: {container_id}") from exc
    if not isinstance(item, Mapping):
        raise ValueError(f"running container inspection is invalid: {container_id}")
    return item


def attest_running_services(
    required_services: set[str],
    rows: Mapping[str, Mapping[str, Any]],
    image_attestations: Sequence[Mapping[str, Any]],
    *,
    inspector: Callable[[str], Mapping[str, Any]] = inspect_container,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Bind each required running container to an already-attested image ID."""

    images = {
        item.get("service"): item
        for item in image_attestations
        if isinstance(item.get("service"), str)
    }
    bindings: list[dict[str, Any]] = []
    issues: list[str] = []
    for service in sorted(required_services):
        row = rows.get(service)
        image_service = image_service_for_runtime_service(service)
        image = images.get(image_service)
        container_id = row.get("ID") if isinstance(row, Mapping) else None
        verified = False
        actual_image_id: object = None
        if not isinstance(container_id, str) or not container_id:
            issues.append(f"running service {service} container identity is unavailable")
        elif not isinstance(image, Mapping):
            issues.append(f"running service {service} required image is unattested")
        else:
            try:
                inspected = inspector(container_id)
            except (OSError, ValueError) as exc:
                issues.append(str(exc))
            else:
                actual_image_id = inspected.get("Image")
                verified = (
                    image.get("identity_verified") is True
                    and image.get("source_verified") is True
                    and actual_image_id == image.get("actual_inspect_digest")
                )
                if not verified:
                    issues.append(f"running service {service} image identity mismatch")
        bindings.append(
            {
                "service": service,
                "container_id": container_id,
                "image_service": image_service,
                "actual_inspect_digest": actual_image_id,
                "verified": verified,
            }
        )
    return bindings, issues
