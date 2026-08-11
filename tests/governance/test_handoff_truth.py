from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).parents[2]
README = ROOT / "README.md"
PERMISSION_MATRIX = ROOT / "docs" / "permission-matrix.md"
EXTERNAL_DEPS = ROOT / "docs" / "external-deps.md"
HANDOFF = ROOT / "docs" / "HANDOFF.md"
LOCAL_PLAN = ROOT / "docs" / "local-pilot" / "IMPLEMENTATION_PLAN.md"
LOCAL_INFRA_README = ROOT / "infra" / "local" / "README.md"
IDENTITY_PLAN = (
    ROOT / "docs" / "superpowers" / "plans" / "2026-08-09-gbos-user-identity-resolution.md"
)
DOCTYPE_ROOT = ROOT / "apps" / "esan_gbos" / "esan_gbos" / "gbos" / "doctype"
CEO_ACCESS = ROOT / "apps" / "esan_gbos" / "esan_gbos" / "ceo_access.py"
MANIFEST = ROOT / "infra" / "local" / "local-pilot-manifest.json"
ENTRYPOINTS = ROOT / "infra" / "local" / "runtime-entrypoints.json"
IMAGE_LOCK = ROOT / "infra" / "local" / "images.lock.json"
TASK13_CLOSURE_DIR = ROOT / "docs" / "evidence" / "task13-credential-free-closure"
TASK13_CLOSURE_EVIDENCE = TASK13_CLOSURE_DIR / "task13-evidence.json"
TASK13_CLOSURE_SUMMARY = TASK13_CLOSURE_DIR / "task13-summary.md"
TASK13_CLOSURE_SUMS = TASK13_CLOSURE_DIR / "SHA256SUMS"
IDENTITY_CLOSURE_DIR = ROOT / "docs" / "evidence" / "user-identity-governance-closure"
IDENTITY_CLOSURE_EVIDENCE = IDENTITY_CLOSURE_DIR / "identity-governance-evidence.json"
IDENTITY_CLOSURE_SUMMARY = IDENTITY_CLOSURE_DIR / "identity-governance-summary.md"
IDENTITY_CLOSURE_SUMS = IDENTITY_CLOSURE_DIR / "SHA256SUMS"

HISTORICAL_TASK13_SOURCE_COMMIT = "ad58ab3ea8c0d521cebd90c2642709d135f98fac"
CURRENT_FRAPPE_SOURCE_COMMIT = "4b2512ba5bf8bbc3bc12cc6beb62055c735dc629"
CURRENT_RUNTIME_SOURCE_COMMIT = "341b2df9c45b22c0579f960dcb5ecbe694cdd215"
CURRENT_IMAGE_LOCK_COMMIT = "d8bdc18b468f0e0b2507b4db3a5d0e55ef9ab2f2"

CEO_ROLES = (
    "CEO",
    "GBOS Admin",
    "Integration Admin",
    "Reviewer",
    "System Manager",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_readme_reports_the_current_doctype_inventory_and_local_boundary() -> None:
    readme = _read(README)
    documents = [
        json.loads(path.read_text(encoding="utf-8")) for path in DOCTYPE_ROOT.glob("*/*.json")
    ]
    parent_count = sum(not document.get("istable") for document in documents)
    child_count = sum(bool(document.get("istable")) for document in documents)

    assert (parent_count, child_count) == (15, 3)
    assert f"{parent_count} 个父 DocType 与 {child_count} 个 Child DocType" in readme
    assert "13 个父 DocType 与 2 个 Child DocType" not in readme
    assert "local pilot" in readme.lower()
    assert "No-Go" in readme


def test_permission_matrix_records_the_closed_ceo_auto_elevation_bundle() -> None:
    matrix = _read(PERMISSION_MATRIX)
    ceo_source = _read(CEO_ACCESS)

    assert "CEO auto-elevation" in matrix
    assert "System User" in matrix
    assert "before_validate" in matrix
    assert "after_install" in matrix
    assert "after_migrate" in matrix
    for role in CEO_ROLES:
        assert f"`{role}`" in matrix
    ceo_section = matrix.split("CEO auto-elevation", 1)[1].split("Legend:", 1)[0]
    assert "Privacy/Audit" not in ceo_section
    assert "CEO_FULL_ACCESS_ROLES" in ceo_source
    for role in CEO_ROLES:
        assert role in _read(HANDOFF)
    assert "无连接配置；受控汇总导出" not in matrix
    assert "Gate 2 均未启动下列服务身份" not in matrix
    assert "所有服务身份均未启动" not in matrix
    assert "Observer Identity Resolver" in matrix
    assert "Agent TrustedMaterializer" in matrix
    assert "正式 local pilot" in matrix


def test_external_dependency_truth_names_deepseek_without_claiming_a_real_call() -> None:
    dependencies = _read(EXTERNAL_DEPS)

    assert "DeepSeek gateway" in dependencies
    assert "deepseek-v4-flash" in dependencies
    assert "no real call" in dependencies.lower()
    assert "model identity" in dependencies.lower()
    assert "real provider not selected" not in dependencies.lower()
    assert "formal local pilot" in dependencies.lower()
    for capability in ("real channels", "real model", "Kingdee", "cloud", "production"):
        assert capability.lower() in dependencies.lower()


def test_handoff_binds_source_baseline_current_runtime_truth_and_historical_boundary() -> None:
    handoff = _read(HANDOFF)
    manifest = json.loads(_read(MANIFEST))
    entrypoints = json.loads(_read(ENTRYPOINTS))
    image_lock = json.loads(_read(IMAGE_LOCK))

    assert "8c40731" in handoff
    assert "feat/user-identity-resolution-20260810" in handoff
    assert "historical" in handoff.lower()
    assert "current main" in handoff.lower()
    assert "do not modify" in handoff.lower()
    assert "15 parent" in handoff
    assert "3 child" in handoff
    assert "CEO" in handoff and "System User" in handoff
    assert "DeepSeek gateway" in handoff
    assert "real call" in handoff.lower()
    assert "model identity" in handoff.lower()
    assert "No-Go" in handoff
    assert "composition.status=composed" in handoff
    assert "local_pilot_go=false" in handoff
    assert "real channels" in handoff.lower()
    assert "Kingdee" in handoff
    assert "cloud" in handoff.lower()
    assert "production" in handoff.lower()

    assert f"production_go={str(manifest['production_go']).lower()}" in handoff
    assert f"local_pilot_go={str(manifest['local_pilot_go']).lower()}" in handoff
    assert entrypoints["composition"]["status"] in handoff

    locked_digests = {
        image["service"]: image["local_inspect_digest"]
        for image in image_lock["images"]
        if image["service"] in {"frappe-pwa", "local-runtime"}
    }
    for service, digest in locked_digests.items():
        assert digest in handoff, service


def test_owned_handoff_docs_do_not_reintroduce_stale_runtime_claims() -> None:
    owned_docs = "\n".join(
        _read(path) for path in (README, PERMISSION_MATRIX, EXTERNAL_DEPS, HANDOFF)
    )

    forbidden = (
        "13 个父 DocType 与 2 个 Child DocType",
        "real provider not selected",
        "local_pilot_go=true",
        "composition.status=go",
        "production_go=true",
        "real channels verified",
        "real model verified",
        "Kingdee live verified",
        "cloud deployment verified",
        "production deployed",
    )
    for statement in forbidden:
        assert statement.lower() not in owned_docs.lower(), statement


def test_current_local_pilot_docs_follow_the_locked_runtime_images() -> None:
    image_lock = json.loads(_read(IMAGE_LOCK))
    locked_digests = {
        image["service"]: image["local_inspect_digest"]
        for image in image_lock["images"]
        if image["service"] in {"frappe-pwa", "local-runtime"}
    }
    current_docs = "\n".join(
        _read(path) for path in (EXTERNAL_DEPS, LOCAL_PLAN, LOCAL_INFRA_README)
    )

    assert CURRENT_FRAPPE_SOURCE_COMMIT in current_docs
    assert CURRENT_RUNTIME_SOURCE_COMMIT in current_docs
    assert CURRENT_IMAGE_LOCK_COMMIT in current_docs
    for service, digest in locked_digests.items():
        assert digest in current_docs, service
    for stale in (
        "00a1a0a395d6326688ff131192c9aa332f8d32b1",
        "sha256:94c1bb068a868e0c0c7bb1deda231c2fc5bd13f2928b83036f83802674c5afe6",
        "sha256:705012abe856dbe33298e508c79e121831585e1036dca701a93553ebe0186c8b",
    ):
        assert stale not in current_docs


def test_identity_handoff_keeps_the_four_user_relations_separate_and_truthful() -> None:
    handoff = _read(HANDOFF)
    plan = _read(IDENTITY_PLAN)

    for relation in (
        "Observation.team_ref ↔ GBOS Team Member.user",
        "Connector Instance.account_user_ref",
        "Participant.identity_ref",
        "Deal owner / owner_user / assigned_to",
    ):
        assert relation in handoff
        assert relation in plan

    for document in (handoff, plan):
        assert "禁止相互推导" in document
        assert "c98f6a5" in document
        assert "Task 13" in document
        assert "未执行" in document
        assert "真实 Frappe" in document
        assert "Prometheus" in document
        assert "local_pilot_go=false" in document
    assert "72 小时连续运行不再作为" in handoff
    assert "72 小时" in plan


def test_current_task13_closure_snapshot_is_bound_to_code_and_not_a_canary_claim() -> None:
    evidence = json.loads(_read(TASK13_CLOSURE_EVIDENCE))
    summary = _read(TASK13_CLOSURE_SUMMARY)

    assert evidence["schema_version"] == "1.0"
    assert evidence["validation_reference_commit"] == HISTORICAL_TASK13_SOURCE_COMMIT
    source_scope = evidence["source_scope"]
    assert source_scope["runtime_code_validation_reference"] == HISTORICAL_TASK13_SOURCE_COMMIT
    assert source_scope["branch_head_scope"] == (
        "final branch includes only image-lock/test/docs successors after the "
        "runtime validation reference"
    )
    assert source_scope["network_used_for_this_snapshot"] is True
    assert source_scope["containers_started_for_this_snapshot"] is True
    assert source_scope["provider_channel_network"] is False
    assert source_scope["pilot_application_services_started"] is False
    assert "governed dependency/image/scanner network" in source_scope["activity_qualifier"]
    captured_at = datetime.fromisoformat(evidence["captured_at"].replace("Z", "+00:00"))
    assert captured_at.tzinfo is not None
    assert captured_at.utcoffset() == UTC.utcoffset(captured_at)
    assert evidence["status"] == "credential_free_closure_external_canary_deferred"
    assert evidence["stability"] == {
        "continuous_runtime_required": False,
        "seventy_two_hour_run": "deferred_by_user",
    }
    assert evidence["verification"]["pytest"]["passed"] == 2692
    assert evidence["verification"]["pytest"]["skipped"] == 42
    assert evidence["verification"]["pytest"]["failed"] == 0
    assert evidence["verification"]["pytest"]["warnings"] == 1
    assert evidence["verification"]["frontend"] == {
        "unit_passed": 188,
        "harness_playwright_passed": 22,
        "lint": "pass",
        "typecheck": "pass",
        "build": "pass",
    }
    assert evidence["verification"]["python_static"] == {
        "ruff_check": "pass",
        "ruff_format": "pass",
        "mypy": "pass",
        "compileall": "pass",
        "secret_scan": "pass",
    }
    assert evidence["verification"]["model_fatal_latch"]["status"] == "verified"
    assert evidence["verification"]["model_fatal_latch"]["real_canary_invocations"] == 0
    assert (
        evidence["verification"]["model_fatal_latch"]["database_integration"]
        == "isolated_fatal_latch_only"
    )
    email_checkpoint = evidence["verification"]["email_status_checkpoint"]
    assert email_checkpoint["status"] == "verified_by_credential_free_tests"
    assert email_checkpoint["operation"] == "STATUS_UIDVALIDITY_UIDNEXT"
    assert email_checkpoint["read_only"] is True
    assert email_checkpoint["source_bound"] is True
    assert email_checkpoint["receipt_required_by_preflight"] is True
    assert email_checkpoint["real_imap_connections"] == 0
    chain_verifier = evidence["verification"]["canary_chain_verifier"]
    assert chain_verifier["status"] == "machine_db_attested_narrow_window_only"
    assert chain_verifier["reports_only"] == "response_reported_observed_model"
    assert chain_verifier["free_form_observed_model"] is False
    assert chain_verifier["real_canary_runs"] == 0
    assert evidence["formal_state"] == {
        "production_go": False,
        "local_pilot_go": False,
        "checked_in_email_enabled": False,
        "checked_in_deepseek_enabled": False,
        "external_send": False,
        "kingdee": False,
        "cloud": False,
    }
    assert evidence["go_no_go"] == {
        "credential_free_closure": "go",
        "real_email_deepseek_canary": "no_go",
        "response_reported_observed_model": "unknown",
        "production": "no_go",
        "kingdee": "no_go",
        "cloud": "no_go",
        "external_send": "no_go",
    }
    assert evidence["runtime_images"]["rebuild_required_before_real_canary"] is False
    assert evidence["runtime_images"]["rebuild_verified"] is True
    assert evidence["runtime_images"]["frappe_pwa"]["image_id"].startswith("sha256:")
    assert evidence["runtime_images"]["local_runtime"]["image_id"].startswith("sha256:")
    assert evidence["missing_external_credentials"]
    evidence_text = TASK13_CLOSURE_EVIDENCE.read_text(encoding="utf-8")
    assert "evidence_commit" not in evidence_text
    assert "sk-" not in evidence_text
    assert "-----BEGIN" not in evidence_text
    assert "password" not in evidence_text.lower()
    assert "real Email" in summary
    assert "unknown" in summary
    assert "72 小时" in summary
    assert "PostgreSQL integration matrix" in summary
    assert "final evidence commit" in summary


def test_current_task13_closure_snapshot_checksums_cover_only_current_files() -> None:
    entries = {}
    for line in _read(TASK13_CLOSURE_SUMS).splitlines():
        digest, name = line.split("  ", maxsplit=1)
        entries[name] = digest

    assert set(entries) == {TASK13_CLOSURE_EVIDENCE.name, TASK13_CLOSURE_SUMMARY.name}
    for name, expected in entries.items():
        assert hashlib.sha256((TASK13_CLOSURE_DIR / name).read_bytes()).hexdigest() == expected


def test_handoff_calls_out_current_source_and_image_rebuild_boundary() -> None:
    handoff = _read(HANDOFF)

    assert CURRENT_FRAPPE_SOURCE_COMMIT in handoff
    assert CURRENT_RUNTIME_SOURCE_COMMIT in handoff
    assert CURRENT_IMAGE_LOCK_COMMIT in handoff
    assert "frappe source reference" in handoff.lower()
    assert "runtime source reference" in handoff.lower()
    assert "current code HEAD 是" not in handoff
    assert "governed rebuild/record" in handoff.lower()
    assert "response_reported_observed_model=unknown" in handoff
    assert "real_email_deepseek_canary=no_go" in handoff
    assert "72 小时连续运行不再作为本阶段退出条件" in handoff


def test_current_identity_governance_closure_is_source_bound_and_honest() -> None:
    evidence = json.loads(_read(IDENTITY_CLOSURE_EVIDENCE))
    summary = _read(IDENTITY_CLOSURE_SUMMARY)
    image_lock = json.loads(_read(IMAGE_LOCK))
    locked_digests = {
        image["service"]: image["local_inspect_digest"]
        for image in image_lock["images"]
        if image["service"] in {"frappe-pwa", "local-runtime"}
    }

    assert evidence["schema_version"] == "1.0"
    assert evidence["status"] == "credential_free_design_closure_real_canary_deferred"
    assert evidence["source_scope"] == {
        "branch": "feat/user-identity-resolution-20260810",
        "frappe_source_reference": CURRENT_FRAPPE_SOURCE_COMMIT,
        "runtime_source_reference": CURRENT_RUNTIME_SOURCE_COMMIT,
        "image_lock_commit": CURRENT_IMAGE_LOCK_COMMIT,
        "historical_evidence_modified": False,
    }
    assert (
        evidence["runtime_images"]["frappe_pwa"]["inspect_digest"] == locked_digests["frappe-pwa"]
    )
    assert (
        evidence["runtime_images"]["local_runtime"]["inspect_digest"]
        == locked_digests["local-runtime"]
    )
    assert evidence["verification"]["pytest"] == {
        "passed": 2850,
        "skipped": 44,
        "failed": 0,
        "warnings": 1,
    }
    assert evidence["verification"]["domain_contracts_passed"] == 799
    assert evidence["verification"]["postgresql_integration"] == {
        "passed": 43,
        "warnings": 1,
        "source_scope": "earlier_same_feature_lineage",
        "disposable_environment_removed": True,
    }
    assert evidence["verification"]["postgresql_current_source_canary_closure"] == {
        "runtime_source_reference": CURRENT_RUNTIME_SOURCE_COMMIT,
        "observer_migrations": "001-013_applied_twice",
        "context_migrations": "001-005_applied_twice",
        "agent_migrations": "001-006_applied_twice",
        "app_roles": ["gbos_observer_app", "gbos_context_app", "gbos_agent_app"],
        "roles_have_bypassrls": False,
        "read_only_start_guard_and_chain_queries": "pass",
        "disposable_environment_removed": True,
    }
    assert evidence["verification"]["native_frappe"] == {
        "identity_tests_passed": 13,
        "whole_app_tests_passed": 59,
        "migrations_completed_twice": True,
        "disposable_environment_removed": True,
    }
    assert evidence["verification"]["frontend"] == {
        "unit_passed": 196,
        "harness_playwright_passed": 25,
        "lint": "pass",
        "typecheck": "pass",
        "build": "pass",
    }
    assert evidence["verification"]["infra_passed"] == 179
    assert evidence["formal_state"]["local_pilot_go"] is False
    assert evidence["formal_state"]["production_go"] is False
    assert evidence["external_activity"] == {
        "real_imap_connections": 0,
        "real_model_api_calls": 0,
        "provider_channel_network": False,
        "pilot_application_stack_started": False,
        "observed_model_identity": "unknown",
        "response_reported_observed_model": "unknown",
    }
    assert evidence["stability"] == {
        "seventy_two_hour_run": "deferred_by_user",
        "required_for_this_stage": False,
    }
    assert evidence["external_input_inventory"] == {
        "checked_at": "2026-08-11T02:14:28Z",
        "method": "macos_keychain_metadata_existence_only",
        "secret_values_read_or_recorded": False,
        "formal_external_manifest_preflight": "pass_with_placeholder_keychain_refs",
        "generated_locally_at": "2026-08-11T02:14:28Z",
        "generated_fixed_items": [
            "identity-hmac-key",
            "frappe-identity-resolver-api-key",
            "frappe-identity-resolver-api-secret",
        ],
        "generation_contract": "independent_256_bit_random_lowercase_hex",
        "fixed_required_available": [
            "postgres-password",
            "postgres-observer-password",
            "postgres-context-password",
            "postgres-agent-password",
            "postgres-media-password",
            "mariadb-root-password",
            "frappe-admin-password",
            "agent-api-bearer",
            "context-api-bearer",
            "cursor-hmac-key",
            "tokenizer-hmac-key",
            "mapping-vault-aes-256-key",
            "frappe-materializer-api-key",
            "frappe-materializer-api-secret",
            "identity-hmac-key",
            "frappe-identity-resolver-api-key",
            "frappe-identity-resolver-api-secret",
        ],
        "fixed_required_missing": [
            "trusted-phrase-lexicon",
        ],
        "dynamic_references_not_supplied": [
            "approved Email credential Keychain reference",
            "approved DeepSeek API Keychain reference",
        ],
        "operator_scope_not_supplied": [
            "approved activation time",
            "approved target team and connector account user",
            "approved reviewer",
            "approved target User and Party",
        ],
    }
    assert evidence["go_no_go"]["credential_free_design_closure"] == "go"
    assert evidence["go_no_go"]["real_email_deepseek_canary"] == "no_go"
    assert evidence["go_no_go"]["formal_local_pilot"] == "no_go"
    assert evidence["missing_external_inputs"]
    assert "真实 Email/DeepSeek canary 未执行" in summary
    assert "72 小时" in summary
    assert "2850 passed, 44 skipped, 1 warning" in summary
    assert "credential binding" in summary
    assert "Email delivery" in summary
    assert "metadata-only Keychain inventory" in summary
    assert "三个本地随机凭据" in summary

    evidence_text = IDENTITY_CLOSURE_EVIDENCE.read_text(encoding="utf-8")
    for forbidden in ("sk-", "-----BEGIN", "Cookie:", "Authorization:"):
        assert forbidden not in evidence_text


def test_current_identity_governance_closure_checksums_cover_only_current_files() -> None:
    entries = {}
    for line in _read(IDENTITY_CLOSURE_SUMS).splitlines():
        digest, name = line.split("  ", maxsplit=1)
        entries[name] = digest

    assert set(entries) == {
        IDENTITY_CLOSURE_EVIDENCE.name,
        IDENTITY_CLOSURE_SUMMARY.name,
    }
    for name, expected in entries.items():
        assert hashlib.sha256((IDENTITY_CLOSURE_DIR / name).read_bytes()).hexdigest() == expected
