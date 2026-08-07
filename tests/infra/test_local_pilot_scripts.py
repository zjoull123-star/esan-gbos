from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[2]
SCRIPTS = ROOT / "scripts" / "local-pilot"
MANIFEST = ROOT / "infra" / "local" / "local-pilot-manifest.json"
IMAGE_LOCK = ROOT / "infra" / "local" / "images.lock.json"


def _read(path: Path) -> str:
    assert path.is_file(), f"required local-pilot asset is missing: {path.relative_to(ROOT)}"
    return path.read_text(encoding="utf-8")


def _run_preflight(
    *args: str,
    skip_image_check: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = [str(SCRIPTS / "preflight"), "--repo-root", str(ROOT), *args]
    if skip_image_check:
        command.append("--skip-runtime-image-check")
    return subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_operational_scripts_are_executable_and_shell_safe() -> None:
    for name in (
        "start",
        "start-synthetic",
        "status",
        "stop",
        "emergency-stop",
        "prepare-secrets",
        "inspect-images",
        "preflight",
        "migrate",
        "render-config",
        "record-images",
        "build-runtime-image",
        "build-frappe-image",
        "bootstrap-synthetic-user",
    ):
        path = SCRIPTS / name
        content = _read(path)
        assert stat.S_IMODE(path.stat().st_mode) & stat.S_IXUSR
        assert content.startswith("#!")
        assert "set -x" not in content
        if content.startswith("#!/usr/bin/env bash"):
            assert "set -euo pipefail" in content
    assert "未组合，不可启动" in _read(SCRIPTS / "status")


def test_synthetic_user_bootstrap_is_explicit_image_gated_and_never_starts_stack() -> None:
    script = _read(SCRIPTS / "bootstrap-synthetic-user")

    assert "--acknowledge-synthetic" in script
    assert "--synthetic" in script
    assert "--require-runtime-images" in script
    assert "--require-go" not in script
    assert script.index("--require-runtime-images") < script.index(
        "run --rm --no-deps frappe-synthetic-bootstrap"
    )
    assert "--profile synthetic-bootstrap" in script
    assert " up " not in re.sub(r"\s+", " ", script)
    assert "frappe_demo_password" not in script


def test_start_runs_fail_closed_preflight_before_secret_or_compose_actions() -> None:
    start = _read(SCRIPTS / "start")
    compose_file = _read(ROOT / "infra" / "local" / "compose.yml")

    preflight = start.index('"${SCRIPT_DIR}/preflight"')
    secrets = start.index('"${SCRIPT_DIR}/prepare-secrets"')
    compose = start.index('compose "${profile_args[@]}" config --quiet')
    assert preflight < secrets < compose
    assert "--require-go" in start
    assert 'GBOS_CONNECTOR_KILL_SWITCH="true"' in start
    assert 'GBOS_MODEL_KILL_SWITCH="true"' in start
    assert 'GBOS_EXTERNAL_SEND_ENABLED="false"' in start
    assert "EMERGENCY_STOP" in start
    assert "export GBOS_LOCAL_PILOT_MANIFEST" in start
    assert "${GBOS_LOCAL_PILOT_MANIFEST:-./local-pilot-manifest.json}" in compose_file
    assert "--skip-runtime-image-check" not in start


def test_synthetic_start_is_explicit_image_gated_and_never_enables_external_profiles() -> None:
    start = _read(SCRIPTS / "start-synthetic")

    assert "--acknowledge-synthetic" in start
    assert "--synthetic" in start
    assert "--require-runtime-images" in start
    assert "--require-go" not in start
    assert "--synthetic-runtime" in start
    assert "prepare-secrets" in start
    assert "profile_args=(--profile runtime)" in start
    for forbidden_profile in (
        "connectors",
        "email",
        "wecom",
        "whatsapp",
        "media",
        "model",
        "tunnel",
    ):
        assert f'--profile "{forbidden_profile}"' not in start
    assert 'GBOS_CONNECTOR_KILL_SWITCH="true"' in start
    assert 'GBOS_MODEL_KILL_SWITCH="true"' in start
    assert 'GBOS_MODEL_PROJECTION_KILL_SWITCH="true"' in start
    assert 'GBOS_DEEPSEEK_EGRESS_ENABLED="false"' in start
    assert 'GBOS_EXTERNAL_SEND_ENABLED="false"' in start
    for core_service in (
        "context-api",
        "agent-api",
        "observer-api",
        "frappe-worker",
        "frappe-scheduler",
        "pwa",
    ):
        assert core_service in start
    assert "materialization-worker" not in start
    migration = "compose --profile runtime run --rm migrations"
    bootstrap = "run --rm --no-deps frappe-materializer-bootstrap"
    runtime_up = 'compose "${profile_args[@]}" up -d --wait "${synthetic_services[@]}"'
    demo_bootstrap = '"${SCRIPT_DIR}/bootstrap-synthetic-user" --acknowledge-synthetic'
    assert start.index(migration) < start.index(bootstrap) < start.index(runtime_up)
    assert start.index(runtime_up) < start.index(demo_bootstrap)


def test_stop_preserves_volumes_and_emergency_stop_preserves_state_services() -> None:
    stop = _read(SCRIPTS / "stop")
    emergency = _read(SCRIPTS / "emergency-stop")

    assert " down " in re.sub(r"\s+", " ", stop)
    executable_stop = "\n".join(
        line for line in stop.splitlines() if not line.lstrip().startswith("#")
    )
    assert "--volumes" not in executable_stop
    assert re.search(r"(^|\s)-v(\s|$)", executable_stop) is None
    assert "remove-orphans" in stop
    assert "cleanup_secret_dir" in stop

    assert " down " not in re.sub(r"\s+", " ", emergency)
    assert "docker compose" in emergency
    assert " stop " in re.sub(r"\s+", " ", emergency)
    for service in (
        "cloudflared",
        "email-poller",
        "wecom-poller",
        "connector-worker",
        "model-projection-worker",
        "media-worker",
        "agent-worker",
        "materialization-worker",
    ):
        assert service in emergency
    stop_command = emergency[emergency.index("docker compose") :]
    assert " postgres" not in stop_command
    assert " mariadb" not in stop_command
    assert " local-pilot-evidence-cas" not in stop_command
    assert "EMERGENCY_STOP" in emergency
    assert "whatsapp-poller" not in emergency


def test_keychain_secret_materialization_is_non_logging_and_mode_0600() -> None:
    script = _read(SCRIPTS / "prepare-secrets")
    library = _read(SCRIPTS / "lib.sh")

    assert "umask 077" in script
    assert "/usr/bin/security" in script
    assert "find-generic-password" in script
    assert "-w" in script
    assert "chmod 600" in script
    assert "mktemp -d" in script
    assert "printf '%s' \"${secret_value}\"" in script
    assert 'echo "${secret_value}"' not in script
    assert "set -x" not in script
    assert "keychain://" in script
    assert 'secret_tmp_root="${secret_tmp_root%/}"' in script
    assert 'find "${secret_dir}"' not in library


def test_image_inspection_reports_id_and_repo_digests_without_pulling() -> None:
    script = _read(SCRIPTS / "inspect-images")

    assert '"docker", "image", "inspect"' in script
    assert '"image_id"' in script
    assert '"repo_digests"' in script
    assert "docker pull" not in script


def test_preflight_references_governed_manifest_schema_and_disabled_manifest_fails_go() -> None:
    script = _read(SCRIPTS / "preflight.py")
    manifest = json.loads(_read(MANIFEST))

    assert "contracts/local_pilot/local-pilot-manifest-v1.0.schema.json" in script
    assert "infra/local/images.lock.json" in script
    assert manifest["mode"] == "local_pilot"
    assert manifest["production_go"] is False
    assert manifest["local_pilot_go"] is False
    assert all(value is False for value in manifest["capabilities"].values())
    assert manifest["deepseek"]["enabled"] is False
    assert manifest["deepseek"]["kill_switch"] is True
    assert all(not channel["enabled"] for channel in manifest["channels"].values())

    result = _run_preflight("--manifest", str(MANIFEST), "--require-go")
    assert result.returncode != 0
    assert "local_pilot_go must be true" in result.stderr


def test_synthetic_runtime_preflight_accepts_recorded_images_without_waiving_go() -> None:
    result = _run_preflight(
        "--manifest",
        str(MANIFEST),
        "--synthetic",
        "--require-runtime-images",
    )

    assert result.returncode == 0
    assert "passed without enabling any capability" in result.stdout
    assert "declared composition is not runtime verified" not in result.stderr


def test_preflight_rejects_placeholder_media_hashes(tmp_path: Path) -> None:
    manifest = json.loads(_read(MANIFEST))
    manifest["local_pilot_go"] = True
    manifest["local_pilot_status"] = "ready"
    manifest["channels"]["media"].update(
        {
            "enabled": True,
            "activation_time": "2026-08-08T00:00:00Z",
            "ffmpeg_sha256": "0" * 64,
            "whisper_model_sha256": "f" * 64,
        }
    )
    candidate = tmp_path / "manifest.json"
    candidate.write_text(json.dumps(manifest), encoding="utf-8")

    result = _run_preflight("--manifest", str(candidate))

    assert result.returncode != 0
    assert "placeholder" in result.stderr.lower()


def test_preflight_fails_closed_when_runtime_composition_is_unavailable(tmp_path: Path) -> None:
    manifest = json.loads(_read(MANIFEST))
    manifest["local_pilot_go"] = True
    manifest["local_pilot_status"] = "ready"
    candidate = tmp_path / "manifest.json"
    candidate.write_text(json.dumps(manifest), encoding="utf-8")

    result = _run_preflight("--manifest", str(candidate))

    assert result.returncode != 0
    assert "未组合，不可启动" in result.stderr
    assert "declared composition is not runtime verified" in result.stderr


def test_preflight_rejects_null_digest_for_required_image(tmp_path: Path) -> None:
    lock = json.loads(_read(IMAGE_LOCK))
    mariadb = next(item for item in lock["images"] if item["service"] == "mariadb")
    mariadb["local_inspect_digest"] = None
    mariadb["local_repo_digest"] = None
    candidate = tmp_path / "images.lock.json"
    candidate.write_text(json.dumps(lock), encoding="utf-8")

    result = _run_preflight("--image-lock", str(candidate))

    assert result.returncode != 0
    assert "image mariadb local_inspect_digest is required" in result.stderr
    assert "image mariadb local_repo_digest is required" in result.stderr
    assert "image local-runtime local_inspect_digest is required" not in result.stderr
    assert "image local-runtime local_repo_digest is required" not in result.stderr


def test_preflight_requires_digest_for_enabled_tunnel_image(tmp_path: Path) -> None:
    manifest = json.loads(_read(MANIFEST))
    manifest["local_pilot_go"] = True
    manifest["local_pilot_status"] = "ready"
    manifest["channels"]["whatsapp"].update(
        {
            "enabled": True,
            "activation_time": "2026-08-08T00:00:00Z",
            "credential_ref": "keychain://com.esan.gbos.local-pilot/whatsapp",
            "named_tunnel_ref": "cloudflare://tunnel/esan-gbos-local-pilot",
        }
    )
    candidate = tmp_path / "manifest.json"
    candidate.write_text(json.dumps(manifest), encoding="utf-8")

    result = _run_preflight("--manifest", str(candidate))

    assert result.returncode != 0
    assert "image cloudflared local_inspect_digest is required" in result.stderr
    assert "image cloudflared local_repo_digest is required" in result.stderr


def test_preflight_rejects_stale_local_inspect_id(tmp_path: Path) -> None:
    lock = json.loads(_read(IMAGE_LOCK))
    postgres = next(item for item in lock["images"] if item["service"] == "postgres")
    postgres["local_inspect_digest"] = "sha256:" + "2" * 64
    candidate = tmp_path / "images.lock.json"
    candidate.write_text(json.dumps(lock), encoding="utf-8")

    result = _run_preflight(
        "--image-lock",
        str(candidate),
        skip_image_check=False,
    )

    assert result.returncode != 0
    assert "local inspect ID mismatch for postgres" in result.stderr


def test_preflight_rejects_local_repo_digest_mismatch(tmp_path: Path) -> None:
    lock = json.loads(_read(IMAGE_LOCK))
    postgres = next(item for item in lock["images"] if item["service"] == "postgres")
    postgres["local_repo_digest"] = "pgvector/pgvector@sha256:" + "3" * 64
    candidate = tmp_path / "images.lock.json"
    candidate.write_text(json.dumps(lock), encoding="utf-8")

    result = _run_preflight(
        "--image-lock",
        str(candidate),
        skip_image_check=False,
    )

    assert result.returncode != 0
    assert "local RepoDigest mismatch for postgres" in result.stderr


def test_preflight_requires_remote_reference_digest(tmp_path: Path) -> None:
    lock = json.loads(_read(IMAGE_LOCK))
    mariadb = next(item for item in lock["images"] if item["service"] == "mariadb")
    mariadb["reference"] = "mariadb:11.8"
    candidate = tmp_path / "images.lock.json"
    candidate.write_text(json.dumps(lock), encoding="utf-8")

    result = _run_preflight("--image-lock", str(candidate))

    assert result.returncode != 0
    assert "remote image mariadb reference must include @sha256" in result.stderr


def test_preflight_requires_frozen_arm64_build_inputs_and_platform(tmp_path: Path) -> None:
    lock = json.loads(_read(IMAGE_LOCK))
    services = {item["service"]: item for item in lock["images"]}
    assert services["python-runtime-base"]["platform"] == "linux/arm64"
    assert services["uv-builder"]["platform"] == "linux/arm64"

    services["python-runtime-base"]["platform"] = "linux/amd64"
    candidate = tmp_path / "images.lock.json"
    candidate.write_text(json.dumps(lock), encoding="utf-8")

    result = _run_preflight("--image-lock", str(candidate))

    assert result.returncode != 0
    assert "python-runtime-base platform must be linux/arm64" in result.stderr


def test_preflight_binds_containerfile_build_inputs_to_image_lock(tmp_path: Path) -> None:
    lock = json.loads(_read(IMAGE_LOCK))
    python_base = next(item for item in lock["images"] if item["service"] == "python-runtime-base")
    python_base["reference"] = "python:3.14.2-slim-bookworm@sha256:" + "4" * 64
    candidate = tmp_path / "images.lock.json"
    candidate.write_text(json.dumps(lock), encoding="utf-8")

    result = _run_preflight("--image-lock", str(candidate))

    assert result.returncode != 0
    assert "Containerfile PYTHON_BASE_IMAGE does not match image lock" in result.stderr


def test_scripts_contain_no_embedded_secret_shaped_values() -> None:
    secret_pattern = re.compile(
        r"(?:sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|"
        r"xox[baprs]-[A-Za-z0-9-]{20,}|-----BEGIN [A-Z ]+PRIVATE KEY-----)"
    )
    for path in SCRIPTS.iterdir():
        if path.is_file():
            assert secret_pattern.search(path.read_text(encoding="utf-8")) is None

    assert os.environ.get("DEEPSEEK_API_KEY") is None or "DEEPSEEK_API_KEY" not in _read(
        SCRIPTS / "start"
    )
