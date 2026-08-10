from __future__ import annotations

import json
import os
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).parents[2]
STATUS = ROOT / "scripts" / "local-pilot" / "status.py"
STATUS_WRAPPER = ROOT / "scripts" / "local-pilot" / "status"
MANIFEST = ROOT / "infra" / "local" / "local-pilot-manifest.json"
CANARY_PREPARE = ROOT / "scripts" / "local-pilot" / "prepare-email-deepseek-canary"
CANARY_PREFLIGHT = ROOT / "scripts" / "local-pilot" / "canary-preflight"
FAULT_DRILLS = ROOT / "scripts" / "local-pilot" / "run-offline-fault-drills"
CANARY_EVIDENCE = ROOT / "scripts" / "local-pilot" / "canary-evidence"
START = ROOT / "scripts" / "local-pilot" / "start"

REQUIRED_CANARY_CHECKS = {
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


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _fixture(tmp_path: Path, *, running: list[str]) -> tuple[list[str], dict[str, str]]:
    entrypoints = tmp_path / "entrypoints.json"
    entrypoints.write_text(
        json.dumps(
            {
                "composition": {"status": "composed", "frappe_pwa": "composed"},
                "services": {},
            }
        ),
        encoding="utf-8",
    )
    images = tmp_path / "images.json"
    images.write_text(
        json.dumps(
            {
                "images": [
                    {
                        "service": "local-runtime",
                        "reference": "runtime:test",
                        "local_inspect_digest": "sha256:" + "a" * 64,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    compose = tmp_path / "compose.yml"
    compose.write_text("name: status-test\nservices: {}\n", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    docker = tmp_path / "docker"
    running_rows = json.dumps([{"Service": value, "State": "running"} for value in running])
    _write_executable(
        docker,
        f"#!/usr/bin/env python3\nimport json\nprint(json.dumps({running_rows}))\n",
    )
    environment = os.environ.copy()
    environment["PATH"] = os.pathsep.join((str(tmp_path), os.defpath))
    command = [
        "python3",
        str(STATUS),
        "--manifest",
        str(MANIFEST),
        "--entrypoints",
        str(entrypoints),
        "--image-lock",
        str(images),
        "--runtime-dir",
        str(runtime),
        "--repo-root",
        str(ROOT),
        "--compose-file",
        str(compose),
        "--json",
    ]
    return command, environment


def test_status_wrapper_without_json_flag_is_compatible_with_macos_bash(
    tmp_path: Path,
) -> None:
    docker = tmp_path / "docker"
    _write_executable(
        docker,
        "#!/usr/bin/env python3\nimport json\nprint(json.dumps([]))\n",
    )
    environment = os.environ.copy()
    environment["PATH"] = os.pathsep.join((str(tmp_path), os.defpath))

    result = subprocess.run(
        [str(STATUS_WRAPPER)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert "本地试点状态：disabled" in result.stdout


def test_status_json_reports_disabled_formal_manifest_without_stale_fixed_copy(
    tmp_path: Path,
) -> None:
    command, environment = _fixture(tmp_path, running=[])

    result = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "1.0"
    assert payload["captured_at"].endswith("Z")
    assert datetime.fromisoformat(payload["captured_at"].replace("Z", "+00:00")).tzinfo
    assert payload["composition_status"] == "composed"
    assert payload["manifest"]["local_pilot_go"] is False
    assert payload["manifest"]["status"] == "disabled"
    assert payload["verdict"] == "disabled"
    assert payload["no_go_reasons"] == ["manifest_disabled"]
    assert len(payload["manifest"]["sha256"]) == 64
    assert "未组合" not in result.stdout


def test_status_json_requires_every_email_projection_service_and_clear_latch(
    tmp_path: Path,
) -> None:
    required = [
        "postgres",
        "mariadb",
        "redis-cache",
        "redis-queue",
        "context-api",
        "agent-api",
        "observer-api",
        "materialization-worker",
        "frappe-backend",
        "frappe-websocket",
        "frappe-worker",
        "frappe-scheduler",
        "pwa",
        "prometheus",
        "email-poller",
        "connector-worker",
        "identity-resolution-worker",
        "model-projection-worker",
        "communication-draft-worker",
    ]
    command, environment = _fixture(tmp_path, running=required)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest["local_pilot_go"] = True
    manifest["local_pilot_status"] = "running"
    manifest["channels"]["email"].update(
        {
            "enabled": True,
            "activation_time": "2026-08-11T00:00:00Z",
            "credential_ref": "keychain://com.esan.gbos.local-pilot/email",
        }
    )
    manifest["deepseek"].update(
        {
            "enabled": True,
            "kill_switch": False,
            "keychain_ref": "keychain://com.esan.gbos.local-pilot/deepseek",
        }
    )
    candidate = tmp_path / "pilot.json"
    candidate.write_text(json.dumps(manifest), encoding="utf-8")
    command[command.index(str(MANIFEST))] = str(candidate)

    result = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["verdict"] == "running"
    assert payload["no_go_reasons"] == []
    assert payload["emergency_stop"]["active"] is False
    assert payload["services"]["missing"] == []
    assert payload["services"]["required"] == sorted(required)

    required.remove("communication-draft-worker")
    docker = tmp_path / "docker"
    required_rows = json.dumps([{"Service": value, "State": "running"} for value in required])
    _write_executable(
        docker,
        f"#!/usr/bin/env python3\nimport json\nprint(json.dumps({required_rows}))\n",
    )
    failed = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert failed.returncode != 0
    failed_payload = json.loads(failed.stdout)
    assert failed_payload["verdict"] == "no_go"
    assert failed_payload["services"]["missing"] == ["communication-draft-worker"]
    assert "required_services_not_running" in failed_payload["no_go_reasons"]

    unhealthy_rows = [
        {
            "Service": value,
            "State": "running",
            "Health": "unhealthy" if value == "pwa" else "healthy",
        }
        for value in [*required, "communication-draft-worker"]
    ]
    _write_executable(
        docker,
        f"#!/usr/bin/env python3\nimport json\nprint(json.dumps({json.dumps(unhealthy_rows)}))\n",
    )
    unhealthy = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert unhealthy.returncode != 0
    unhealthy_payload = json.loads(unhealthy.stdout)
    assert unhealthy_payload["services"]["missing"] == ["pwa"]


def test_canary_prepare_writes_only_private_repo_external_email_model_controls(
    tmp_path: Path,
) -> None:
    output = tmp_path / "prepared"
    formal_before = MANIFEST.read_bytes()

    result = subprocess.run(
        [
            str(CANARY_PREPARE),
            "--acknowledge-shadow-pilot",
            "--output-dir",
            str(output),
            "--site-id",
            "gbos.localhost",
            "--activation-time",
            "2026-08-11T09:00:00Z",
            "--email-credential-ref",
            "keychain://com.esan.gbos.local-pilot/email-canary",
            "--deepseek-keychain-ref",
            "keychain://com.esan.gbos.local-pilot/deepseek-canary",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert MANIFEST.read_bytes() == formal_before
    manifest_path = output / "pilot-manifest.json"
    control_path = output / "canary-run.json"
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(control_path.stat().st_mode) == 0o600
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    control = json.loads(control_path.read_text(encoding="utf-8"))
    assert manifest["local_pilot_go"] is True
    assert manifest["local_pilot_status"] == "ready"
    assert manifest["production_go"] is False
    assert manifest["capabilities"] == {
        "kingdee": False,
        "cloud_server": False,
        "cloud_business_storage": False,
        "external_send": False,
        "formal_business_commands": False,
    }
    assert manifest["channels"]["email"]["enabled"] is True
    for disabled in ("wecom", "whatsapp", "media"):
        assert manifest["channels"][disabled]["enabled"] is False
    assert manifest["deepseek"]["enabled"] is True
    assert manifest["deepseek"]["model"] == "deepseek-v4-flash"
    assert control["state"] == "prepared"
    assert control["schema_version"] == "1.1"
    assert control["stability_assessment"] == {
        "continuous_runtime_required": False,
        "seventy_two_hour_run": "deferred_by_user",
    }
    assert "pilot_window_hours" not in control
    assert "seventy_two_hour_window" not in control["live_checks"]
    assert control["scope"] == {
        "channels": ["email"],
        "model": "deepseek-v4-flash",
        "external_send": False,
        "formal_commands": False,
    }
    assert (
        control["manifest_sha256"]
        == __import__("hashlib").sha256(manifest_path.read_bytes()).hexdigest()
    )
    serialized = json.dumps(control, sort_keys=True) + json.dumps(manifest, sort_keys=True)
    assert "@" not in serialized
    assert "password" not in serialized.lower()
    assert "api_key" not in serialized.lower()


def test_canary_prepare_rejects_repo_paths_and_missing_acknowledgement(tmp_path: Path) -> None:
    base = [
        str(CANARY_PREPARE),
        "--output-dir",
        str(tmp_path / "prepared"),
        "--site-id",
        "gbos.localhost",
        "--activation-time",
        "2026-08-11T09:00:00Z",
        "--email-credential-ref",
        "keychain://com.esan.gbos.local-pilot/email-canary",
        "--deepseek-keychain-ref",
        "keychain://com.esan.gbos.local-pilot/deepseek-canary",
    ]
    missing_ack = subprocess.run(
        base,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert missing_ack.returncode != 0

    inside_repo = ROOT / ".runtime" / "forbidden-canary-output"
    rejected = subprocess.run(
        [*base, "--acknowledge-shadow-pilot", "--output-dir", str(inside_repo)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert not inside_repo.exists()


def test_generic_start_requires_canary_control_before_enabled_email_or_model(
    tmp_path: Path,
) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest["local_pilot_go"] = True
    manifest["local_pilot_status"] = "ready"
    manifest["channels"]["email"].update(
        {
            "enabled": True,
            "activation_time": "2026-08-11T09:00:00Z",
            "credential_ref": "keychain://com.esan.gbos.local-pilot/email-canary",
        }
    )
    manifest["deepseek"].update(
        {
            "enabled": True,
            "kill_switch": False,
            "keychain_ref": "keychain://com.esan.gbos.local-pilot/deepseek-canary",
        }
    )
    candidate = tmp_path / "enabled-pilot-manifest.json"
    _private(candidate, json.dumps(manifest).encode())

    result = subprocess.run(
        [str(START), "--manifest", str(candidate)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 78
    assert result.stderr == ("Enabled Email or DeepSeek requires a bound canary control file.\n")
    assert "Keychain" not in result.stderr
    assert "Docker" not in result.stderr


def _private(path: Path, content: bytes) -> None:
    path.write_bytes(content)
    path.chmod(0o600)


def _ledger_record(payload: dict[str, object]) -> dict[str, object]:
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return {**payload, "record_sha256": __import__("hashlib").sha256(encoded).hexdigest()}


def _write_ledger(path: Path, records: list[dict[str, object]]) -> None:
    previous = "0" * 64
    lines: list[str] = []
    for sequence, value in enumerate(records, start=1):
        record = _ledger_record(
            {
                **value,
                "sequence": sequence,
                "previous_sha256": previous,
            }
        )
        previous = str(record["record_sha256"])
        lines.append(json.dumps(record, separators=(",", ":"), sort_keys=True))
    _private(path, ("\n".join(lines) + "\n").encode())


def _prepared_canary(tmp_path: Path) -> tuple[Path, Path, Path]:
    output = tmp_path / "prepared"
    result = subprocess.run(
        [
            str(CANARY_PREPARE),
            "--acknowledge-shadow-pilot",
            "--output-dir",
            str(output),
            "--site-id",
            "gbos.localhost",
            "--activation-time",
            "2026-08-11T09:00:00Z",
            "--email-credential-ref",
            "keychain://com.esan.gbos.local-pilot/email-canary",
            "--deepseek-keychain-ref",
            "keychain://com.esan.gbos.local-pilot/deepseek-canary",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    credential = {
        "instance_id": "email-canary-v1",
        "team_ref": "TEAM-SALES",
        "agent_task_type": "sales",
        "account_user_ref": "ceo@example.invalid",
        "host": "imap.example.invalid",
        "port": 993,
        "mailbox": "pilot@example.invalid",
        "folder": "INBOX",
        "username": "pilot@example.invalid",
        "password": "RAW-PASSWORD-SENTINEL",
        "poll_limit": 20,
        "max_message_bytes": 10_000_000,
        "max_attachment_bytes": 5_000_000,
        "max_attachments": 20,
        "rescan_max_window_seconds": 86_400,
        "rescan_max_uids": 500,
        "initial_checkpoint": json.dumps(
            {"mailbox": "pilot@example.invalid", "uid": 900, "uidvalidity": 55, "version": 1},
            separators=(",", ":"),
        ),
    }
    _private(secrets / "email_credential", json.dumps(credential).encode())
    _private(secrets / "deepseek_api_key", b"DEEPSEEK-SECRET-SENTINEL")
    _private(secrets / "identity_hmac_key", b"i" * 32)
    _private(secrets / "tokenizer_hmac_key", b"t" * 32)
    _private(secrets / "mapping_vault_key", b"m" * 32)
    _private(secrets / "frappe_materializer_api_key", b"materializer-key")
    _private(secrets / "frappe_materializer_api_secret", b"materializer-secret")
    _private(secrets / "frappe_identity_resolver_api_key", b"resolver-key")
    _private(secrets / "frappe_identity_resolver_api_secret", b"resolver-secret")
    _private(
        secrets / "trusted_phrase_lexicon",
        json.dumps(
            {
                "schema_version": "1.0",
                "site_id": "gbos.localhost",
                "resolver_version": "canary-v1",
                "approved_by": "local-operator",
                "approved_at": "2026-08-10T00:00:00Z",
                "expires_at": "2026-08-20T00:00:00Z",
                "names_complete": True,
                "organizations_complete": True,
                "names": ["Approved Test Name"],
                "organizations": ["Approved Test Organization"],
            }
        ).encode(),
    )
    return output / "pilot-manifest.json", output / "canary-run.json", secrets


def test_canary_preflight_validates_private_inputs_without_rendering_values(tmp_path: Path) -> None:
    manifest, control, secrets = _prepared_canary(tmp_path)

    result = subprocess.run(
        [
            str(CANARY_PREFLIGHT),
            "--manifest",
            str(manifest),
            "--run-control",
            str(control),
            "--secret-dir",
            str(secrets),
            "--repo-root",
            str(ROOT),
            "--now",
            "2026-08-11T10:00:00Z",
            "--json",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == {
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
    combined = result.stdout + result.stderr
    for forbidden in (
        "RAW-PASSWORD-SENTINEL",
        "DEEPSEEK-SECRET-SENTINEL",
        "pilot@example.invalid",
        "Approved Test Name",
        "Approved Test Organization",
    ):
        assert forbidden not in combined


def test_canary_preflight_rejects_missing_checkpoint_without_echoing_credentials(
    tmp_path: Path,
) -> None:
    manifest, control, secrets = _prepared_canary(tmp_path)
    credential_path = secrets / "email_credential"
    credential = json.loads(credential_path.read_text(encoding="utf-8"))
    credential["initial_checkpoint"] = None
    _private(credential_path, json.dumps(credential).encode())

    result = subprocess.run(
        [
            str(CANARY_PREFLIGHT),
            "--manifest",
            str(manifest),
            "--run-control",
            str(control),
            "--secret-dir",
            str(secrets),
            "--repo-root",
            str(ROOT),
            "--now",
            "2026-08-11T10:00:00Z",
            "--json",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "checkpoint" in result.stderr.lower()
    assert "RAW-PASSWORD-SENTINEL" not in result.stderr
    assert "pilot@example.invalid" not in result.stderr


def test_canary_preflight_rejects_email_numeric_boundaries_before_runtime_mutation(
    tmp_path: Path,
) -> None:
    manifest, control, secrets = _prepared_canary(tmp_path)
    credential_path = secrets / "email_credential"
    credential = json.loads(credential_path.read_text(encoding="utf-8"))
    credential["port"] = 0
    _private(credential_path, json.dumps(credential).encode())

    result = subprocess.run(
        [
            str(CANARY_PREFLIGHT),
            "--manifest",
            str(manifest),
            "--run-control",
            str(control),
            "--secret-dir",
            str(secrets),
            "--repo-root",
            str(ROOT),
            "--now",
            "2026-08-11T10:00:00Z",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "email" in result.stderr.lower()
    assert "RAW-PASSWORD-SENTINEL" not in result.stderr
    assert "pilot@example.invalid" not in result.stderr


def test_canary_preflight_binds_activation_across_control_manifest_and_clock(
    tmp_path: Path,
) -> None:
    manifest, control_path, secrets = _prepared_canary(tmp_path)
    control = json.loads(control_path.read_text(encoding="utf-8"))
    control["activation_time"] = "2026-08-11T09:01:00Z"
    _private(control_path, json.dumps(control).encode())

    result = subprocess.run(
        [
            str(CANARY_PREFLIGHT),
            "--manifest",
            str(manifest),
            "--run-control",
            str(control_path),
            "--secret-dir",
            str(secrets),
            "--repo-root",
            str(ROOT),
            "--now",
            "2026-08-11T10:00:00Z",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "activation" in result.stderr.lower()


def test_canary_preflight_rejects_any_control_or_manifest_inside_repository(
    tmp_path: Path,
) -> None:
    _manifest, control, secrets = _prepared_canary(tmp_path)

    result = subprocess.run(
        [
            str(CANARY_PREFLIGHT),
            "--manifest",
            str(MANIFEST),
            "--run-control",
            str(control),
            "--secret-dir",
            str(secrets),
            "--repo-root",
            str(ROOT),
            "--now",
            "2026-08-11T10:00:00Z",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "outside" in result.stderr.lower()


def test_offline_fault_drills_emit_checksums_and_content_free_receipt(tmp_path: Path) -> None:
    output = tmp_path / "drills"

    result = subprocess.run(
        [
            str(FAULT_DRILLS),
            "--acknowledge-offline-drills",
            "--output-dir",
            str(output),
            "--repo-root",
            str(ROOT),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    receipt_path = output / "offline-fault-drills.json"
    checksums = output / "SHA256SUMS"
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(checksums.stat().st_mode) == 0o600
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["schema_version"] == "1.0"
    assert receipt["verdict"] == "pass"
    assert {item["drill"] for item in receipt["drills"]} == {
        "email_duplicate_uid",
        "email_uidvalidity_change",
        "email_attachment_quarantine",
        "model_retry_and_protocol_failure",
        "identity_restart_and_revocation",
    }
    assert all(item["passed"] is True for item in receipt["drills"])
    expected = __import__("hashlib").sha256(receipt_path.read_bytes()).hexdigest()
    assert checksums.read_text(encoding="utf-8") == (f"{expected}  offline-fault-drills.json\n")
    serialized = json.dumps(receipt, sort_keys=True)
    for forbidden in ("@", "RAW-", "Bearer ", "password", "provider_error"):
        assert forbidden not in serialized


def test_canary_evidence_sample_appends_private_hash_chained_runtime_state(
    tmp_path: Path,
) -> None:
    manifest, control_path, _secrets = _prepared_canary(tmp_path)
    control = json.loads(control_path.read_text(encoding="utf-8"))
    status_path = tmp_path / "status.json"
    _private(
        status_path,
        json.dumps(
            {
                "schema_version": "1.0",
                "captured_at": "2026-08-11T10:00:00Z",
                "source_commit": control["source_commit"],
                "composition_status": "composed",
                "manifest": {
                    "sha256": control["manifest_sha256"],
                    "site_id": "gbos.localhost",
                    "local_pilot_go": True,
                    "status": "running",
                },
                "emergency_stop": {"active": False, "containment_verified": False},
                "services": {
                    "required": ["email-poller", "model-projection-worker"],
                    "running": ["email-poller", "model-projection-worker"],
                    "missing": [],
                    "inspection_error": None,
                },
                "images": [
                    {
                        "service": "local-runtime",
                        "reference": "runtime:test",
                        "local_inspect_digest": "sha256:" + "a" * 64,
                    },
                    {
                        "service": "frappe-pwa",
                        "reference": "frappe:test",
                        "local_inspect_digest": "sha256:" + "b" * 64,
                    },
                ],
                "verdict": "running",
                "no_go_reasons": [],
            }
        ).encode(),
    )

    result = subprocess.run(
        [
            str(CANARY_EVIDENCE),
            "sample",
            "--canary-dir",
            str(manifest.parent),
            "--status-json",
            str(status_path),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    ledger = manifest.parent / "runtime-samples.jsonl"
    assert stat.S_IMODE(ledger.stat().st_mode) == 0o600
    record = json.loads(ledger.read_text(encoding="utf-8"))
    assert record["record_type"] == "status_sample"
    assert record["sequence"] == 1
    assert record["previous_sha256"] == "0" * 64
    assert len(record["record_sha256"]) == 64
    assert record["run_id"] == control["run_id"]
    assert record["manifest_sha256"] == control["manifest_sha256"]
    assert record["verdict"] == "running"
    assert record["missing_service_count"] == 0
    assert record["image_binding_count"] == 2
    assert "running" not in record
    assert "required" not in record


def test_canary_evidence_finalize_accepts_observed_short_run_and_defers_72_hour_assessment(
    tmp_path: Path,
) -> None:
    manifest, control_path, _secrets = _prepared_canary(tmp_path)
    canary_dir = manifest.parent
    control = json.loads(control_path.read_text(encoding="utf-8"))
    start = datetime(2026, 8, 11, 10, 30, tzinfo=UTC)
    common = {
        "run_id": control["run_id"],
        "source_commit": control["source_commit"],
        "manifest_sha256": control["manifest_sha256"],
    }
    samples = [
        {
            **common,
            "record_type": "status_sample",
            "observed_at": "2026-08-11T10:00:00Z",
            "status_snapshot_sha256": "1" * 64,
            "verdict": "running",
            "missing_service_count": 0,
            "emergency_stop_active": False,
            "image_binding_count": 2,
        },
        {
            **common,
            "record_type": "status_sample",
            "observed_at": "2026-08-11T11:00:00Z",
            "status_snapshot_sha256": "2" * 64,
            "verdict": "running",
            "missing_service_count": 0,
            "emergency_stop_active": False,
            "image_binding_count": 2,
        },
    ]
    checks = []
    for index, (kind, source) in enumerate(REQUIRED_CANARY_CHECKS.items(), start=1):
        checks.append(
            {
                **common,
                "record_type": "live_check",
                "observed_at": start.isoformat().replace("+00:00", "Z"),
                "kind": kind,
                "status": "pass",
                "source": source,
                "evidence_sha256": f"{index + 100:064x}",
                **(
                    {"observed_model": "deepseek-v4-flash"}
                    if kind == "model_identity_exact"
                    else {}
                ),
            }
        )
    _write_ledger(canary_dir / "runtime-samples.jsonl", samples)
    _write_ledger(canary_dir / "live-checks.jsonl", checks)

    success = subprocess.run(
        [str(CANARY_EVIDENCE), "finalize", "--canary-dir", str(canary_dir)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert success.returncode == 0, success.stderr
    evidence_dir = canary_dir / "evidence"
    evidence_path = evidence_dir / "task13-canary-evidence.json"
    summary_path = evidence_dir / "task13-canary-summary.md"
    checksums = evidence_dir / "SHA256SUMS"
    assert stat.S_IMODE(evidence_dir.stat().st_mode) == 0o700
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o600
        for path in (evidence_path, summary_path, checksums)
    )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["runtime_window"] == {
        "duration_hours": 1,
        "max_gap_seconds": 3600,
        "sample_count": 2,
    }
    assert evidence["stability_assessment"] == {
        "continuous_runtime_required": False,
        "continuous_runtime_stability": "not_assessed",
        "seventy_two_hour_run": "deferred_by_user",
    }
    assert evidence["observed_model_identity"] == "deepseek-v4-flash"
    assert evidence["scope_verdicts"] == {
        "email_deepseek_identity_local_shadow": "go",
        "formal_compliance": "no_go",
        "formal_production": "no_go",
        "external_send": "no_go",
        "kingdee": "no_go",
        "cloud_deployment": "no_go",
    }
    serialized = evidence_path.read_text(encoding="utf-8") + summary_path.read_text(
        encoding="utf-8"
    )
    for forbidden in ("@", "Bearer ", "password", "api_key", "target_ref"):
        assert forbidden not in serialized
    assert "72-hour continuous stability: deferred by user; not assessed" in serialized
    assert len(checksums.read_text(encoding="utf-8").splitlines()) == 2

    evidence_dir.rename(canary_dir / "valid-evidence")
    unhealthy_samples = [
        {**samples[0], "verdict": "degraded"},
        samples[1],
    ]
    _write_ledger(canary_dir / "runtime-samples.jsonl", unhealthy_samples)
    unhealthy = subprocess.run(
        [str(CANARY_EVIDENCE), "finalize", "--canary-dir", str(canary_dir)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert unhealthy.returncode != 0
    assert "unhealthy" in unhealthy.stderr.lower()
    assert not (canary_dir / "evidence").exists()


def test_canary_evidence_requires_two_health_samples_and_brackets_live_checks(
    tmp_path: Path,
) -> None:
    manifest, control_path, _secrets = _prepared_canary(tmp_path)
    canary_dir = manifest.parent
    control = json.loads(control_path.read_text(encoding="utf-8"))
    common = {
        "run_id": control["run_id"],
        "source_commit": control["source_commit"],
        "manifest_sha256": control["manifest_sha256"],
    }
    single_sample = [
        {
            **common,
            "record_type": "status_sample",
            "observed_at": "2026-08-11T11:00:00Z",
            "status_snapshot_sha256": "1" * 64,
            "verdict": "running",
            "missing_service_count": 0,
            "emergency_stop_active": False,
            "image_binding_count": 2,
        }
    ]
    checks = [
        {
            **common,
            "record_type": "live_check",
            "observed_at": "2026-08-11T10:30:00Z",
            "kind": kind,
            "status": "pass",
            "source": source,
            "evidence_sha256": f"{index + 100:064x}",
            **({"observed_model": "deepseek-v4-flash"} if kind == "model_identity_exact" else {}),
        }
        for index, (kind, source) in enumerate(REQUIRED_CANARY_CHECKS.items(), start=1)
    ]
    _write_ledger(canary_dir / "runtime-samples.jsonl", single_sample)
    _write_ledger(canary_dir / "live-checks.jsonl", checks)

    one_point = subprocess.run(
        [str(CANARY_EVIDENCE), "finalize", "--canary-dir", str(canary_dir)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert one_point.returncode != 0
    assert "samples" in one_point.stderr.lower()
    assert not (canary_dir / "evidence").exists()

    two_samples = [
        {**single_sample[0], "observed_at": "2026-08-11T10:00:00Z"},
        {**single_sample[0], "observed_at": "2026-08-11T11:00:00Z"},
    ]
    _write_ledger(canary_dir / "runtime-samples.jsonl", two_samples)
    checks[0]["observed_at"] = "2026-08-11T09:30:00Z"
    _write_ledger(canary_dir / "live-checks.jsonl", checks)

    unbracketed = subprocess.run(
        [str(CANARY_EVIDENCE), "finalize", "--canary-dir", str(canary_dir)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert unbracketed.returncode != 0
    assert "observed canary period" in unbracketed.stderr.lower()
    assert not (canary_dir / "evidence").exists()


def test_canary_evidence_records_only_hash_of_private_live_check_artifact(
    tmp_path: Path,
) -> None:
    manifest, control_path, _secrets = _prepared_canary(tmp_path)
    control = json.loads(control_path.read_text(encoding="utf-8"))
    artifact = tmp_path / "model-check.json"
    _private(artifact, b'{"model":"deepseek-v4-flash","result":"pass"}\n')

    result = subprocess.run(
        [
            str(CANARY_EVIDENCE),
            "record",
            "--canary-dir",
            str(manifest.parent),
            "--kind",
            "model_identity_exact",
            "--source",
            "system_query",
            "--observed-at",
            "2026-08-11T11:00:00Z",
            "--evidence-file",
            str(artifact),
            "--observed-model",
            "deepseek-v4-flash",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    record = json.loads((manifest.parent / "live-checks.jsonl").read_text(encoding="utf-8"))
    assert record["run_id"] == control["run_id"]
    assert record["kind"] == "model_identity_exact"
    assert record["source"] == "system_query"
    assert record["observed_model"] == "deepseek-v4-flash"
    assert (
        record["evidence_sha256"] == __import__("hashlib").sha256(artifact.read_bytes()).hexdigest()
    )
    serialized = json.dumps(record, sort_keys=True)
    assert "result" not in serialized
    assert str(artifact) not in serialized


def test_canary_evidence_rejects_tampered_hash_chain_and_missing_model_identity(
    tmp_path: Path,
) -> None:
    manifest, control_path, _secrets = _prepared_canary(tmp_path)
    canary_dir = manifest.parent
    control = json.loads(control_path.read_text(encoding="utf-8"))
    common = {
        "run_id": control["run_id"],
        "source_commit": control["source_commit"],
        "manifest_sha256": control["manifest_sha256"],
    }
    _write_ledger(
        canary_dir / "runtime-samples.jsonl",
        [
            {
                **common,
                "record_type": "status_sample",
                "observed_at": "2026-08-11T10:00:00Z",
                "status_snapshot_sha256": "a" * 64,
                "verdict": "running",
                "missing_service_count": 0,
                "emergency_stop_active": False,
                "image_binding_count": 2,
            }
        ],
    )
    content = (canary_dir / "runtime-samples.jsonl").read_text(encoding="utf-8")
    (canary_dir / "runtime-samples.jsonl").write_text(
        content.replace('"verdict":"running"', '"verdict":"no_go"'),
        encoding="utf-8",
    )
    os.chmod(canary_dir / "runtime-samples.jsonl", 0o600)
    _write_ledger(canary_dir / "live-checks.jsonl", [])

    result = subprocess.run(
        [str(CANARY_EVIDENCE), "finalize", "--canary-dir", str(canary_dir)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "ledger" in result.stderr.lower()
    assert not (canary_dir / "evidence").exists()
