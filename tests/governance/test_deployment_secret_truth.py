from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

ROOT = Path(__file__).parents[2]
TEMPLATE = ROOT / "infra" / "prod" / "secret-provider-v1.template.json"
CONTRACT_EXAMPLE = (
    ROOT / "contracts" / "examples" / "gate6" / "deployment-secret-projection-valid.json"
)
CONTRACT_SCHEMA = ROOT / "contracts" / "gate6" / "deployment-secret-projection-v1.0.schema.json"
DEPLOYMENT_GUIDE = ROOT / "docs" / "deployment-secrets.md"
EXTERNAL_DEPS = ROOT / "docs" / "external-deps.md"
THREAT_MODEL = ROOT / "docs" / "governance" / "threat-model.md"

PROJECTION_FIELDS = {
    "logical_name",
    "target_filename",
    "kind",
    "minimum_bytes",
    "maximum_bytes",
    "exact_bytes",
    "component",
    "required",
    "platform_version_id",
}
FORBIDDEN_METADATA_KEYS = {
    "value",
    "secret_value",
    "token",
    "password",
    "keychain_ref",
    "secret_hash",
    "sha256",
    "uri",
    "secret_uri",
    "provider_payload",
    "resource_id",
    "resource_uri",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(_read(path))
    assert isinstance(value, dict)
    return value


def _projection_catalog(value: dict[str, Any]) -> dict[str, dict[str, Any]]:
    projections = value["projections"]
    assert isinstance(projections, list)
    return {projection["logical_name"]: projection for projection in projections}


def test_production_template_is_the_exact_value_free_contract_catalog() -> None:
    template = _json(TEMPLATE)
    contract = _json(CONTRACT_EXAMPLE)
    schema = _json(CONTRACT_SCHEMA)

    Draft202012Validator.check_schema(schema)
    assert list(Draft202012Validator(schema).iter_errors(template)) == []
    assert template["schema_version"] == "1.0"
    assert template["environment"] == "production"

    expected = _projection_catalog(contract)
    actual = _projection_catalog(template)
    assert set(actual) == set(expected)
    for logical_name, projection in actual.items():
        expected_projection = expected[logical_name]
        assert set(projection) <= PROJECTION_FIELDS
        for field in PROJECTION_FIELDS - {"platform_version_id", "exact_bytes"}:
            assert projection[field] == expected_projection[field]
        assert projection.get("exact_bytes") == expected_projection.get("exact_bytes")
        assert projection["platform_version_id"] == f"placeholder-version-{logical_name}"


def test_production_template_has_no_secret_or_vendor_resource_metadata() -> None:
    template = _json(TEMPLATE)
    serialized = json.dumps(template, sort_keys=True).lower()

    for projection in template["projections"]:
        assert FORBIDDEN_METADATA_KEYS.isdisjoint(projection)
    assert "://" not in serialized
    for vendor_term in ("aws", "azure", "google", "gcp", "vault://", "arn:"):
        assert vendor_term not in serialized


def test_deployment_guide_defines_one_authenticated_regular_file_path() -> None:
    guide = _read(DEPLOYMENT_GUIDE)

    for required_text in (
        "macOS Keychain is local-only",
        "platform-managed version identifiers",
        "workload identity",
        "authenticated projection",
        "private tmpfs or secret volume",
        "regular files",
        "0400 or 0600",
        "read-only application mount",
        "MountedFileSecretProvider",
    ):
        assert required_text in guide
    assert "symlink projection is forbidden" in guide


def test_deployment_guide_forbids_plaintext_secret_channels_and_unsafe_audit_data() -> None:
    guide = _read(DEPLOYMENT_GUIDE)

    for forbidden_channel in (
        "environment variables",
        "process arguments",
        "repository",
        "container image",
        "logs",
        "Frappe site config",
    ):
        assert f"No plaintext secret in {forbidden_channel}" in guide
    assert "Audit metadata contains no secret values or secret hashes" in guide


def test_adapter_selection_is_blocked_and_all_supported_patterns_are_described() -> None:
    guide = _read(DEPLOYMENT_GUIDE)
    dependencies = _read(EXTERNAL_DEPS)

    assert "adapter_selection: blocked_platform_selection" in guide
    assert "managed container secrets" in guide
    assert "Kubernetes CSI or External Secrets" in guide
    assert "copy into private regular files" in guide
    assert "Vault Agent" in guide
    assert "blocked_platform_selection" in dependencies
    assert "Production Go: false" in guide
    assert "separate Security, Platform, and Release Owner approvals" in guide


def test_restart_bound_rotation_has_stable_preflight_and_bounded_rollback() -> None:
    guide = _read(DEPLOYMENT_GUIDE)

    assert "rotation_mode: restart-bound-v1" in guide
    assert "rollback_window_minutes: 60" in guide
    assert "stable preflight must pass before rollout begins" in guide
    ordered_steps = (
        "Create a new platform secret version",
        "Project the candidate into the private volume",
        "Run the stable preflight",
        "Restart a bounded canary",
        "Prove health",
        "Complete the bounded rollout",
        "Revoke the old version",
    )
    positions = [guide.index(step) for step in ordered_steps]
    assert positions == sorted(positions)
    assert "rollback to the previous approved version" in guide
    assert "restart the affected workloads" in guide


def test_governance_documents_secret_projection_threats_and_no_go_boundary() -> None:
    threat_model = _read(THREAT_MODEL)
    dependencies = _read(EXTERNAL_DEPS)

    assert "TM-13" in threat_model
    assert "workload identity" in threat_model
    assert "regular file" in threat_model
    assert "restart-bound" in threat_model
    assert "values or hashes" in threat_model
    assert "Production Go remains false" in dependencies
    assert "separate approvals" in dependencies
