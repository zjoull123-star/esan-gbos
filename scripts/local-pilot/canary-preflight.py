#!/usr/bin/env python3
"""Validate private Email + DeepSeek canary inputs without rendering their values."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_EMAIL_FIELDS = {
    "instance_id",
    "team_ref",
    "agent_task_type",
    "account_user_ref",
    "host",
    "port",
    "mailbox",
    "folder",
    "username",
    "password",
    "poll_limit",
    "max_message_bytes",
    "max_attachment_bytes",
    "max_attachments",
    "rescan_max_window_seconds",
    "rescan_max_uids",
    "initial_checkpoint",
}
_CHECKPOINT_FIELDS = {"mailbox", "uid", "uidvalidity", "version"}
_CREDENTIAL_BINDING_FIELDS = (
    "account_user_ref",
    "agent_task_type",
    "folder",
    "host",
    "instance_id",
    "mailbox",
    "port",
    "team_ref",
    "username",
)
_RECEIPT_FIELDS = {
    "activation_time",
    "checkpoint_sha256",
    "credential_binding_hmac_sha256",
    "observed_at",
    "operation",
    "read_only",
    "schema",
    "source_commit",
    "version",
}
_RECEIPT_SCHEMA = "gbos.email_checkpoint_receipt"
_RECEIPT_OPERATION = "STATUS_UIDVALIDITY_UIDNEXT"
_MAX_CHECKPOINT_ARTIFACT_BYTES = 8_192
_LEXICON_FIELDS = {
    "schema_version",
    "site_id",
    "resolver_version",
    "approved_by",
    "approved_at",
    "expires_at",
    "names_complete",
    "organizations_complete",
    "names",
    "organizations",
}
_TASKS = {"sales", "purchase", "product_sample", "ceo"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--run-control", required=True, type=Path)
    parser.add_argument("--secret-dir", required=True, type=Path)
    parser.add_argument("--repo-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--now")
    parser.add_argument("--json", action="store_true")
    return parser


def _object(path: Path, *, maximum: int, private: bool) -> dict[str, Any]:
    try:
        before = path.lstat()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError("required private input is unavailable") from exc
    try:
        details = os.fstat(descriptor)
        if (
            stat.S_ISLNK(before.st_mode)
            or before.st_dev != details.st_dev
            or before.st_ino != details.st_ino
            or not stat.S_ISREG(details.st_mode)
            or not 0 < details.st_size <= maximum
            or (private and stat.S_IMODE(details.st_mode) != 0o600)
        ):
            raise ValueError("required private input is unsafe")
        payload = os.read(descriptor, maximum + 1)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if len(payload) != details.st_size or after.st_size != details.st_size:
        raise ValueError("required private input changed during validation")
    try:
        value = json.loads(payload, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("required private input is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("required private input must be an object")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _private_artifact(path: Path) -> bytes:
    descriptor = -1
    try:
        before = path.lstat()
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        details = os.fstat(descriptor)
        if (
            stat.S_ISLNK(before.st_mode)
            or before.st_dev != details.st_dev
            or before.st_ino != details.st_ino
            or not stat.S_ISREG(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o600
            or not 0 < details.st_size <= _MAX_CHECKPOINT_ARTIFACT_BYTES
        ):
            raise ValueError("email checkpoint receipt validation failed")
        chunks: list[bytes] = []
        remaining = details.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, _MAX_CHECKPOINT_ARTIFACT_BYTES + 1))
            if not chunk:
                raise ValueError("email checkpoint receipt validation failed")
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(payload) != details.st_size
            or after.st_dev != details.st_dev
            or after.st_ino != details.st_ino
            or after.st_size != details.st_size
            or stat.S_IMODE(after.st_mode) != 0o600
        ):
            raise ValueError("email checkpoint receipt validation failed")
        return payload
    except ValueError:
        raise
    except OSError:
        raise ValueError("email checkpoint receipt is unavailable") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _closed_json_object(payload: bytes) -> dict[str, object]:
    try:
        value = json.loads(payload, object_pairs_hook=_unique_object)
    except UnicodeDecodeError, json.JSONDecodeError, ValueError:
        raise ValueError("email checkpoint receipt validation failed") from None
    if not isinstance(value, dict):
        raise ValueError("email checkpoint receipt validation failed")
    return value


def _private_bytes(path: Path, *, minimum: int, maximum: int, exact: int = 0) -> bytes:
    try:
        before = path.lstat()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise ValueError("required credential file is unavailable") from exc
    try:
        details = os.fstat(descriptor)
        expected = exact if exact else details.st_size
        if (
            stat.S_ISLNK(before.st_mode)
            or before.st_dev != details.st_dev
            or before.st_ino != details.st_ino
            or not stat.S_ISREG(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o600
            or not minimum <= details.st_size <= maximum
            or (exact and details.st_size != exact)
        ):
            raise ValueError("required credential file is unsafe")
        payload = os.read(descriptor, maximum + 1)
    finally:
        os.close(descriptor)
    if len(payload) != expected:
        raise ValueError("required credential file changed during validation")
    return payload


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp is invalid")
    return parsed.astimezone(UTC)


def _now(value: str | None) -> datetime:
    return datetime.now(UTC) if value is None else _timestamp(value)


def _source_commit(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ValueError("source binding is unavailable")
    return value


def _require_repo_external(path: Path, repo_root: Path) -> None:
    try:
        resolved = path.resolve(strict=True)
        root = repo_root.resolve(strict=True)
    except OSError as exc:
        raise ValueError("canary private inputs must exist outside the repository") from exc
    if os.path.commonpath((str(resolved), str(root))) == str(root):
        raise ValueError("canary private inputs must stay outside the repository")


def _validate_manifest(
    manifest: Mapping[str, Any],
    control: Mapping[str, Any],
    *,
    now: datetime,
) -> None:
    channels = manifest.get("channels")
    deepseek = manifest.get("deepseek")
    if (
        manifest.get("production_go") is not False
        or manifest.get("local_pilot_go") is not True
        or manifest.get("local_pilot_status") != "ready"
        or not isinstance(channels, Mapping)
        or not isinstance(deepseek, Mapping)
        or deepseek.get("enabled") is not True
        or deepseek.get("kill_switch") is not False
        or deepseek.get("model") != "deepseek-v4-flash"
    ):
        raise ValueError("canary manifest boundary is invalid")
    for name in ("email", "wecom", "whatsapp", "media"):
        value = channels.get(name)
        expected_enabled = name == "email"
        if not isinstance(value, Mapping) or value.get("enabled") is not expected_enabled:
            raise ValueError("canary manifest channel scope is invalid")
    scope = control.get("scope")
    if scope != {
        "channels": ["email"],
        "model": "deepseek-v4-flash",
        "external_send": False,
        "formal_commands": False,
    }:
        raise ValueError("canary control scope is invalid")
    if (
        control.get("state") != "prepared"
        or control.get("schema_version") != "1.1"
        or control.get("stability_assessment")
        != {
            "continuous_runtime_required": False,
            "seventy_two_hour_run": "deferred_by_user",
        }
        or "pilot_window_hours" in control
    ):
        raise ValueError("canary stability scope is invalid")
    email = channels.get("email")
    if not isinstance(email, Mapping):
        raise ValueError("canary activation binding is invalid")
    activation = _timestamp(control.get("activation_time"))
    if email.get("activation_time") != control.get("activation_time") or activation > now:
        raise ValueError("canary activation binding is invalid")


def _bounded_text(value: object, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > maximum
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        raise ValueError("email credential string boundary is invalid")
    return value


def _bounded_integer(value: object, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("email credential numeric boundary is invalid")
    return value


def _validate_email(path: Path) -> tuple[dict[str, object], dict[str, object]]:
    value = _object(path, maximum=65_536, private=True)
    if set(value) != _EMAIL_FIELDS:
        raise ValueError("email credential schema is invalid")
    for field, maximum in (
        ("instance_id", 256),
        ("team_ref", 256),
        ("account_user_ref", 256),
        ("host", 253),
        ("mailbox", 256),
        ("folder", 256),
        ("username", 4_096),
        ("password", 4_096),
    ):
        _bounded_text(value.get(field), maximum=maximum)
    account_user_ref = str(value["account_user_ref"])
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in account_user_ref
    ):
        raise ValueError("email credential routing is incomplete")
    if value.get("agent_task_type") not in _TASKS:
        raise ValueError("email credential routing is incomplete")
    for field, minimum, maximum in (
        ("port", 1, 65_535),
        ("poll_limit", 1, 1_000),
        ("max_message_bytes", 1, 100_000_000),
        ("max_attachment_bytes", 1, 100_000_000),
        ("max_attachments", 1, 1_000),
        ("rescan_max_window_seconds", 1, 90 * 86_400),
        ("rescan_max_uids", 1, 10_000),
    ):
        _bounded_integer(value.get(field), minimum=minimum, maximum=maximum)
    checkpoint_value = value.get("initial_checkpoint")
    if not isinstance(checkpoint_value, str) or not checkpoint_value:
        raise ValueError("email initial checkpoint is required")
    if (
        checkpoint_value != checkpoint_value.strip()
        or len(checkpoint_value.encode("utf-8")) > 4_096
        or any(character in checkpoint_value for character in ("\x00", "\r", "\n"))
    ):
        raise ValueError("email initial checkpoint is invalid")
    try:
        checkpoint = json.loads(checkpoint_value, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("email initial checkpoint is invalid") from exc
    if (
        not isinstance(checkpoint, dict)
        or set(checkpoint) != _CHECKPOINT_FIELDS
        or checkpoint.get("version") != 1
        or checkpoint.get("mailbox") != value.get("mailbox")
        or type(checkpoint.get("uid")) is not int
        or checkpoint["uid"] < 0
        or type(checkpoint.get("uidvalidity")) is not int
        or checkpoint["uidvalidity"] < 1
    ):
        raise ValueError("email initial checkpoint is invalid")
    credential_identity = {field: value[field] for field in _CREDENTIAL_BINDING_FIELDS}
    return checkpoint, credential_identity


def _credential_binding_hmac_sha256(
    credential_identity: Mapping[str, object],
    binding_key: bytes,
) -> str:
    canonical_identity = (
        json.dumps(credential_identity, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    return hmac.new(binding_key, canonical_identity, hashlib.sha256).hexdigest()


def _canary_directory(manifest: Path, control: Path) -> Path:
    directory = manifest.parent
    control_directory = control.parent
    try:
        details = directory.lstat()
        control_details = control_directory.lstat()
        resolved = directory.resolve(strict=True)
        control_parent = control_directory.resolve(strict=True)
    except OSError:
        raise ValueError("email checkpoint receipt directory is unavailable") from None
    if (
        directory.is_symlink()
        or control_directory.is_symlink()
        or not stat.S_ISDIR(details.st_mode)
        or not stat.S_ISDIR(control_details.st_mode)
        or stat.S_IMODE(details.st_mode) != 0o700
        or stat.S_IMODE(control_details.st_mode) != 0o700
        or resolved != control_parent
    ):
        raise ValueError("email checkpoint receipt directory is unsafe")
    return resolved


def _validate_checkpoint_receipt(
    directory: Path,
    *,
    binding_key: bytes,
    credential_identity: Mapping[str, object],
    initial_checkpoint: Mapping[str, object],
    activation: datetime,
    now: datetime,
    source_commit: str,
) -> None:
    checkpoint_bytes = _private_artifact(directory / "email-checkpoint.json")
    receipt_bytes = _private_artifact(directory / "email-checkpoint-receipt.json")
    checkpoint = _closed_json_object(checkpoint_bytes)
    receipt = _closed_json_object(receipt_bytes)
    version = checkpoint.get("version")
    mailbox = checkpoint.get("mailbox")
    uid = checkpoint.get("uid")
    uidvalidity = checkpoint.get("uidvalidity")
    if (
        set(checkpoint) != _CHECKPOINT_FIELDS
        or type(version) is not int
        or version != 1
        or not isinstance(mailbox, str)
        or type(uid) is not int
        or type(uidvalidity) is not int
        or checkpoint != initial_checkpoint
    ):
        raise ValueError("email checkpoint receipt validation failed")
    if uid < 0 or uidvalidity < 1:
        raise ValueError("email checkpoint receipt validation failed")
    if (
        set(receipt) != _RECEIPT_FIELDS
        or receipt.get("schema") != _RECEIPT_SCHEMA
        or type(receipt.get("version")) is not int
        or receipt.get("version") != 1
        or receipt.get("operation") != _RECEIPT_OPERATION
        or receipt.get("read_only") is not True
        or receipt.get("source_commit") != source_commit
        or receipt.get("checkpoint_sha256") != hashlib.sha256(checkpoint_bytes).hexdigest()
        or not hmac.compare_digest(
            str(receipt.get("credential_binding_hmac_sha256")),
            _credential_binding_hmac_sha256(credential_identity, binding_key),
        )
    ):
        raise ValueError("email checkpoint receipt validation failed")
    receipt_activation = _timestamp(receipt.get("activation_time"))
    observed = _timestamp(receipt.get("observed_at"))
    if receipt_activation != activation or not activation <= observed <= now:
        raise ValueError("email checkpoint receipt validation failed")


def _validate_lexicon(path: Path, *, site_id: object, now: datetime) -> None:
    value = _object(path, maximum=65_536, private=True)
    if (
        set(value) != _LEXICON_FIELDS
        or value.get("schema_version") != "1.0"
        or value.get("site_id") != site_id
        or value.get("names_complete") is not True
        or value.get("organizations_complete") is not True
        or not isinstance(value.get("names"), list)
        or not isinstance(value.get("organizations"), list)
        or not value["names"] + value["organizations"]
    ):
        raise ValueError("trusted phrase lexicon is invalid")
    approved = _timestamp(value.get("approved_at"))
    expires = _timestamp(value.get("expires_at"))
    if not approved <= now < expires:
        raise ValueError("trusted phrase lexicon is not current")
    if expires - approved > timedelta(days=30):
        raise ValueError("trusted phrase lexicon attestation window is invalid")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        repo_root = Path(__file__).resolve().parents[2]
        if args.repo_root is not None and args.repo_root.resolve() != repo_root:
            raise ValueError("repository root override does not match the bound repository")
        for candidate in (args.manifest, args.run_control, args.secret_dir):
            _require_repo_external(candidate, repo_root)
        manifest = _object(args.manifest, maximum=65_536, private=True)
        control = _object(args.run_control, maximum=65_536, private=True)
        if hashlib.sha256(args.manifest.read_bytes()).hexdigest() != control.get("manifest_sha256"):
            raise ValueError("canary manifest digest conflicts")
        source_commit = _source_commit(repo_root)
        if control.get("source_commit") != source_commit:
            raise ValueError("canary source binding conflicts")
        current_time = _now(args.now)
        _validate_manifest(manifest, control, now=current_time)
        canary_directory = _canary_directory(args.manifest, args.run_control)
        directory = args.secret_dir
        details = directory.lstat()
        if (
            directory.is_symlink()
            or not directory.is_dir()
            or stat.S_IMODE(details.st_mode) != 0o700
        ):
            raise ValueError("secret directory is unsafe")
        initial_checkpoint, credential_identity = _validate_email(directory / "email_credential")
        identity_hmac_key = _private_bytes(
            directory / "identity_hmac_key", minimum=32, maximum=32, exact=32
        )
        _validate_checkpoint_receipt(
            canary_directory,
            binding_key=identity_hmac_key,
            credential_identity=credential_identity,
            initial_checkpoint=initial_checkpoint,
            activation=_timestamp(control.get("activation_time")),
            now=current_time,
            source_commit=source_commit,
        )
        _private_bytes(directory / "deepseek_api_key", minimum=1, maximum=4096)
        _private_bytes(directory / "tokenizer_hmac_key", minimum=32, maximum=32, exact=32)
        _private_bytes(directory / "mapping_vault_key", minimum=32, maximum=32, exact=32)
        for name in (
            "frappe_materializer_api_key",
            "frappe_materializer_api_secret",
            "frappe_identity_resolver_api_key",
            "frappe_identity_resolver_api_secret",
        ):
            _private_bytes(directory / name, minimum=1, maximum=4096)
        _validate_lexicon(
            directory / "trusted_phrase_lexicon",
            site_id=manifest.get("site_id"),
            now=current_time,
        )
        payload = {
            "schema_version": "1.0",
            "ready": True,
            "checks": {
                "credential_files": "verified",
                "email_initial_checkpoint": "verified",
                "manifest_binding": "verified",
                "phrase_lexicon": "verified",
                "source_binding": "verified",
            },
        }
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(f"CANARY PREFLIGHT FAILED: {exc}", file=sys.stderr)
        return 78
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("Email + DeepSeek canary private inputs are ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
