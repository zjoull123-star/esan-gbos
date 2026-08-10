from __future__ import annotations

import hashlib
import hmac
import json
import stat
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CANARY_PREFLIGHT = ROOT / "scripts" / "local-pilot" / "canary-preflight"
ACTIVATION = "2026-08-11T09:00:00Z"
NOW = "2026-08-11T10:00:00Z"
MAILBOX = "mailbox-private-sentinel@example.invalid"
HOST = "imap-private-sentinel.example.invalid"
ACCOUNT = "account-private-sentinel@example.invalid"
PASSWORD = "password-private-sentinel"


@dataclass(frozen=True)
class CanaryFixture:
    canary_dir: Path
    manifest: Path
    control: Path
    secrets: Path
    checkpoint: Path
    receipt: Path
    checkpoint_value: dict[str, object]
    source_commit: str


def _private_bytes(path: Path, content: bytes) -> None:
    path.write_bytes(content)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _private_json(path: Path, value: object) -> None:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    _private_bytes(path, payload.encode())


def _source_commit() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _prepared_canary(tmp_path: Path) -> CanaryFixture:
    source_commit = _source_commit()
    canary_dir = tmp_path / "canary"
    canary_dir.mkdir(mode=0o700)
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    manifest = canary_dir / "pilot-manifest.json"
    control = canary_dir / "canary-run.json"
    checkpoint = canary_dir / "email-checkpoint.json"
    receipt = canary_dir / "email-checkpoint-receipt.json"

    manifest_value = {
        "channels": {
            "email": {"activation_time": ACTIVATION, "enabled": True},
            "media": {"enabled": False},
            "wecom": {"enabled": False},
            "whatsapp": {"enabled": False},
        },
        "deepseek": {
            "enabled": True,
            "kill_switch": False,
            "model": "deepseek-v4-flash",
        },
        "local_pilot_go": True,
        "local_pilot_status": "ready",
        "production_go": False,
        "site_id": "gbos.localhost",
    }
    _private_json(manifest, manifest_value)
    control_value = {
        "activation_time": ACTIVATION,
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "schema_version": "1.1",
        "scope": {
            "channels": ["email"],
            "external_send": False,
            "formal_commands": False,
            "model": "deepseek-v4-flash",
        },
        "source_commit": source_commit,
        "stability_assessment": {
            "continuous_runtime_required": False,
            "seventy_two_hour_run": "deferred_by_user",
        },
        "state": "prepared",
    }
    _private_json(control, control_value)

    checkpoint_value: dict[str, object] = {
        "mailbox": MAILBOX,
        "uid": 900,
        "uidvalidity": 55,
        "version": 1,
    }
    credential = {
        "account_user_ref": ACCOUNT,
        "agent_task_type": "sales",
        "folder": "INBOX",
        "host": HOST,
        "initial_checkpoint": json.dumps(
            checkpoint_value,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "instance_id": "email-canary-v1",
        "mailbox": MAILBOX,
        "max_attachment_bytes": 5_000_000,
        "max_attachments": 20,
        "max_message_bytes": 10_000_000,
        "password": PASSWORD,
        "poll_limit": 20,
        "port": 993,
        "rescan_max_uids": 500,
        "rescan_max_window_seconds": 86_400,
        "team_ref": "TEAM-SALES",
        "username": ACCOUNT,
    }
    _private_json(secrets / "email_credential", credential)
    _private_bytes(secrets / "deepseek_api_key", b"deepseek-private-sentinel")
    _private_bytes(secrets / "identity_hmac_key", b"i" * 32)
    _private_bytes(secrets / "tokenizer_hmac_key", b"t" * 32)
    _private_bytes(secrets / "mapping_vault_key", b"m" * 32)
    _private_bytes(secrets / "frappe_materializer_api_key", b"materializer-key")
    _private_bytes(secrets / "frappe_materializer_api_secret", b"materializer-secret")
    _private_bytes(secrets / "frappe_identity_resolver_api_key", b"resolver-key")
    _private_bytes(secrets / "frappe_identity_resolver_api_secret", b"resolver-secret")
    _private_json(
        secrets / "trusted_phrase_lexicon",
        {
            "approved_at": "2026-08-10T00:00:00Z",
            "approved_by": "local-operator",
            "expires_at": "2026-08-20T00:00:00Z",
            "names": ["Approved Private Name"],
            "names_complete": True,
            "organizations": ["Approved Private Organization"],
            "organizations_complete": True,
            "resolver_version": "canary-v1",
            "schema_version": "1.0",
            "site_id": "gbos.localhost",
        },
    )
    return CanaryFixture(
        canary_dir=canary_dir,
        manifest=manifest,
        control=control,
        secrets=secrets,
        checkpoint=checkpoint,
        receipt=receipt,
        checkpoint_value=checkpoint_value,
        source_commit=source_commit,
    )


def _run_preflight(fixture: CanaryFixture) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(CANARY_PREFLIGHT),
            "--manifest",
            str(fixture.manifest),
            "--run-control",
            str(fixture.control),
            "--secret-dir",
            str(fixture.secrets),
            "--repo-root",
            str(ROOT),
            "--now",
            NOW,
            "--json",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _write_valid_artifacts(fixture: CanaryFixture) -> dict[str, object]:
    _private_json(fixture.checkpoint, fixture.checkpoint_value)
    credential = json.loads((fixture.secrets / "email_credential").read_text(encoding="utf-8"))
    binding_payload = {
        field: credential[field]
        for field in (
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
    }
    canonical_binding = (
        json.dumps(binding_payload, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    receipt = {
        "activation_time": "2026-08-11T17:00:00+08:00",
        "checkpoint_sha256": hashlib.sha256(fixture.checkpoint.read_bytes()).hexdigest(),
        "credential_binding_hmac_sha256": hmac.new(
            (fixture.secrets / "identity_hmac_key").read_bytes(),
            canonical_binding,
            hashlib.sha256,
        ).hexdigest(),
        "observed_at": "2026-08-11T09:30:00Z",
        "operation": "STATUS_UIDVALIDITY_UIDNEXT",
        "read_only": True,
        "schema": "gbos.email_checkpoint_receipt",
        "source_commit": fixture.source_commit,
        "version": 1,
    }
    _private_json(fixture.receipt, receipt)
    return receipt


def _rewrite_credential_checkpoint(fixture: CanaryFixture, checkpoint: str) -> None:
    credential_path = fixture.secrets / "email_credential"
    credential = json.loads(credential_path.read_text(encoding="utf-8"))
    credential["initial_checkpoint"] = checkpoint
    _private_json(credential_path, credential)


def _replace_with_symlink(path: Path, target: Path) -> None:
    content = path.read_bytes()
    path.unlink()
    _private_bytes(target, content)
    path.symlink_to(target)


def _assert_secret_safe_failure(
    result: subprocess.CompletedProcess[str],
    fixture: CanaryFixture,
) -> None:
    assert result.returncode == 78
    rendered = result.stdout + result.stderr
    for forbidden in (MAILBOX, HOST, ACCOUNT, PASSWORD):
        assert forbidden not in rendered
    for path in (fixture.checkpoint, fixture.receipt):
        if path.is_file() and not path.is_symlink():
            assert hashlib.sha256(path.read_bytes()).hexdigest() not in rendered


def test_preflight_accepts_closed_bound_checkpoint_receipt(tmp_path: Path) -> None:
    fixture = _prepared_canary(tmp_path)
    _write_valid_artifacts(fixture)

    result = _run_preflight(fixture)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ready"] is True


def test_preflight_binding_excludes_rotated_email_password(tmp_path: Path) -> None:
    fixture = _prepared_canary(tmp_path)
    _write_valid_artifacts(fixture)
    credential_path = fixture.secrets / "email_credential"
    credential = json.loads(credential_path.read_text(encoding="utf-8"))
    credential["password"] = "rotated-password-private-sentinel"
    _private_json(credential_path, credential)

    result = _run_preflight(fixture)

    assert result.returncode == 0, result.stderr


def test_preflight_rejects_missing_checkpoint_receipt_pair(tmp_path: Path) -> None:
    fixture = _prepared_canary(tmp_path)

    result = _run_preflight(fixture)

    assert result.returncode == 78
    assert "checkpoint" in result.stderr.lower()
    assert "receipt" in result.stderr.lower()


@pytest.mark.parametrize(
    "case",
    [
        "missing_checkpoint",
        "missing_receipt",
        "checkpoint_mode",
        "receipt_mode",
        "checkpoint_symlink",
        "receipt_symlink",
        "checkpoint_directory",
        "receipt_directory",
        "checkpoint_oversize",
        "receipt_oversize",
        "checkpoint_extra_field",
        "receipt_extra_field",
        "receipt_missing_field",
        "checkpoint_duplicate_key",
        "receipt_duplicate_key",
        "credential_checkpoint_duplicate_key",
        "future_observation",
        "stale_observation",
        "naive_observation",
        "activation_drift",
        "source_drift",
        "operation_drift",
        "read_only_drift",
        "checkpoint_digest_tamper",
        "credential_binding_tamper",
        "credential_identity_drift",
        "checkpoint_value_drift",
        "checkpoint_version_drift",
        "receipt_schema_drift",
        "receipt_version_drift",
        "canary_directory_mode",
        "control_parent_drift",
        "control_parent_symlink",
    ],
)
def test_preflight_rejects_unsafe_or_unbound_checkpoint_receipt(
    tmp_path: Path,
    case: str,
) -> None:
    fixture = _prepared_canary(tmp_path)
    receipt = _write_valid_artifacts(fixture)

    if case == "missing_checkpoint":
        fixture.checkpoint.unlink()
    elif case == "missing_receipt":
        fixture.receipt.unlink()
    elif case == "checkpoint_mode":
        fixture.checkpoint.chmod(0o640)
    elif case == "receipt_mode":
        fixture.receipt.chmod(0o640)
    elif case == "checkpoint_symlink":
        _replace_with_symlink(fixture.checkpoint, tmp_path / "checkpoint-target")
    elif case == "receipt_symlink":
        _replace_with_symlink(fixture.receipt, tmp_path / "receipt-target")
    elif case == "checkpoint_directory":
        fixture.checkpoint.unlink()
        fixture.checkpoint.mkdir(mode=0o700)
    elif case == "receipt_directory":
        fixture.receipt.unlink()
        fixture.receipt.mkdir(mode=0o700)
    elif case == "checkpoint_oversize":
        _private_bytes(fixture.checkpoint, b"x" * 8_193)
    elif case == "receipt_oversize":
        _private_bytes(fixture.receipt, b"x" * 8_193)
    elif case == "checkpoint_extra_field":
        _private_json(fixture.checkpoint, {**fixture.checkpoint_value, "extra": False})
        receipt["checkpoint_sha256"] = hashlib.sha256(fixture.checkpoint.read_bytes()).hexdigest()
        _private_json(fixture.receipt, receipt)
    elif case == "receipt_extra_field":
        _private_json(fixture.receipt, {**receipt, "extra": False})
    elif case == "receipt_missing_field":
        receipt.pop("operation")
        _private_json(fixture.receipt, receipt)
    elif case == "checkpoint_duplicate_key":
        raw = f'{{"mailbox":"{MAILBOX}","uid":900,"uid":901,"uidvalidity":55,"version":1}}\n'
        _private_bytes(fixture.checkpoint, raw.encode())
        receipt["checkpoint_sha256"] = hashlib.sha256(fixture.checkpoint.read_bytes()).hexdigest()
        _private_json(fixture.receipt, receipt)
    elif case == "receipt_duplicate_key":
        raw = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
        _private_bytes(fixture.receipt, (raw[:-1] + ',"version":1}\n').encode())
    elif case == "credential_checkpoint_duplicate_key":
        raw = f'{{"mailbox":"{MAILBOX}","uid":900,"uid":901,"uidvalidity":55,"version":1}}'
        _rewrite_credential_checkpoint(fixture, raw)
    elif case == "future_observation":
        receipt["observed_at"] = "2026-08-11T10:00:00.000001Z"
        _private_json(fixture.receipt, receipt)
    elif case == "stale_observation":
        receipt["observed_at"] = "2026-08-11T08:59:59Z"
        _private_json(fixture.receipt, receipt)
    elif case == "naive_observation":
        receipt["observed_at"] = "2026-08-11T09:30:00"
        _private_json(fixture.receipt, receipt)
    elif case == "activation_drift":
        receipt["activation_time"] = "2026-08-11T09:00:01Z"
        _private_json(fixture.receipt, receipt)
    elif case == "source_drift":
        receipt["source_commit"] = "0" * 40
        _private_json(fixture.receipt, receipt)
    elif case == "operation_drift":
        receipt["operation"] = "SELECT"
        _private_json(fixture.receipt, receipt)
    elif case == "read_only_drift":
        receipt["read_only"] = False
        _private_json(fixture.receipt, receipt)
    elif case == "checkpoint_digest_tamper":
        receipt["checkpoint_sha256"] = "0" * 64
        _private_json(fixture.receipt, receipt)
    elif case == "credential_binding_tamper":
        receipt["credential_binding_hmac_sha256"] = "0" * 64
        _private_json(fixture.receipt, receipt)
    elif case == "credential_identity_drift":
        credential_path = fixture.secrets / "email_credential"
        credential = json.loads(credential_path.read_text(encoding="utf-8"))
        credential["folder"] = "Pilot-Changed"
        _private_json(credential_path, credential)
    elif case == "checkpoint_value_drift":
        _private_json(fixture.checkpoint, {**fixture.checkpoint_value, "uid": 901})
        receipt["checkpoint_sha256"] = hashlib.sha256(fixture.checkpoint.read_bytes()).hexdigest()
        _private_json(fixture.receipt, receipt)
    elif case == "checkpoint_version_drift":
        _private_json(fixture.checkpoint, {**fixture.checkpoint_value, "version": 2})
        receipt["checkpoint_sha256"] = hashlib.sha256(fixture.checkpoint.read_bytes()).hexdigest()
        _private_json(fixture.receipt, receipt)
    elif case == "receipt_schema_drift":
        receipt["schema"] = "gbos.email_checkpoint_receipt.v2"
        _private_json(fixture.receipt, receipt)
    elif case == "receipt_version_drift":
        receipt["version"] = 2
        _private_json(fixture.receipt, receipt)
    elif case == "canary_directory_mode":
        fixture.canary_dir.chmod(0o750)
    elif case == "control_parent_drift":
        alternate = tmp_path / "other-canary"
        alternate.mkdir(mode=0o700)
        moved_control = alternate / fixture.control.name
        fixture.control.replace(moved_control)
        fixture = replace(fixture, control=moved_control)
    elif case == "control_parent_symlink":
        alias = tmp_path / "canary-alias"
        alias.symlink_to(fixture.canary_dir, target_is_directory=True)
        fixture = replace(fixture, control=alias / fixture.control.name)
    else:
        raise AssertionError(f"unhandled test case: {case}")

    result = _run_preflight(fixture)

    _assert_secret_safe_failure(result, fixture)
