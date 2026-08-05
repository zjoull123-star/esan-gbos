from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

VERSION_LOCK = Path(__file__).parents[2] / "docs" / "compat" / "versions.json"
CRM_CONTRACT = Path(__file__).parents[2] / "docs" / "compat" / "crm-doctype-contract.json"
REPO_ROOT = Path(__file__).parents[2]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SHA_PATTERN = re.compile(r"^[a-f0-9]{40}$")
DIGEST_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")


def load_lock() -> dict[str, Any]:
    return json.loads(VERSION_LOCK.read_text(encoding="utf-8"))


def test_version_lock_pins_all_upstream_commits() -> None:
    lock = load_lock()

    assert set(lock["upstreams"]) == {
        "frappe",
        "erpnext",
        "frappe_crm",
        "frappe_docker",
    }
    for upstream in lock["upstreams"].values():
        assert SHA_PATTERN.fullmatch(upstream["commit"])
        assert upstream["tag"] not in {"latest", "main", "develop"}
        assert upstream["license"]
        assert upstream["release_url"].startswith("https://github.com/frappe/")


def test_version_lock_stays_on_supported_major_versions() -> None:
    lock = load_lock()

    assert lock["upstreams"]["frappe"]["tag"].startswith("v16.")
    assert lock["upstreams"]["erpnext"]["tag"].startswith("v16.")
    assert lock["upstreams"]["frappe_crm"]["tag"].startswith("v1.")


def test_version_lock_pins_multi_arch_image_digests() -> None:
    lock = load_lock()

    assert set(lock["images"]) == {"erpnext", "mariadb", "postgres", "redis"}
    for image in lock["images"].values():
        assert DIGEST_PATTERN.fullmatch(image["index_digest"])
        assert DIGEST_PATTERN.fullmatch(image["linux_arm64_digest"])
        assert image["tag"] not in {"latest", "main", "develop"}


def test_runtime_versions_match_frappe_v16_baseline() -> None:
    runtime = load_lock()["runtime"]

    assert runtime == {
        "python": "3.14.2",
        "node": "24.13.0",
        "mariadb": "11.8",
        "postgres": "17-bookworm",
        "pgvector": "0.8.2",
        "redis": "6.2-alpine",
        "architecture": "linux/arm64",
    }


def test_repository_and_ci_pin_python_and_node_patch_versions() -> None:
    runtime = load_lock()["runtime"]

    assert (REPO_ROOT / ".python-version").read_text(encoding="utf-8").strip() == runtime["python"]
    assert (REPO_ROOT / ".node-version").read_text(encoding="utf-8").strip() == runtime["node"]
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020" in workflow
    assert 'node-version: "24.13.0"' in workflow


def test_local_upstream_runtime_evidence_is_frozen_without_claiming_gate1() -> None:
    lock = load_lock()

    assert lock["tooling"] == {
        "orbstack": "2.2.2",
        "docker": "29.4.0",
        "compose": "5.1.2",
        "buildx": "0.33.0",
        "host_architecture": "arm64",
    }
    evidence = lock["local_evidence"]["upstream_image"]
    assert evidence["tag"] == "esan-gbos-upstream:gate0"
    assert (
        evidence["digest"]
        == "sha256:b69f0001225523ec52ceb6d80fc696c34f24c560a0d15c5ebc53e803eb5286ec"
    )
    assert evidence["created"] == "2026-08-06T03:00:09+08:00"
    assert evidence["bench_version"] == {
        "crm": "1.81.0",
        "erpnext": "16.31.0",
        "frappe": "16.30.0",
    }
    assert evidence["node_scope"] == "builder-only"
    assert lock["gate_status"] == {
        "upstream_three_app_runtime": "verified-local",
        "final_four_app_runtime": "pending-gate1",
    }


def test_crm_doctype_contract_freezes_verified_parent_fields() -> None:
    contract = json.loads(CRM_CONTRACT.read_text(encoding="utf-8"))
    doctypes = contract["doctypes"]

    assert set(doctypes) == {
        "CRM Organization",
        "Contact",
        "CRM Lead",
        "CRM Deal",
        "CRM Contacts",
    }
    assert doctypes["CRM Organization"]["autoname"] == "field:organization_name"
    assert doctypes["CRM Organization"]["fields"]["organization_name"] == {
        "fieldtype": "Data",
        "unique": True,
    }
    assert doctypes["Contact"]["autoname"] is None
    assert doctypes["Contact"]["fields"]["email_id"] == {
        "fieldtype": "Data",
        "options": "Email",
    }
    assert doctypes["CRM Lead"]["fields"]["first_name"] == {
        "fieldtype": "Data",
        "reqd": True,
    }
    assert doctypes["CRM Lead"]["fields"]["status"] == {
        "fieldtype": "Link",
        "options": "CRM Lead Status",
        "reqd": True,
    }
    assert doctypes["CRM Deal"]["fields"]["status"] == {
        "fieldtype": "Link",
        "options": "CRM Deal Status",
        "reqd": True,
    }
    assert doctypes["CRM Deal"]["fields"]["contact"] == {
        "fieldtype": "Link",
        "options": "Contact",
    }
    assert doctypes["CRM Contacts"]["fields"]["contact"] == {
        "fieldtype": "Link",
        "options": "Contact",
    }
    assert contract["reference_values"] == {
        "CRM Lead Status": [
            "Contacted",
            "Converted",
            "Junk",
            "New",
            "Nurture",
            "Qualified",
            "Unqualified",
        ],
        "CRM Deal Status": [
            "Demo/Making",
            "Lost",
            "Negotiation",
            "Proposal/Quotation",
            "Qualification",
            "Ready to Close",
            "Won",
        ],
    }
