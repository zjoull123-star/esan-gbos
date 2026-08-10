#!/usr/bin/env python3
# ruff: noqa: UP017
"""Report privacy-safe local-pilot readiness and runtime state."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 1_048_576:
        raise ValueError(f"{label} is unavailable")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _running_services(*, compose_file: Path, project_name: str) -> tuple[set[str], str | None]:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--project-name",
            project_name,
            "-f",
            str(compose_file),
            "--profile",
            "*",
            "ps",
            "--format",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return set(), "compose_status_unavailable"
    output = result.stdout.strip()
    if not output:
        return set(), None
    try:
        decoded = json.loads(output)
        rows = decoded if isinstance(decoded, list) else [decoded]
    except json.JSONDecodeError:
        try:
            rows = [json.loads(line) for line in output.splitlines() if line.strip()]
        except json.JSONDecodeError:
            return set(), "compose_status_invalid"
    running: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            return set(), "compose_status_invalid"
        service = row.get("Service")
        state = row.get("State")
        if isinstance(service, str) and state == "running":
            running.add(service)
    return running, None


def _required_services(manifest: Mapping[str, Any]) -> set[str]:
    if manifest.get("local_pilot_go") is not True:
        return set()
    required = {"observer-api", "frappe-backend", "pwa", "prometheus"}
    channels = manifest.get("channels")
    if isinstance(channels, Mapping):
        enabled_channel = False
        for channel, service in (
            ("email", "email-poller"),
            ("wecom", "wecom-poller"),
            ("whatsapp", "webhook-ingress"),
            ("media", "media-worker"),
        ):
            value = channels.get(channel)
            if isinstance(value, Mapping) and value.get("enabled") is True:
                required.add(service)
                enabled_channel = True
        if enabled_channel:
            required.update({"connector-worker", "identity-resolution-worker"})
    deepseek = manifest.get("deepseek")
    if isinstance(deepseek, Mapping) and deepseek.get("enabled") is True:
        required.update({"model-projection-worker", "communication-draft-worker"})
    return required


def _emergency_state(runtime_dir: Path) -> dict[str, Any]:
    latch_path = runtime_dir / "EMERGENCY_STOP"
    receipt_path = runtime_dir / "containment-receipt.json"
    if not latch_path.is_file():
        return {"active": False, "containment_verified": False}
    verified = False
    try:
        latch = _object(latch_path, "emergency latch")
        receipt = _object(receipt_path, "containment receipt")
        verified = (
            receipt.get("verified") is True
            and receipt.get("running_services") == []
            and receipt.get("latch_id") == latch.get("latch_id")
        )
    except (OSError, ValueError, json.JSONDecodeError):  # fmt: skip
        verified = False
    return {"active": True, "containment_verified": verified}


def _source_commit(repo_root: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip()
    return value if result.returncode == 0 and len(value) == 40 else None


def _image_bindings(image_lock: Mapping[str, Any]) -> list[dict[str, Any]]:
    values = image_lock.get("images")
    if not isinstance(values, list):
        return []
    bindings: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, Mapping):
            continue
        service = item.get("service")
        reference = item.get("reference")
        digest = item.get("local_inspect_digest")
        if isinstance(service, str) and isinstance(reference, str):
            bindings.append(
                {
                    "service": service,
                    "reference": reference,
                    "local_inspect_digest": digest if isinstance(digest, str) else None,
                }
            )
    return sorted(bindings, key=lambda value: value["service"])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--entrypoints", required=True, type=Path)
    parser.add_argument("--image-lock", required=True, type=Path)
    parser.add_argument("--runtime-dir", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--compose-file", required=True, type=Path)
    parser.add_argument("--project-name", default="esan-gbos-local-pilot")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = _object(args.manifest, "manifest")
        entrypoints = _object(args.entrypoints, "entrypoints")
        image_lock = _object(args.image_lock, "image lock")
        composition = entrypoints.get("composition")
        composition_status = (
            composition.get("status") if isinstance(composition, Mapping) else "missing"
        )
        running, inspection_error = _running_services(
            compose_file=args.compose_file,
            project_name=args.project_name,
        )
        required = _required_services(manifest)
        missing = sorted(required - running)
        emergency = _emergency_state(args.runtime_dir)
        reasons: list[str] = []
        if manifest.get("local_pilot_go") is not True:
            reasons.append("manifest_disabled")
            verdict = "disabled"
        else:
            if composition_status != "composed":
                reasons.append("composition_not_verified")
            if inspection_error is not None:
                reasons.append(inspection_error)
            if missing:
                reasons.append("required_services_not_running")
            if emergency["active"]:
                reasons.append("emergency_stop_active")
            verdict = "running" if not reasons else "no_go"
        payload = {
            "schema_version": "1.0",
            "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source_commit": _source_commit(args.repo_root),
            "composition_status": composition_status,
            "manifest": {
                "sha256": _sha256(args.manifest),
                "site_id": manifest.get("site_id"),
                "local_pilot_go": manifest.get("local_pilot_go"),
                "status": manifest.get("local_pilot_status"),
            },
            "emergency_stop": emergency,
            "services": {
                "required": sorted(required),
                "running": sorted(running),
                "missing": missing,
                "inspection_error": inspection_error,
            },
            "images": _image_bindings(image_lock),
            "verdict": verdict,
            "no_go_reasons": reasons,
        }
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"STATUS ERROR: {exc}", file=sys.stderr)
        return 78
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"本地试点状态：{payload['verdict']}")
        print(f"组合状态：{payload['composition_status']}")
        print(f"运行服务：{len(payload['services']['running'])}")
        if payload["no_go_reasons"]:
            print("未通过原因：" + ", ".join(payload["no_go_reasons"]))
    return 0 if payload["verdict"] in {"disabled", "running"} else 3


if __name__ == "__main__":
    raise SystemExit(main())
