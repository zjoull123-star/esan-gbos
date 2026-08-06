from __future__ import annotations

import json
import re
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
COMPOSE_FILE = REPO_ROOT / "infra" / "dev" / "compose.yml"
ENV_EXAMPLE = REPO_ROOT / "infra" / "dev" / ".env.example"
UPSTREAM_APPS = REPO_ROOT / "infra" / "dev" / "apps.upstream.json"
FINAL_CONTAINERFILE = REPO_ROOT / "infra" / "dev" / "Containerfile.final"
REALTIME_PACKAGE = REPO_ROOT / "infra" / "dev" / "realtime-runtime" / "package.json"
REALTIME_LOCK = REPO_ROOT / "infra" / "dev" / "realtime-runtime" / "package-lock.json"
FRONTEND_WORKSPACE = REPO_ROOT / "apps" / "esan_gbos" / "frontend" / "pnpm-workspace.yaml"
GITLEAKS_CONFIG = REPO_ROOT / ".gitleaks.toml"
TRIVY_WAIVERS = REPO_ROOT / "security" / "trivy-gate01-ignore.yaml"
NGINX_TEMPLATE = REPO_ROOT / "infra" / "dev" / "nginx" / "frappe.conf.template"
OBSERVER_CONTRACT_CHECK = REPO_ROOT / "services" / "observer" / "contract_check.py"
SCRIPTS_DIR = REPO_ROOT / "scripts" / "dev"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

ERPNext_IMAGE = (
    "frappe/erpnext:v16.31.0"
    "@sha256:473407805828fd0afa0df15ae72c17a0030113e0cb0ea24be0ec0d91d822b392"
)
MARIADB_IMAGE = (
    "mariadb:11.8@sha256:d9f7eb2637296652f24b484afd5d246f759f49f5babcadc6a9e344c9acb75fbf"
)
REDIS_IMAGE = (
    "redis:6.2-alpine@sha256:ec5e187c913d422cdf60f4216a5fdfb95246792c6de6fe21ff5bed75cbfc8c23"
)
POSTGRES_IMAGE = (
    "pgvector/pgvector:0.8.2-pg17-bookworm"
    "@sha256:feb68f4f15446397d8cac7f4fe48fe4586de83160d1fc48b46283312d1a33966"
)


def read_required(path: Path) -> str:
    assert path.is_file(), f"required file is missing: {path.relative_to(REPO_ROOT)}"
    return path.read_text(encoding="utf-8")


def test_compose_uses_frozen_images_and_profiles() -> None:
    compose = read_required(COMPOSE_FILE)

    assert 'frappe_docker: "v3.2.2@3061850feface8fbbad15b5dc08a110c596107cb"' in compose
    assert ERPNext_IMAGE in compose
    assert MARIADB_IMAGE in compose
    assert REDIS_IMAGE in compose
    assert POSTGRES_IMAGE in compose
    assert re.search(r"profiles:\s*\[?[\s\"']*core", compose)
    assert re.search(r"profiles:\s*\[?[\s\"']*observer", compose)


def test_compose_limits_published_ports_to_loopback() -> None:
    compose = read_required(COMPOSE_FILE)

    assert "0.0.0.0:" not in compose
    published_ports = [
        line.strip().lstrip("-").strip().strip("\"'")
        for line in compose.splitlines()
        if re.search(r":\d{2,5}(?:/tcp)?[\"']?\s*$", line) and "-" in line
    ]
    assert published_ports
    assert all(port.startswith("127.0.0.1:") for port in published_ports)


def test_compose_has_persistent_state_and_healthchecks() -> None:
    compose = read_required(COMPOSE_FILE)

    for volume in (
        "db-data",
        "redis-cache-data",
        "redis-queue-data",
        "sites",
        "logs",
        "observer-postgres-data",
    ):
        assert re.search(rf"(?m)^  {re.escape(volume)}:\s*$", compose)

    assert compose.count("healthcheck:") >= 5
    assert "headers={'Host': '${SITE_NAME:-gbos.localhost}'}" in compose
    assert "wget -qO-" not in compose
    assert "curl --fail --silent --show-error" in compose


def test_observer_profile_is_contract_and_connectivity_only() -> None:
    compose = read_required(COMPOSE_FILE)
    check = read_required(OBSERVER_CONTRACT_CHECK)

    assert "observer-contract-check:" in compose
    assert "../../contracts:/contracts:ro" in compose
    assert "../../services/observer:/observer:ro" in compose
    assert "condition: service_healthy" in compose
    assert "./observer/init:/docker-entrypoint-initdb.d:ro" in compose
    assert "select extversion from pg_extension where extname = 'vector'" in compose
    assert "grep -Fx '0.8.2'" in compose
    assert "socket.create_connection" in check
    for contract in (
        "canonical-observation-event.schema.json",
        "evidence-ref.schema.json",
        "extracted-fact.schema.json",
        "draft-mutation.schema.json",
        "approved-command.schema.json",
        "connector-checkpoint.schema.json",
    ):
        assert contract in check
    assert "kingdee" not in check.lower()
    assert "model" not in check.lower()
    assert "webhook" not in check.lower()


def test_compose_requires_secrets_instead_of_defaulting_passwords() -> None:
    compose = read_required(COMPOSE_FILE)

    assert "${DB_ROOT_PASSWORD:?" in compose
    assert "${ADMIN_PASSWORD:?" in compose
    assert "${OBSERVER_POSTGRES_PASSWORD:?" in compose
    assert not re.search(
        r"\$\{(?:DB_ROOT_PASSWORD|ADMIN_PASSWORD|OBSERVER_POSTGRES_PASSWORD):-",
        compose,
    )


def test_compose_defaults_to_the_four_app_final_gate() -> None:
    compose = read_required(COMPOSE_FILE)
    env_example = read_required(ENV_EXAMPLE)

    assert 'APP_LIST: "${APP_LIST:-erpnext,crm,esan_gbos}"' in compose
    assert "for app in $$(echo \"$$APP_LIST\" | tr ',' ' ')" in compose
    assert 'if ! grep -Fxq "$$app" sites/apps.txt; then' in compose
    assert "printf '%s\\n' \"$$app\" >> sites/apps.txt;" in compose
    assert 'bench --site "$$SITE_NAME" install-app "$$app"' in compose
    assert 'bench --site "$$SITE_NAME" migrate;' in compose
    assert 'bench --site "$$SITE_NAME" list-apps --format json' in compose
    assert "jq -e --arg site" in compose
    assert (
        'jq -e --arg site "$$SITE_NAME" --arg app "$$app" '
        "'.[$$site] | index($$app) != null' >/dev/null"
    ) in compose
    assert "APP_LIST=erpnext,crm,esan_gbos" in env_example
    assert "GBOS_IMAGE_STAGE=final" in env_example
    app_directory_check = compose.index('if [ ! -d "apps/$$app" ]; then')
    app_registry_update = compose.index('if ! grep -Fxq "$$app" sites/apps.txt; then')
    app_install = compose.index('bench --site "$$SITE_NAME" install-app "$$app"')
    app_migrate = compose.index('bench --site "$$SITE_NAME" migrate;')
    fixture_seed = compose.index('bench --site "$$SITE_NAME" execute esan_gbos.demo.seed')
    assert app_directory_check < app_registry_update < app_install < app_migrate
    assert app_migrate < fixture_seed


def test_site_creator_keeps_multiline_cli_options_in_the_same_shell_command() -> None:
    compose_lines = read_required(COMPOSE_FILE).splitlines()

    new_site_start = next(
        index
        for index, line in enumerate(compose_lines)
        if line.strip().startswith('bench new-site "$$SITE_NAME"')
    )
    assert [line.strip() for line in compose_lines[new_site_start : new_site_start + 6]] == [
        'bench new-site "$$SITE_NAME" \\',
        '--mariadb-user-host-login-scope="%" \\',
        "--db-root-username=root \\",
        '--db-root-password="$$DB_ROOT_PASSWORD" \\',
        '--admin-password="$$ADMIN_PASSWORD" \\',
        "--set-default;",
    ]

    seed_start = next(
        index
        for index, line in enumerate(compose_lines)
        if line.strip().startswith('bench --site "$$SITE_NAME" execute esan_gbos.demo.seed')
    )
    assert [line.strip() for line in compose_lines[seed_start : seed_start + 2]] == [
        'bench --site "$$SITE_NAME" execute esan_gbos.demo.seed \\',
        """--kwargs '{"confirm_synthetic": True}';""",
    ]


def test_site_creator_app_registry_handles_a_missing_trailing_newline(
    tmp_path: Path,
) -> None:
    compose = read_required(COMPOSE_FILE)
    start = compose.index("        touch sites/apps.txt;")
    end = compose.index('        if [ ! -d "sites/$$SITE_NAME" ]; then', start)
    registry_script = "\n".join(
        line.removeprefix("        ") for line in compose[start:end].splitlines()
    ).replace("$$", "$")

    sites = tmp_path / "sites"
    sites.mkdir()
    apps_file = sites / "apps.txt"
    apps_file.write_text("frappe\nerpnext\ncrm", encoding="utf-8")

    subprocess.run(
        [
            "/bin/sh",
            "-eu",
            "-c",
            f"APP_LIST=erpnext,crm,esan_gbos; {registry_script}",
        ],
        cwd=tmp_path,
        check=True,
    )

    assert apps_file.read_text(encoding="utf-8").splitlines() == [
        "frappe",
        "erpnext",
        "crm",
        "esan_gbos",
    ]


def test_upstream_app_manifest_is_release_pinned() -> None:
    apps = read_required(UPSTREAM_APPS)

    assert '"branch": "v16.31.0"' in apps
    assert '"branch": "v1.81.0"' in apps
    assert '"commit": "68ea583a1fbd1c533004cabc4294213e9a58716e"' in apps
    assert '"commit": "f6016eab20936ea15e5f450ec8dff9880f4dffe9"' in apps
    assert '"branch": "main"' not in apps
    assert '"branch": "latest"' not in apps


def test_example_environment_is_explicitly_non_production() -> None:
    env_example = read_required(ENV_EXAMPLE)

    assert "GBOS_ENV=development" in env_example
    assert "GBOS_PRODUCTION_ENABLED=false" in env_example
    assert "SYNTHETIC" in env_example
    assert "change-me" not in env_example.lower()
    assert "password" not in {
        line.partition("=")[2].strip().lower()
        for line in env_example.splitlines()
        if not line.startswith("#")
    }


def test_dev_scripts_are_executable_safe_and_shell_valid() -> None:
    for name in (
        "bootstrap",
        "build-custom-image",
        "license-sbom",
        "purge-local-data",
        "secret-scan",
        "security-scan",
        "status",
        "teardown",
        "validate-security-waivers",
        "verify-crm-contract",
    ):
        path = SCRIPTS_DIR / name
        script = read_required(path)

        assert path.stat().st_mode & stat.S_IXUSR
        assert 'SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")"' in script
        assert 'REPO_ROOT="$(CDPATH= cd -- "${SCRIPT_DIR}/../.."' in script
        assert 'export PATH="${HOME}/.orbstack/bin:${PATH}"' in script
        subprocess.run(["bash", "-n", str(path)], check=True)


def test_teardown_preserves_named_volumes() -> None:
    teardown = read_required(SCRIPTS_DIR / "teardown")

    assert " down" in teardown
    assert "--volumes" not in teardown
    assert not re.search(r"(?:^|\s)-v(?:\s|$)", teardown)


def test_local_data_purge_requires_an_exact_confirmation_flag() -> None:
    purge = read_required(SCRIPTS_DIR / "purge-local-data")

    assert "--confirm-delete-local-volumes" in purge
    assert '"${1:-}" != "--confirm-delete-local-volumes"' in purge
    assert "GBOS_PRODUCTION_ENABLED" in purge
    assert "--volumes" in purge
    assert "down --remove-orphans --volumes" in purge


def test_bootstrap_is_final_by_default_with_explicit_upstream_escape_hatch() -> None:
    bootstrap = read_required(SCRIPTS_DIR / "bootstrap")

    assert "--upstream-only" in bootstrap
    assert "GBOS_IMAGE_STAGE" in bootstrap
    assert "APP_LIST=erpnext,crm" in bootstrap
    assert "APP_LIST=erpnext,crm,esan_gbos" in bootstrap
    assert "GBOS_PRODUCTION_ENABLED" in bootstrap
    assert '"${SCRIPT_DIR}/build-custom-image"' in bootstrap
    assert "--esan-commit" in bootstrap
    assert "docker image inspect" in bootstrap
    assert "org.opencontainers.image.revision" in bootstrap
    assert "does not match repository HEAD" in bootstrap
    assert "Final runtime source must be tracked and clean" in bootstrap
    assert "ls-files --others --exclude-standard" in bootstrap
    assert "FINAL_INPUT_PATHS" in bootstrap
    assert "OBSERVER_ENABLED=false" in bootstrap
    assert "OBSERVER_ENABLED=true" in bootstrap
    assert "--profile core" in bootstrap
    assert "--profile observer" in bootstrap
    assert "up -d --wait observer-postgres" in bootstrap
    assert "run --rm observer-contract-check" in bootstrap
    assert bootstrap.index("up -d --wait observer-postgres") < bootstrap.index(
        "run --rm observer-contract-check"
    )
    assert '"${PROFILES[@]}"' not in bootstrap


def test_custom_image_builder_verifies_every_source_ref() -> None:
    builder = read_required(SCRIPTS_DIR / "build-custom-image")
    final_containerfile = read_required(FINAL_CONTAINERFILE)

    for expected in (
        "9523516cac25992bc2cd810e1015df8994c257f5",
        "68ea583a1fbd1c533004cabc4294213e9a58716e",
        "f6016eab20936ea15e5f450ec8dff9880f4dffe9",
        "3061850feface8fbbad15b5dc08a110c596107cb",
        "PLATFORM=linux/arm64",
        "--platform",
        "--push",
        "Final push target must be a registry-qualified image tag",
        "--secret id=apps_json",
        "docker image inspect",
        "--stage final",
        "--esan-commit",
        "apps/esan_gbos",
        "git archive",
        "FINAL_CONTEXT",
        "FINAL_INPUT_PATHS",
        "infra/dev/Containerfile.final",
        "infra/dev/apps.upstream.json",
        "infra/dev/realtime-runtime",
        "scripts/dev/build-custom-image",
    ):
        assert expected in builder
    assert "latest" not in builder
    assert "COPY --chown=frappe:frappe apps/esan_gbos" in final_containerfile
    assert "pip install --no-deps -e apps/esan_gbos" in final_containerfile
    assert "grep -Fxq esan_gbos sites/apps.txt" in final_containerfile
    assert "printf '\\n%s\\n' esan_gbos" in final_containerfile
    assert "awk 'NF && !seen[$0]++'" in final_containerfile
    assert "bench build --app esan_gbos" in final_containerfile
    assert "pnpm install --frozen-lockfile" in final_containerfile
    assert "pnpm run build" in final_containerfile
    assert "apps/esan_gbos/frontend/dist/" in final_containerfile
    assert "apps/esan_gbos/esan_gbos/public/frontend/" in final_containerfile
    assert "/home/frappe/frappe-bench/sites/assets/" in final_containerfile
    assert "infra/dev/nginx/frappe.conf.template" in final_containerfile
    backend_stage = final_containerfile.split("FROM ${UPSTREAM_IMAGE} AS final", 1)[1]
    assert "bench build" not in backend_stage
    assert "pnpm install" not in backend_stage
    assert "npm ci" not in backend_stage
    assert "\n    python3 -" in builder


def test_final_runtime_prunes_build_tooling_and_uses_a_locked_realtime_bundle() -> None:
    containerfile = read_required(FINAL_CONTAINERFILE)
    runtime_package = read_required(REALTIME_PACKAGE)
    runtime_lock = read_required(REALTIME_LOCK)

    assert "FROM ${UPSTREAM_BUILDER_IMAGE} AS realtime-runtime" in containerfile
    assert "realtime-runtime/package.json" in containerfile
    assert "realtime-runtime/package-lock.json" in containerfile
    assert "npm ci --omit=dev --ignore-scripts" in containerfile
    assert "npm cache clean --force" in containerfile
    assert "/usr/local/bin/node" in containerfile
    assert "/home/frappe/frappe-bench/node_modules/" in containerfile
    assert "rm -rf /home/frappe/.nvm" in containerfile
    assert "find /home/frappe/frappe-bench/apps -type d -name node_modules" in containerfile
    assert "find apps/esan_gbos -type d -name node_modules" in containerfile
    assert "apt-get upgrade -y" in containerfile
    assert "apt-get purge -y --auto-remove vim vim-common vim-runtime xxd" in containerfile

    for dependency in ('"socket.io": "4.8.3"', '"@redis/client": "1.5.12"', '"cookie": "0.7.0"'):
        assert dependency in runtime_package
    assert '"ws": "8.21.2"' in runtime_package
    assert '"lockfileVersion": 3' in runtime_lock
    for resolved_version in (
        '"node_modules/engine.io"',
        '"version": "6.6.9"',
        '"node_modules/socket.io-parser"',
        '"version": "4.2.7"',
        '"node_modules/ws"',
        '"version": "8.21.2"',
    ):
        assert resolved_version in runtime_lock


def test_final_image_registers_the_app_before_linking_frontend_assets() -> None:
    final_containerfile = read_required(FINAL_CONTAINERFILE)
    asset_stage = final_containerfile.split("FROM ${UPSTREAM_IMAGE} AS final", 1)[0]

    registry_position = asset_stage.index("grep -Fxq esan_gbos sites/apps.txt")
    build_position = asset_stage.index("bench build --app esan_gbos")
    manifest_check_position = asset_stage.index(
        "test -f sites/assets/esan_gbos/frontend/.vite/manifest.json"
    )

    assert registry_position < build_position < manifest_check_position


def test_custom_nginx_allows_only_the_gbos_worker_to_control_gbos_routes() -> None:
    template = read_required(NGINX_TEMPLATE)

    assert "location = /assets/esan_gbos/frontend/service-worker.js" in template
    assert 'add_header Service-Worker-Allowed "/gbos/" always;' in template
    assert template.index(
        "location = /assets/esan_gbos/frontend/service-worker.js"
    ) < template.index("location /assets")


def test_crm_contract_validator_uses_available_python_command() -> None:
    validator = read_required(SCRIPTS_DIR / "verify-crm-contract")

    assert "\npython3 -" in validator
    assert "\npython -" not in validator


def test_security_and_sbom_commands_are_reproducible_and_pinned() -> None:
    security = read_required(SCRIPTS_DIR / "security-scan")
    sbom = read_required(SCRIPTS_DIR / "license-sbom")

    trivy = (
        "aquasec/trivy:0.73.0"
        "@sha256:7cced7cae583819fc7806d4cbc0dbbc7"
        "cad18b99f7d3e235192e6da8c091045c"
    )
    assert trivy in security
    assert trivy in sbom
    assert "--severity HIGH,CRITICAL" in security
    assert "--ignore-unfixed" not in security
    assert '"${SCRIPT_DIR}/validate-security-waivers"' in security
    assert '"${WAIVER_FILE}:/policy/trivy-gate01-ignore.yaml:ro"' in security
    assert "--ignorefile /policy/trivy-gate01-ignore.yaml" in security
    assert "--show-suppressed" in security
    assert "--skip-files usr/share/java/libintl-0.21.jar" in security
    assert "**/*.jar" not in security
    assert "--format cyclonedx" in sbom
    assert "--scanners license" in sbom
    assert '--skip-files "${NON_RUNTIME_JAR}"' in sbom
    assert 'NON_RUNTIME_JAR="usr/share/java/libintl-0.21.jar"' in sbom
    assert "**/*.jar" not in sbom
    assert "docker image inspect" in security
    assert "docker image inspect" in sbom


def test_gate01_security_waivers_are_narrow_expiring_and_machine_validated() -> None:
    waivers = json.loads(read_required(TRIVY_WAIVERS))
    entries = waivers["vulnerabilities"]

    assert len(entries) == 57
    assert sum(len(entry["purls"]) for entry in entries) == 103
    assert len({entry["id"] for entry in entries}) == len(entries)
    assert all(set(entry) == {"id", "purls", "expired_at", "statement"} for entry in entries)
    assert all(entry["expired_at"] == "2026-09-30T00:00:00Z" for entry in entries)
    assert all(entry["purls"] for entry in entries)
    assert all(
        purl.startswith("pkg:") and "@" in purl and "*" not in purl
        for entry in entries
        for purl in entry["purls"]
    )
    assert all("Gate 0/1" in entry["statement"] for entry in entries)
    assert all("Gate 5/6" in entry["statement"] for entry in entries)
    subprocess.run([str(SCRIPTS_DIR / "validate-security-waivers")], check=True)


def test_ci_has_required_jobs_and_immutable_actions() -> None:
    workflow = read_required(WORKFLOWS_DIR / "ci.yml")
    smoke_workflow = read_required(WORKFLOWS_DIR / "frappe-app-smoke.yml")
    final_containerfile = read_required(FINAL_CONTAINERFILE)

    for job in (
        "contracts:",
        "compat:",
        "ruff:",
        "compose-config:",
        "app-materialization:",
        "frappe-vue-playwright:",
        "gitleaks:",
        "security-sbom:",
    ):
        assert job in workflow
    assert "docker compose" in workflow
    assert (
        "ghcr.io/gitleaks/gitleaks:v8.30.1"
        "@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f"
    ) in workflow
    assert "fetch-depth: 0" in workflow
    assert workflow.count("git . --no-banner --redact --exit-code 1") == 1
    assert "scripts/dev/security-scan" in workflow
    assert "scripts/dev/license-sbom" in workflow
    assert "apps/esan_gbos" in workflow
    assert "pnpm" in workflow
    assert "playwright" in workflow
    assert "git . --no-banner --redact --exit-code 1" in workflow
    assert "git --source ." not in workflow
    assert "corepack install --global pnpm@11.9.0" in workflow
    assert 'test "$(pnpm --version)" = "11.9.0"' in workflow
    assert "corepack install --global pnpm@11.9.0" in final_containerfile
    assert 'test "$(pnpm --version)" = "11.9.0"' in final_containerfile

    action_refs = [
        ref
        for path in WORKFLOWS_DIR.glob("*.yml")
        for ref in re.findall(r"uses:\s*([^\s#]+)", read_required(path))
    ]
    assert action_refs
    assert all(re.search(r"@[a-f0-9]{40}$", ref) for ref in action_refs)
    assert set(action_refs) == {
        "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803",
        "actions/setup-node@249970729cb0ef3589644e2896645e5dc5ba9c38",
        "actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f",
        "astral-sh/setup-uv@94527f2e458b27549849d47d273a16bec83a01e9",
    }
    assert workflow.count("actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803") == 8
    assert workflow.count("astral-sh/setup-uv@94527f2e458b27549849d47d273a16bec83a01e9") == 4
    assert workflow.count("actions/setup-node@249970729cb0ef3589644e2896645e5dc5ba9c38") == 1
    assert workflow.count("actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f") == 8
    assert smoke_workflow.count("actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803") == 1
    assert (
        smoke_workflow.count("actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f")
        == 1
    )


def test_frontend_dependency_build_scripts_are_explicitly_denied() -> None:
    workspace = read_required(FRONTEND_WORKSPACE)

    assert "allowBuilds:" in workspace
    for dependency in ("core-js", "sharp", "vue-demi"):
        assert re.search(rf"(?m)^  {re.escape(dependency)}: false$", workspace)
    assert "dangerouslyAllowAllBuilds" not in workspace
    assert "strictDepBuilds: false" not in workspace


def test_gitleaks_exception_is_narrow_and_keeps_default_rules() -> None:
    config = read_required(GITLEAKS_CONFIG)

    assert "useDefault = true" in config
    assert 'targetRules = ["generic-api-key"]' in config
    assert 'condition = "AND"' in config
    assert 'regexTarget = "line"' in config
    assert r"^\.github/workflows/frappe-app-smoke\.yml$" in config
    assert r"DB_ROOT_PASSWORD: SYNTHETIC-ci-db-root-[a-f0-9]{8}" in config
    assert "commits =" not in config


def test_frappe_app_smoke_builds_pr_image_or_accepts_pinned_dispatch_image() -> None:
    workflow = read_required(WORKFLOWS_DIR / "frappe-app-smoke.yml")

    assert "workflow_dispatch:" in workflow
    assert "pull_request:" in workflow
    assert re.search(r"(?m)^  push:", workflow) is None
    assert "custom_image" in workflow
    assert "sha256:" in workflow
    assert "linux/amd64" in workflow
    assert "scripts/dev/build-custom-image" in workflow
    assert '"containerd-snapshotter": true' in workflow
    assert "sudo systemctl restart docker" in workflow
    assert "io.containerd.snapshotter.v1" in workflow
    assert "Reclaim disposable runner disk" in workflow
    for disposable_path in (
        "/usr/local/lib/android",
        "/usr/share/dotnet",
        "/opt/ghc",
        "/usr/local/share/boost",
        "/opt/hostedtoolcache/CodeQL",
    ):
        assert disposable_path in workflow
    assert "docker system prune --all --force --volumes" in workflow
    assert "docker buildx use default" in workflow
    assert "docker buildx create --name gbos-ci" not in workflow
    assert "docker buildx inspect --bootstrap" in workflow
    assert workflow.index("sudo systemctl restart docker") < workflow.index(
        "docker buildx use default"
    )
    assert "--stage final" in workflow
    assert '--esan-commit "${SOURCE_COMMIT}"' in workflow
    assert "--platform linux/amd64" in workflow
    assert 'docker pull --platform linux/amd64 "${PROVIDED_IMAGE}"' in workflow
    assert 'echo "ERPNEXT_IMAGE=${RESOLVED_IMAGE}" >> "${GITHUB_ENV}"' in workflow
    assert "APP_LIST: erpnext,crm,esan_gbos" in workflow
    assert workflow.count('bench --site "${SITE_NAME}" migrate') >= 2
    assert "list-apps" in workflow
    assert "esan_gbos" in workflow
    assert 'apps_by_site["gbos-smoke.localhost"]' in workflow
    assert "set-config allow_tests true" in workflow
    assert 'bench --site "${SITE_NAME}" run-tests --app esan_gbos' in workflow
    assert "docker buildx prune --all --force" in workflow
