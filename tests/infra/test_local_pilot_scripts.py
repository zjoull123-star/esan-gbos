from __future__ import annotations

import json
import os
import re
import shutil
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


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _prepare_runtime_image_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    scripts = repo / "scripts" / "local-pilot"
    infra = repo / "infra" / "local"
    scripts.mkdir(parents=True)
    infra.mkdir(parents=True)
    shutil.copy2(SCRIPTS / "build-runtime-image", scripts / "build-runtime-image")
    shutil.copy2(SCRIPTS / "lib.sh", scripts / "lib.sh")
    shutil.copy2(ROOT / "infra" / "local" / "Containerfile.runtime", infra)
    (repo / "services").mkdir()
    (repo / "services" / "runtime.py").write_text("RUNTIME = True\n", encoding="utf-8")
    (repo / "contracts").mkdir()
    (repo / "contracts" / "runtime.json").write_text("{}\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text("[project]\nname = 'runtime-test'\n", encoding="utf-8")
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (infra / "runtime-entrypoints.json").write_text(
        json.dumps({"runtime_image": "gbos-runtime:test"}),
        encoding="utf-8",
    )
    (infra / "images.lock.json").write_text(
        json.dumps(
            {
                "images": [
                    {
                        "service": "python-runtime-base",
                        "reference": "python:test@sha256:" + "1" * 64,
                    },
                    {
                        "service": "uv-builder",
                        "reference": "uv:test@sha256:" + "2" * 64,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    command_log = tmp_path / "docker-commands.jsonl"
    docker = tmp_path / "docker"
    _write_executable(
        docker,
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import pathlib\n"
        "import sys\n\n"
        "log = pathlib.Path(os.environ['DOCKER_COMMAND_LOG'])\n"
        "with log.open('a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "state = log.with_suffix('.state.json')\n"
        "args = sys.argv[1:]\n"
        "if args and args[0] == 'build':\n"
        "    values = {}\n"
        "    for index, value in enumerate(args):\n"
        "        if value == '--build-arg':\n"
        "            key, item = args[index + 1].split('=', 1)\n"
        "            values[key] = item\n"
        "    state.write_text(json.dumps(values), encoding='utf-8')\n"
        "elif args[:2] == ['image', 'inspect']:\n"
        "    values = json.loads(state.read_text(encoding='utf-8'))\n"
        "    template = args[args.index('--format') + 1]\n"
        "    override = os.environ.get('RUNTIME_TEST_LABEL_OVERRIDE')\n"
        "    if 'org.opencontainers.image.revision' in template:\n"
        "        print(override or values['RUNTIME_SOURCE_COMMIT'])\n"
        "    elif 'com.esan.gbos.runtime-source-sha256' in template:\n"
        "        print(override or values['RUNTIME_SOURCE_SHA256'])\n"
        "    else:\n"
        "        raise SystemExit(2)\n",
    )
    _write_executable(
        scripts / "record-images",
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "printf '%s\\n' record-images >> \"${DOCKER_COMMAND_LOG}\"\n",
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "tests@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Runtime Tests"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "runtime fixture"], cwd=repo, check=True)
    return repo, command_log


def _run_runtime_image_build(
    repo: Path,
    command_log: Path,
    *,
    label_override: str | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PATH"] = os.pathsep.join((str(command_log.parent), os.defpath))
    environment["DOCKER_COMMAND_LOG"] = str(command_log)
    if label_override is not None:
        environment["RUNTIME_TEST_LABEL_OVERRIDE"] = label_override
    return subprocess.run(
        [str(repo / "scripts" / "local-pilot" / "build-runtime-image"), "--confirm-network-build"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def _run_preflight(
    *args: str,
    skip_image_check: bool = True,
    docker_inspect_stub: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [str(SCRIPTS / "preflight"), "--repo-root", str(ROOT), *args]
    if skip_image_check:
        command.append("--skip-runtime-image-check")
    environment = os.environ.copy()
    if docker_inspect_stub is not None:
        environment["PATH"] = os.pathsep.join(
            (str(docker_inspect_stub.parent), environment.get("PATH", os.defpath))
        )
    return subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def _write_docker_inspect_stub(tmp_path: Path, image_lock: dict[str, object]) -> Path:
    images = image_lock.get("images")
    assert isinstance(images, list)
    inspected_images: dict[str, dict[str, object]] = {}
    for item in images:
        assert isinstance(item, dict)
        reference = item.get("reference")
        platform = item.get("platform")
        assert isinstance(reference, str)
        assert isinstance(platform, str) and "/" in platform
        operating_system, architecture = platform.split("/", 1)
        inspect_digest = item.get("local_inspect_digest")
        repo_digest = item.get("local_repo_digest")
        inspected_images[reference] = {
            "Id": inspect_digest if isinstance(inspect_digest, str) else "sha256:" + "0" * 64,
            "RepoDigests": [repo_digest] if isinstance(repo_digest, str) else [],
            "Os": operating_system,
            "Architecture": architecture,
        }

    stub = tmp_path / "docker"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import sys\n\n"
        f"images = {json.dumps(inspected_images, sort_keys=True)}\n\n"
        "if sys.argv[1:3] != ['image', 'inspect'] or len(sys.argv) != 4:\n"
        "    raise SystemExit(2)\n"
        "image = images.get(sys.argv[3])\n"
        "if image is None:\n"
        "    raise SystemExit(1)\n"
        "print(json.dumps([image]))\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return stub


def _prepare_emergency_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    scripts = repo / "scripts" / "local-pilot"
    infra = repo / "infra" / "local"
    scripts.mkdir(parents=True)
    infra.mkdir(parents=True)
    for name in ("lib.sh", "containment.py", "emergency-stop", "clear-emergency-stop"):
        shutil.copy2(SCRIPTS / name, scripts / name)
    (infra / "compose.yml").write_text("name: emergency-test\nservices: {}\n", encoding="utf-8")
    docker_log = tmp_path / "docker.jsonl"
    docker = tmp_path / "docker"
    _write_executable(
        docker,
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import pathlib\n"
        "import sys\n\n"
        "log = pathlib.Path(os.environ['EMERGENCY_DOCKER_LOG'])\n"
        "with log.open('a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "args = sys.argv[1:]\n"
        "if 'stop' in args:\n"
        "    raise SystemExit(int(os.environ.get('EMERGENCY_STOP_CODE', '0')))\n"
        "if 'ps' in args:\n"
        "    if os.environ.get('EMERGENCY_PS_FAIL') == '1':\n"
        "        raise SystemExit(3)\n"
        "    running = os.environ.get('EMERGENCY_RUNNING_SERVICE')\n"
        "    if running:\n"
        "        print(running)\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(2)\n",
    )
    return repo, docker_log


def _run_emergency(
    repo: Path,
    docker_log: Path,
    *,
    stop_code: int = 0,
    ps_fail: bool = False,
    running_service: str | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PATH"] = os.pathsep.join((str(docker_log.parent), os.defpath))
    environment["EMERGENCY_DOCKER_LOG"] = str(docker_log)
    environment["EMERGENCY_STOP_CODE"] = str(stop_code)
    if ps_fail:
        environment["EMERGENCY_PS_FAIL"] = "1"
    if running_service is not None:
        environment["EMERGENCY_RUNNING_SERVICE"] = running_service
    return subprocess.run(
        [str(repo / "scripts" / "local-pilot" / "emergency-stop")],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_local_pilot_preflight_compiles_with_default_python3(tmp_path: Path) -> None:
    python3 = shutil.which("python3", path=os.defpath)
    assert python3 is not None
    environment = os.environ.copy()
    environment["PYTHONPYCACHEPREFIX"] = str(tmp_path)

    result = subprocess.run(
        [python3, "-m", "py_compile", str(SCRIPTS / "preflight.py")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr


def test_ci_compiles_tracked_python_before_static_checks() -> None:
    workflow = _read(ROOT / ".github" / "workflows" / "ci.yml")
    ruff_job = workflow.split("  ruff:\n", 1)[1].split("\n  compose-config:\n", 1)[0]

    tracked_files = ruff_job.index("git ls-files -z")
    compileall = ruff_job.index("python -m compileall")
    ruff_check = ruff_job.index("ruff check .")
    mypy = ruff_job.index("mypy")

    assert tracked_files < compileall < ruff_check < mypy
    assert "xargs -0" in ruff_job
    for directory in ("apps", "services", "scripts", "tests"):
        assert f"'{directory}/**/*.py'" in ruff_job


def test_operational_scripts_are_executable_and_shell_safe() -> None:
    for name in (
        "start",
        "start-synthetic",
        "start-email-deepseek-canary",
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
        "prepare-email-deepseek-canary",
        "canary-preflight",
        "canary-evidence",
        "run-offline-fault-drills",
        "run-retention",
    ):
        path = SCRIPTS / name
        content = _read(path)
        assert stat.S_IMODE(path.stat().st_mode) & stat.S_IXUSR
        assert content.startswith("#!")
        assert "set -x" not in content
        if content.startswith("#!/usr/bin/env bash"):
            assert "set -euo pipefail" in content
    status = _read(SCRIPTS / "status")
    assert 'python3 "${SCRIPT_DIR}/status.py"' in status
    assert "未组合，不可启动" not in status


def test_runtime_image_build_rejects_dirty_runtime_sources_before_docker(
    tmp_path: Path,
) -> None:
    repo, command_log = _prepare_runtime_image_repo(tmp_path)
    (repo / "services" / "runtime.py").write_text("RUNTIME = False\n", encoding="utf-8")

    result = _run_runtime_image_build(repo, command_log)

    assert result.returncode == 65
    assert "runtime image inputs must be tracked and clean" in result.stderr
    assert not command_log.exists()


def test_runtime_image_build_labels_exact_clean_source_before_recording(
    tmp_path: Path,
) -> None:
    repo, command_log = _prepare_runtime_image_repo(tmp_path)
    expected_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    first = _run_runtime_image_build(repo, command_log)

    assert first.returncode == 0, first.stderr
    entries = command_log.read_text(encoding="utf-8").splitlines()
    commands = [json.loads(entry) for entry in entries[:-1]]
    build = next(command for command in commands if command[0] == "build")
    build_args = [build[index + 1] for index, item in enumerate(build) if item == "--build-arg"]
    assert f"RUNTIME_SOURCE_COMMIT={expected_commit}" in build_args
    source_digest = next(
        item.split("=", 1)[1] for item in build_args if item.startswith("RUNTIME_SOURCE_SHA256=")
    )
    assert re.fullmatch(r"[0-9a-f]{64}", source_digest)
    revision_inspect = next(
        index
        for index, command in enumerate(commands)
        if command[:2] == ["image", "inspect"]
        and "org.opencontainers.image.revision" in command[command.index("--format") + 1]
    )
    digest_inspect = next(
        index
        for index, command in enumerate(commands)
        if command[:2] == ["image", "inspect"]
        and "com.esan.gbos.runtime-source-sha256" in command[command.index("--format") + 1]
    )
    assert revision_inspect < len(commands)
    assert digest_inspect < len(commands)
    assert entries[-1] == "record-images"

    lock = repo / "infra" / "local" / "images.lock.json"
    lock.write_text(lock.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    command_log.unlink()
    second = _run_runtime_image_build(repo, command_log)

    assert second.returncode == 0, second.stderr
    second_commands = [
        json.loads(entry) for entry in command_log.read_text(encoding="utf-8").splitlines()[:-1]
    ]
    second_build = next(command for command in second_commands if command[0] == "build")
    second_build_args = [
        second_build[index + 1] for index, item in enumerate(second_build) if item == "--build-arg"
    ]
    assert f"RUNTIME_SOURCE_SHA256={source_digest}" in second_build_args


def test_runtime_image_build_refuses_mismatched_label_before_recording(
    tmp_path: Path,
) -> None:
    repo, command_log = _prepare_runtime_image_repo(tmp_path)

    result = _run_runtime_image_build(repo, command_log, label_override="mismatch")

    assert result.returncode == 65
    assert "revision label does not match repository HEAD" in result.stderr
    assert "record-images" not in command_log.read_text(encoding="utf-8")


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
    assert 'GBOS_IDENTITY_RESOLUTION_KILL_SWITCH="true"' in start
    assert 'GBOS_EXTERNAL_SEND_ENABLED="false"' in start
    assert "EMERGENCY_STOP" in start
    assert "export GBOS_LOCAL_PILOT_MANIFEST" in start
    assert "${GBOS_LOCAL_PILOT_MANIFEST:-./local-pilot-manifest.json}" in compose_file
    assert "--skip-runtime-image-check" not in start
    assert "--canary-control" in start
    canary_preflight = start.index('"${SCRIPT_DIR}/canary-preflight"')
    assert secrets < canary_preflight < compose
    assert "--profile observability" in start
    assert "--profile model-projection" in start
    assert "--profile model)" not in start
    assert 'GBOS_COMMUNICATION_DRAFT_KILL_SWITCH="true"' in start


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
        "identity",
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
    volume_init = "compose --profile runtime run --rm --no-deps runtime-volume-init"
    bootstrap = "run --rm --no-deps frappe-materializer-bootstrap"
    runtime_up = 'compose "${profile_args[@]}" up -d --wait "${synthetic_services[@]}"'
    demo_bootstrap = '"${SCRIPT_DIR}/bootstrap-synthetic-user" --acknowledge-synthetic'
    assert start.index(volume_init) < start.index(migration)
    assert start.index(migration) < start.index(bootstrap) < start.index(runtime_up)
    assert start.index(runtime_up) < start.index(demo_bootstrap)


def test_email_deepseek_canary_start_is_explicit_and_accepts_no_secret_values() -> None:
    script = _read(SCRIPTS / "start-email-deepseek-canary")

    assert "--acknowledge-real-email-and-model" in script
    assert "pilot-manifest.json" in script
    assert "canary-run.json" in script
    assert "--manifest" in script
    assert "--canary-control" in script
    assert "prepare-email-deepseek-canary" not in script
    for forbidden in ("password", "api-key", "token", "secret"):
        assert f"--{forbidden}" not in script.lower()


def test_retention_wrapper_is_dry_run_first_and_requires_double_opt_in_for_deletion() -> None:
    script = _read(SCRIPTS / "run-retention")

    assert "--dry-run" in script
    assert "--execute-expired-data" in script
    assert "--acknowledge-expired-local-data-deletion" in script
    assert 'GBOS_RETENTION_ENABLED="true"' in script
    assert 'GBOS_RETENTION_DRY_RUN="true"' in script
    assert (
        "compose --profile runtime --profile retention run --rm --no-deps retention-worker"
        in script
    )
    execute = script.index("--execute-expired-data)")
    acknowledge = script.index("--acknowledge-expired-local-data-deletion)")
    compose_run = script.index("run --rm --no-deps retention-worker")
    volume_init = script.index("run --rm --no-deps runtime-volume-init")
    assert execute < compose_run
    assert acknowledge < compose_run
    assert volume_init < compose_run
    assert "EMERGENCY_STOP" in script
    assert " down " not in re.sub(r"\s+", " ", script)


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
        "identity-resolution-worker",
        "model-projection-worker",
        "communication-draft-worker",
        "media-worker",
        "agent-worker",
        "materialization-worker",
        "retention-worker",
    ):
        assert service in emergency
    stop_command = emergency[emergency.index("docker compose") :]
    assert " postgres" not in stop_command
    assert " mariadb" not in stop_command
    assert " local-pilot-evidence-cas" not in stop_command
    assert 'containment.py" activate' in emergency
    assert "whatsapp-poller" not in emergency


def test_emergency_stop_verifies_containment_and_binds_a_private_receipt(
    tmp_path: Path,
) -> None:
    repo, docker_log = _prepare_emergency_repo(tmp_path)

    result = _run_emergency(repo, docker_log)

    assert result.returncode == 0, result.stderr
    runtime = repo / ".runtime" / "local-pilot"
    latch = json.loads((runtime / "EMERGENCY_STOP").read_text(encoding="utf-8"))
    receipt_path = runtime / "containment-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["schema_version"] == "1.0"
    assert receipt["verified"] is True
    assert receipt["latch_id"] == latch["latch_id"]
    assert receipt["running_services"] == []
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert "verified" in result.stdout.lower()


def test_emergency_stop_never_claims_success_when_containment_cannot_be_verified(
    tmp_path: Path,
) -> None:
    repo, docker_log = _prepare_emergency_repo(tmp_path)

    result = _run_emergency(repo, docker_log, stop_code=1, ps_fail=True)

    assert result.returncode != 0
    runtime = repo / ".runtime" / "local-pilot"
    assert (runtime / "EMERGENCY_STOP").is_file()
    receipt = json.loads((runtime / "containment-receipt.json").read_text(encoding="utf-8"))
    assert receipt["verified"] is False
    assert "verified" not in result.stdout.lower()


def test_clear_emergency_stop_requires_a_matching_verified_receipt(tmp_path: Path) -> None:
    repo, docker_log = _prepare_emergency_repo(tmp_path)
    runtime = repo / ".runtime" / "local-pilot"
    runtime.mkdir(parents=True)
    (runtime / "EMERGENCY_STOP").write_text(
        json.dumps({"schema_version": "1.0", "latch_id": "new-latch"}),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["PATH"] = os.pathsep.join((str(docker_log.parent), os.defpath))

    refused = subprocess.run(
        [
            str(repo / "scripts" / "local-pilot" / "clear-emergency-stop"),
            "--acknowledge-contained",
        ],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert refused.returncode != 0
    assert (runtime / "EMERGENCY_STOP").is_file()

    (runtime / "containment-receipt.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "latch_id": "new-latch",
                "verified": True,
                "running_services": [],
            }
        ),
        encoding="utf-8",
    )
    cleared = subprocess.run(
        [
            str(repo / "scripts" / "local-pilot" / "clear-emergency-stop"),
            "--acknowledge-contained",
        ],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert cleared.returncode == 0, cleared.stderr
    assert not (runtime / "EMERGENCY_STOP").exists()


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
    assert result.returncode == 78
    assert "local_pilot_go must be true" in result.stderr


def test_synthetic_runtime_preflight_accepts_recorded_images_without_waiving_go(
    tmp_path: Path,
) -> None:
    image_lock = json.loads(_read(IMAGE_LOCK))
    docker_inspect_stub = _write_docker_inspect_stub(tmp_path, image_lock)

    result = _run_preflight(
        "--manifest",
        str(MANIFEST),
        "--synthetic",
        "--require-runtime-images",
        skip_image_check=False,
        docker_inspect_stub=docker_inspect_stub,
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

    candidate_root = tmp_path / "repo"
    (candidate_root / "infra" / "local").mkdir(parents=True)
    for directory in ("apps", "contracts", "services"):
        (candidate_root / directory).symlink_to(ROOT / directory, target_is_directory=True)
    shutil.copy2(
        ROOT / "infra" / "local" / "Containerfile.runtime",
        candidate_root / "infra" / "local" / "Containerfile.runtime",
    )
    entrypoints = json.loads(_read(ROOT / "infra" / "local" / "runtime-entrypoints.json"))
    entrypoints["composition"].update(
        {
            "status": "not_composed",
            "frappe_pwa": "blocked_current_source_image_refresh_required",
        }
    )
    (candidate_root / "infra" / "local" / "runtime-entrypoints.json").write_text(
        json.dumps(entrypoints),
        encoding="utf-8",
    )

    result = _run_preflight(
        "--repo-root",
        str(candidate_root),
        "--manifest",
        str(candidate),
        "--image-lock",
        str(IMAGE_LOCK),
    )

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
    docker_inspect_stub = _write_docker_inspect_stub(tmp_path, lock)
    postgres = next(item for item in lock["images"] if item["service"] == "postgres")
    postgres["local_inspect_digest"] = "sha256:" + "2" * 64
    candidate = tmp_path / "images.lock.json"
    candidate.write_text(json.dumps(lock), encoding="utf-8")

    result = _run_preflight(
        "--image-lock",
        str(candidate),
        skip_image_check=False,
        docker_inspect_stub=docker_inspect_stub,
    )

    assert result.returncode != 0
    assert "local inspect ID mismatch for postgres" in result.stderr


def test_preflight_rejects_local_repo_digest_mismatch(tmp_path: Path) -> None:
    lock = json.loads(_read(IMAGE_LOCK))
    docker_inspect_stub = _write_docker_inspect_stub(tmp_path, lock)
    postgres = next(item for item in lock["images"] if item["service"] == "postgres")
    postgres["local_repo_digest"] = "pgvector/pgvector@sha256:" + "3" * 64
    candidate = tmp_path / "images.lock.json"
    candidate.write_text(json.dumps(lock), encoding="utf-8")

    result = _run_preflight(
        "--image-lock",
        str(candidate),
        skip_image_check=False,
        docker_inspect_stub=docker_inspect_stub,
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
