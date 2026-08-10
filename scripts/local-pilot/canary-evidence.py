#!/usr/bin/env python3
"""Maintain a private, content-free Task 13 canary evidence ledger."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from services.local_pilot_runtime.canary_chain_verifier import (  # noqa: E402
    CanaryChainVerificationError,
    validate_canary_chain_attestation,
)

_HEX = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ZERO_HASH = "0" * 64
_DEFERRED_STABILITY = {
    "continuous_runtime_required": False,
    "seventy_two_hour_run": "deferred_by_user",
}
_REQUIRED_CHECK_SOURCES = {
    "email_body_peek_no_backfill": "system_query",
    "user_mapping_reviewed": "browser_capture",
    "party_mapping_reviewed": "browser_capture",
    "user_second_message_auto_resolved": "system_query",
    "party_second_message_auto_resolved": "system_query",
    "model_identity_exact": "system_query",
    "model_input_tokenized": "system_query",
    "ai_draft_review_visible": "browser_capture",
    "budget_limits_verified": "system_query",
    "retention_verified": "controlled_drill",
    "emergency_stop_verified": "controlled_drill",
    "fault_drills_verified": "controlled_drill",
    "zero_prohibited_actions": "system_query",
}
_LIVE_CHECK_BASE_FIELDS = frozenset(
    {
        "observed_at",
        "run_id",
        "source_commit",
        "manifest_sha256",
        "kind",
        "status",
        "source",
        "evidence_sha256",
        "record_type",
        "sequence",
        "previous_sha256",
        "record_sha256",
    }
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    sample = commands.add_parser("sample")
    sample.add_argument("--canary-dir", required=True, type=Path)
    sample.add_argument("--status-json", required=True, type=Path)
    record = commands.add_parser("record")
    record.add_argument("--canary-dir", required=True, type=Path)
    record.add_argument("--kind", required=True, choices=sorted(_REQUIRED_CHECK_SOURCES))
    record.add_argument(
        "--source",
        required=True,
        choices=("system_query", "browser_capture", "controlled_drill"),
    )
    record.add_argument("--observed-at")
    record.add_argument("--evidence-file", type=Path)
    record.add_argument("--chain-attestation", type=Path)
    finalize = commands.add_parser("finalize")
    finalize.add_argument("--canary-dir", required=True, type=Path)
    return parser


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("hashed artifact must be a regular file")
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError(f"{label} timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} timestamp is invalid")
    return parsed.astimezone(UTC)


def _private_file(path: Path, label: str, *, maximum: int = 10 * 1024 * 1024) -> Path:
    candidate, _raw = _private_bytes(path, label, maximum=maximum)
    return candidate


def _object(path: Path, label: str) -> dict[str, Any]:
    _candidate, value, _digest = _object_with_sha256(path, label)
    return value


def _object_with_sha256(
    path: Path,
    label: str,
    *,
    maximum: int = 1024 * 1024,
) -> tuple[Path, dict[str, Any], str]:
    candidate, raw = _private_bytes(path, label, maximum=maximum)
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} is invalid")
    return candidate, value, hashlib.sha256(raw).hexdigest()


def _private_bytes(path: Path, label: str, *, maximum: int) -> tuple[Path, bytes]:
    try:
        candidate = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{label} must be a private regular file") from exc
    if path.is_symlink() or candidate.is_symlink():
        raise ValueError(f"{label} must be a private regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise ValueError(f"{label} must be a private regular file") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError(f"{label} must be a private regular file")
        if metadata.st_size < 1 or metadata.st_size > maximum:
            raise ValueError(f"{label} size is invalid")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise ValueError(f"{label} changed while being read")
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if os.fstat(descriptor).st_size != len(raw):
            raise ValueError(f"{label} changed while being read")
        return candidate, raw
    finally:
        os.close(descriptor)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _status_attestation_issues(status: Mapping[str, Any]) -> list[str]:
    """Require machine bindings from current status while retaining schema 1.0 history."""

    if status.get("schema_version") != "1.1":
        return []
    attestation = status.get("runtime_attestation")
    if not isinstance(attestation, Mapping):
        return ["status runtime attestation is unavailable"]
    checks = (
        ("repository_source_verified", "status repository source attestation is unbound"),
        ("required_images_verified", "status required image attestation is unbound"),
        ("running_images_verified", "status running image attestation is unbound"),
    )
    return [message for field, message in checks if attestation.get(field) is not True]


def _canary_context(canary_dir: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    repo_root = Path(__file__).resolve().parents[2]
    candidate = canary_dir.resolve(strict=True)
    if canary_dir.is_symlink() or not candidate.is_dir():
        raise ValueError("canary directory must be a private real directory")
    if stat.S_IMODE(candidate.stat().st_mode) != 0o700:
        raise ValueError("canary directory permissions must be exactly 0700")
    if os.path.commonpath((str(candidate), str(repo_root))) == str(repo_root):
        raise ValueError("canary directory must be outside the repository")
    manifest_path = candidate / "pilot-manifest.json"
    control_path = candidate / "canary-run.json"
    _manifest_file, manifest, manifest_sha256 = _object_with_sha256(
        manifest_path,
        "canary manifest",
    )
    control = _object(control_path, "canary control")
    required_control = {
        "schema_version",
        "run_id",
        "state",
        "source_commit",
        "manifest_sha256",
        "activation_time",
        "stability_assessment",
        "scope",
    }
    if not required_control.issubset(control):
        raise ValueError("canary control is incomplete")
    if (
        control.get("schema_version") != "1.1"
        or control.get("state") != "prepared"
        or control.get("stability_assessment") != _DEFERRED_STABILITY
        or "pilot_window_hours" in control
        or _COMMIT.fullmatch(str(control.get("source_commit"))) is None
        or _HEX.fullmatch(str(control.get("manifest_sha256"))) is None
        or control.get("manifest_sha256") == _ZERO_HASH
        or manifest_sha256 != control.get("manifest_sha256")
    ):
        raise ValueError("canary control binding is invalid")
    scope = control.get("scope")
    if scope != {
        "channels": ["email"],
        "model": "deepseek-v4-flash",
        "external_send": False,
        "formal_commands": False,
    }:
        raise ValueError("canary scope is invalid")
    deepseek = manifest.get("deepseek")
    if (
        manifest.get("local_pilot_go") is not True
        or manifest.get("production_go") is not False
        or not isinstance(deepseek, Mapping)
        or deepseek.get("enabled") is not True
        or deepseek.get("model") != "deepseek-v4-flash"
    ):
        raise ValueError("canary manifest is invalid")
    return candidate, manifest, control


def _load_ledger(path: Path, record_type: str, *, required: bool) -> list[dict[str, Any]]:
    if not path.exists():
        if required:
            raise ValueError(f"{record_type} ledger is unavailable")
        return []
    candidate = _private_file(path, f"{record_type} ledger", maximum=8 * 1024 * 1024)
    records: list[dict[str, Any]] = []
    previous = _ZERO_HASH
    try:
        lines = candidate.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError(f"{record_type} ledger is invalid") from exc
    for line in lines:
        if not line:
            continue
        try:
            record = json.loads(line, object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"{record_type} ledger is invalid") from exc
        if not isinstance(record, dict):
            raise ValueError(f"{record_type} ledger is invalid")
        digest = record.get("record_sha256")
        body = {key: value for key, value in record.items() if key != "record_sha256"}
        expected = hashlib.sha256(_canonical(body)).hexdigest()
        if (
            record.get("record_type") != record_type
            or record.get("sequence") != len(records) + 1
            or record.get("previous_sha256") != previous
            or not isinstance(digest, str)
            or not _HEX.fullmatch(digest)
            or digest == _ZERO_HASH
            or digest != expected
        ):
            raise ValueError(f"{record_type} ledger hash chain is invalid")
        records.append(record)
        previous = digest
    if required and not records:
        raise ValueError(f"{record_type} ledger is empty")
    return records


def _append(path: Path, record_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    lock_path = path.with_suffix(path.suffix + ".lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("canary ledger lock is invalid")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        records = _load_ledger(path, record_type, required=False)
        previous = str(records[-1]["record_sha256"]) if records else _ZERO_HASH
        body = {
            **payload,
            "record_type": record_type,
            "sequence": len(records) + 1,
            "previous_sha256": previous,
        }
        record = {**body, "record_sha256": hashlib.sha256(_canonical(body)).hexdigest()}
        line = _canonical(record) + b"\n"
        ledger_flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        ledger_descriptor = os.open(path, ledger_flags, 0o600)
        try:
            ledger_metadata = os.fstat(ledger_descriptor)
            if not stat.S_ISREG(ledger_metadata.st_mode):
                raise ValueError("canary ledger is invalid")
            os.fchmod(ledger_descriptor, 0o600)
            os.write(ledger_descriptor, line)
            os.fsync(ledger_descriptor)
        finally:
            os.close(ledger_descriptor)
        return record
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _sample(canary_dir: Path, status_path: Path) -> int:
    directory, _manifest, control = _canary_context(canary_dir)
    status_file = _private_file(status_path, "status snapshot", maximum=1024 * 1024)
    status = _object(status_file, "status snapshot")
    observed_at = _timestamp(status.get("captured_at"), "status")
    manifest = status.get("manifest")
    services = status.get("services")
    emergency = status.get("emergency_stop")
    images = status.get("images")
    if (
        not isinstance(manifest, Mapping)
        or not isinstance(services, Mapping)
        or not isinstance(emergency, Mapping)
    ):
        raise ValueError("status snapshot shape is invalid")
    missing = services.get("missing")
    if (
        status.get("schema_version") not in {"1.0", "1.1"}
        or status.get("source_commit") != control["source_commit"]
        or status.get("composition_status") != "composed"
        or manifest.get("sha256") != control["manifest_sha256"]
        or manifest.get("local_pilot_go") is not True
        or services.get("inspection_error") is not None
        or not isinstance(missing, list)
        or not isinstance(status.get("no_go_reasons"), list)
        or not isinstance(images, list)
    ):
        raise ValueError("status snapshot binding is invalid")
    attestation_issues = _status_attestation_issues(status)
    if attestation_issues:
        raise ValueError("; ".join(attestation_issues))
    image_services = set()
    binding_count = 0
    for item in images:
        if not isinstance(item, Mapping):
            raise ValueError("status image binding is invalid")
        service = item.get("service")
        digest = item.get(
            "actual_inspect_digest"
            if status.get("schema_version") == "1.1"
            else "local_inspect_digest"
        )
        if isinstance(service, str) and isinstance(digest, str):
            if _IMAGE_DIGEST.fullmatch(digest) is None or digest == "sha256:" + _ZERO_HASH:
                raise ValueError("status image binding is invalid")
            image_services.add(service)
            binding_count += 1
    if not {"local-runtime", "frappe-pwa"}.issubset(image_services):
        raise ValueError("required status image bindings are absent")
    existing = _load_ledger(directory / "runtime-samples.jsonl", "status_sample", required=False)
    if existing and observed_at <= _timestamp(existing[-1].get("observed_at"), "sample"):
        raise ValueError("status sample time must increase")
    reasons = status.get("no_go_reasons")
    record = _append(
        directory / "runtime-samples.jsonl",
        "status_sample",
        {
            "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
            "run_id": control["run_id"],
            "source_commit": control["source_commit"],
            "manifest_sha256": control["manifest_sha256"],
            "status_snapshot_sha256": _sha256(status_file),
            "verdict": status.get("verdict"),
            "missing_service_count": len(missing),
            "emergency_stop_active": emergency.get("active") is True,
            "image_binding_count": binding_count,
        },
    )
    print(f"Recorded canary status sample {record['sequence']}.")
    healthy = status.get("verdict") == "running" and not missing and not reasons
    return 0 if healthy and emergency.get("active") is not True else 3


def _record(
    canary_dir: Path,
    *,
    kind: str,
    source: str,
    observed_at: str | None,
    evidence_file: Path | None,
    chain_attestation: Path | None,
) -> int:
    directory, manifest, control = _canary_context(canary_dir)
    if source != _REQUIRED_CHECK_SOURCES[kind]:
        raise ValueError("live check source is invalid")
    repo_root = Path(__file__).resolve().parents[2]
    attestation: dict[str, Any] | None = None
    if kind == "model_identity_exact":
        if observed_at is not None or evidence_file is not None or chain_attestation is None:
            raise ValueError("model identity requires only a machine chain attestation")
        artifact, candidate, evidence_sha256 = _object_with_sha256(
            chain_attestation, "machine chain attestation", maximum=1024 * 1024
        )
        if os.path.commonpath((str(artifact), str(repo_root))) == str(repo_root):
            raise ValueError("machine chain attestation must be outside the repository")
        site_id = manifest.get("site_id")
        if not isinstance(site_id, str):
            raise ValueError("canary manifest site binding is invalid")
        try:
            attestation = validate_canary_chain_attestation(
                candidate,
                expected_run_id=str(control["run_id"]),
                expected_site_id=site_id,
                expected_source_commit=str(control["source_commit"]),
                expected_manifest_sha256=str(control["manifest_sha256"]),
                expected_activation_time=str(control["activation_time"]),
            )
        except CanaryChainVerificationError as exc:
            raise ValueError("machine chain attestation is invalid") from exc
        window = attestation["observation_window"]
        chain = attestation["chain"]
        if not isinstance(window, Mapping) or not isinstance(chain, Mapping):
            raise ValueError("machine chain attestation is invalid")
        observed = _timestamp(window.get("ended_at"), "machine chain attestation")
    else:
        if observed_at is None or evidence_file is None or chain_attestation is not None:
            raise ValueError("live check artifact inputs are invalid")
        observed = _timestamp(observed_at, "live check")
        artifact = _private_file(evidence_file, "live check artifact")
        if os.path.commonpath((str(artifact), str(repo_root))) == str(repo_root):
            raise ValueError("live check artifact must be outside the repository")
        evidence_sha256 = _sha256(artifact)
    payload: dict[str, Any] = {
        "observed_at": observed.isoformat().replace("+00:00", "Z"),
        "run_id": control["run_id"],
        "source_commit": control["source_commit"],
        "manifest_sha256": control["manifest_sha256"],
        "kind": kind,
        "status": "pass",
        "source": source,
        "evidence_sha256": evidence_sha256,
    }
    if kind == "model_identity_exact":
        if attestation is None:
            raise ValueError("machine chain attestation is invalid")
        chain = attestation["chain"]
        if not isinstance(chain, Mapping):
            raise ValueError("machine chain attestation is invalid")
        payload["response_reported_observed_model"] = chain["response_reported_observed_model"]
        payload["chain_attestation"] = attestation
    record = _append(directory / "live-checks.jsonl", "live_check", payload)
    print(f"Recorded canary live check {record['sequence']}.")
    return 0


def _validate_common(record: Mapping[str, Any], control: Mapping[str, Any]) -> None:
    if (
        record.get("run_id") != control.get("run_id")
        or record.get("source_commit") != control.get("source_commit")
        or record.get("manifest_sha256") != control.get("manifest_sha256")
    ):
        raise ValueError("canary ledger binding is invalid")


def _atomic_private(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _duration_value(seconds: float) -> object:
    hours = seconds / 3600
    return int(hours) if hours.is_integer() else round(hours, 2)


def _finalize(canary_dir: Path) -> int:
    directory, manifest, control = _canary_context(canary_dir)
    samples = _load_ledger(directory / "runtime-samples.jsonl", "status_sample", required=True)
    checks = _load_ledger(directory / "live-checks.jsonl", "live_check", required=True)
    for record in [*samples, *checks]:
        _validate_common(record, control)
    if len(samples) < 2:
        raise ValueError("at least two canary status samples are required")
    sample_times = [_timestamp(record.get("observed_at"), "sample") for record in samples]
    duration_seconds = (sample_times[-1] - sample_times[0]).total_seconds()
    gaps = [
        int((current - previous).total_seconds())
        for previous, current in zip(sample_times, sample_times[1:], strict=False)
    ]
    if any(gap <= 0 for gap in gaps):
        raise ValueError("canary status sample order is invalid")
    for sample in samples:
        if (
            sample.get("verdict") != "running"
            or sample.get("missing_service_count") != 0
            or sample.get("emergency_stop_active") is not False
            or not isinstance(sample.get("image_binding_count"), int)
            or int(sample["image_binding_count"]) < 2
            or _HEX.fullmatch(str(sample.get("status_snapshot_sha256"))) is None
            or sample.get("status_snapshot_sha256") == _ZERO_HASH
        ):
            raise ValueError("canary contains an unhealthy status sample")
    activation_time = _timestamp(control.get("activation_time"), "activation")
    site_id = manifest.get("site_id")
    if not isinstance(site_id, str):
        raise ValueError("canary manifest site binding is invalid")
    latest: dict[str, Mapping[str, Any]] = {}
    for check in checks:
        kind = check.get("kind")
        if kind not in _REQUIRED_CHECK_SOURCES:
            raise ValueError("live check kind is invalid")
        if check.get("source") != _REQUIRED_CHECK_SOURCES[str(kind)]:
            raise ValueError("live check source is invalid")
        observed = _timestamp(check.get("observed_at"), "live check")
        if observed < activation_time or observed < sample_times[0] or observed > sample_times[-1]:
            raise ValueError("live check is outside the observed canary period")
        digest = check.get("evidence_sha256")
        if (
            check.get("status") != "pass"
            or not isinstance(digest, str)
            or _HEX.fullmatch(digest) is None
            or digest == _ZERO_HASH
        ):
            raise ValueError("live check evidence is invalid")
        expected_fields = _LIVE_CHECK_BASE_FIELDS
        if kind == "model_identity_exact":
            expected_fields = expected_fields | {
                "response_reported_observed_model",
                "chain_attestation",
            }
        if set(check) != expected_fields:
            raise ValueError("live check schema is invalid")
        if kind == "model_identity_exact":
            candidate = check.get("chain_attestation")
            try:
                attestation = validate_canary_chain_attestation(
                    candidate,
                    expected_run_id=str(control["run_id"]),
                    expected_site_id=site_id,
                    expected_source_commit=str(control["source_commit"]),
                    expected_manifest_sha256=str(control["manifest_sha256"]),
                    expected_activation_time=str(control["activation_time"]),
                )
            except CanaryChainVerificationError as exc:
                raise ValueError("machine chain attestation is invalid") from exc
            window = attestation["observation_window"]
            chain = attestation["chain"]
            if (
                not isinstance(window, Mapping)
                or not isinstance(chain, Mapping)
                or observed != _timestamp(window.get("ended_at"), "machine chain attestation")
                or _timestamp(window.get("started_at"), "machine chain attestation")
                < sample_times[0]
                or _timestamp(window.get("ended_at"), "machine chain attestation")
                > sample_times[-1]
                or check.get("response_reported_observed_model")
                != chain.get("response_reported_observed_model")
            ):
                raise ValueError("machine chain attestation period or facts are invalid")
        previous = latest.get(str(kind))
        if previous is None or observed >= _timestamp(previous.get("observed_at"), "live check"):
            latest[str(kind)] = check
    missing_checks = sorted(set(_REQUIRED_CHECK_SOURCES) - set(latest))
    if missing_checks:
        raise ValueError("required live checks are incomplete")
    model_check = latest["model_identity_exact"]
    model_attestation = model_check.get("chain_attestation")
    if not isinstance(model_attestation, Mapping):
        raise ValueError("machine chain attestation is invalid")
    model_chain = model_attestation.get("chain")
    if not isinstance(model_chain, Mapping):
        raise ValueError("machine chain attestation is invalid")
    evidence_dir = directory / "evidence"
    if evidence_dir.exists():
        raise ValueError("canary evidence directory already exists")
    evidence_dir.mkdir(mode=0o700)
    maximum_gap = max(gaps) if gaps else 0
    evidence = {
        "schema_version": "1.0",
        "captured_at": sample_times[-1].isoformat().replace("+00:00", "Z"),
        "run_id": control["run_id"],
        "source_commit": control["source_commit"],
        "manifest_sha256": control["manifest_sha256"],
        "evidence_basis": "local_private_hash_bound",
        "runtime_window": {
            "duration_hours": _duration_value(duration_seconds),
            "max_gap_seconds": maximum_gap,
            "sample_count": len(samples),
        },
        "stability_assessment": {
            **_DEFERRED_STABILITY,
            "continuous_runtime_stability": "not_assessed",
        },
        "live_checks": {kind: "pass" for kind in sorted(_REQUIRED_CHECK_SOURCES)},
        "response_reported_observed_model": model_chain["response_reported_observed_model"],
        "chain_attestation_sha256": model_check["evidence_sha256"],
        "chain_attestation_payload_sha256": model_attestation["payload_sha256"],
        "canary_chain": {
            "agent_invocation_count": model_chain["agent_invocation_count"],
            "context_chain_count": model_chain["context_chain_count"],
            "network_call_count": model_chain["network_call_count"],
            "tool_call_count": model_chain["tool_call_count"],
            "external_send_count": model_chain["external_send_count"],
            "fatal_or_mismatch_invocation_count": model_chain["fatal_or_mismatch_invocation_count"],
            "context_state_bound": model_chain["context_state_bound"],
            "observer_fatal_latch_open": model_chain["observer_fatal_latch_open"],
            "frappe_receipt_bound": (
                model_chain["draft_status"] == "succeeded"
                and model_chain["receipt_request_bound"] is True
            ),
        },
        "scope_verdicts": {
            "email_deepseek_identity_local_shadow": "go",
            "formal_compliance": "no_go",
            "formal_production": "no_go",
            "external_send": "no_go",
            "kingdee": "no_go",
            "cloud_deployment": "no_go",
        },
    }
    evidence_bytes = json.dumps(evidence, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    summary = (
        "# Task 13 local shadow canary\n\n"
        "- Email + DeepSeek + identity resolution local shadow: Go\n"
        "- Formal production, compliance, external send, Kingdee and cloud: No-Go\n"
        f"- Observed runtime only: {evidence['runtime_window']['duration_hours']} hours\n"
        f"- Status samples: {len(samples)}\n"
        "- 72-hour continuous stability: deferred by user; not assessed\n"
        "- Response-reported observed model: deepseek-v4-flash\n"
        "- Evidence contains hashes and bounded results only; source artifacts stay private.\n"
    ).encode()
    evidence_path = evidence_dir / "task13-canary-evidence.json"
    summary_path = evidence_dir / "task13-canary-summary.md"
    _atomic_private(evidence_path, evidence_bytes)
    _atomic_private(summary_path, summary)
    sums = (
        f"{hashlib.sha256(evidence_bytes).hexdigest()}  {evidence_path.name}\n"
        f"{hashlib.sha256(summary).hexdigest()}  {summary_path.name}\n"
    ).encode("ascii")
    _atomic_private(evidence_dir / "SHA256SUMS", sums)
    print(f"Finalized private Task 13 canary evidence in {evidence_dir}.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "sample":
            return _sample(args.canary_dir, args.status_json)
        if args.command == "record":
            return _record(
                args.canary_dir,
                kind=args.kind,
                source=args.source,
                observed_at=args.observed_at,
                evidence_file=args.evidence_file,
                chain_attestation=args.chain_attestation,
            )
        if args.command == "finalize":
            return _finalize(args.canary_dir)
        raise ValueError("unsupported canary evidence command")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"CANARY EVIDENCE FAILED: {exc}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
