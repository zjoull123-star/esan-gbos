#!/usr/bin/env python3
"""Fail-closed local validation for immutable Gate 6 release inputs.

This module reads JSON documents containing references only. It never resolves a
secret reference, reads environment variables, or prints document values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

PINNED_IMAGE = re.compile(r"^[^\s@:]+(?:/[^\s@:]+)+@sha256:[0-9a-f]{64}$")
DOMAIN = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
SECRET_REF = re.compile(r"^(?:secret|vault|kms)://[^\s]+$")
BACKUP_REF = re.compile(r"^(?:object|s3|gs|azure)://[^\s]+$")
REQUIRED_COMPONENTS = {
    "app",
    "mariadb",
    "postgres_pgvector",
    "queue_cache",
    "object_storage",
    "ingress_waf",
    "monitoring",
}
DATA_COMPONENTS = {"mariadb", "postgres_pgvector", "queue_cache", "object_storage"}
CAPABILITIES = {
    "connectors_enabled",
    "live_models_enabled",
    "kingdee_enabled",
    "external_sends_enabled",
    "destructive_operations_enabled",
}
RAW_SECRET_KEYS = {
    "password",
    "password_value",
    "secret_value",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "private_key",
    "client_secret",
    "credential",
    "credentials",
}


@dataclass(frozen=True)
class ValidatedRelease:
    manifest: dict[str, Any]
    topology: dict[str, Any]


class ValidationFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def reject(code: str, message: str) -> None:
    raise ValidationFailure(code, message)


def load_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
    except OSError:
        reject("INPUT_UNREADABLE", f"{label} input cannot be read")
    try:
        document = json.loads(raw)
    except UnicodeDecodeError, json.JSONDecodeError:
        reject("INPUT_MALFORMED", f"{label} input is not valid UTF-8 JSON")
    if not isinstance(document, dict):
        reject("INPUT_MALFORMED", f"{label} input must be a JSON object")
    return document, raw


def reject_embedded_secret_material(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in RAW_SECRET_KEYS:
                reject(
                    "RAW_SECRET_MATERIAL_FORBIDDEN",
                    "input contains a secret-bearing field; only references are permitted",
                )
            reject_embedded_secret_material(child)
    elif isinstance(value, list):
        for child in value:
            reject_embedded_secret_material(child)


def require_mapping(document: dict[str, Any], key: str, code: str) -> dict[str, Any]:
    value = document.get(key)
    if not isinstance(value, dict):
        reject(code, f"{key} must be an object")
    return value


def require_exact_keys(document: dict[str, Any], expected: set[str]) -> None:
    if set(document) - expected:
        reject("TOPOLOGY_UNKNOWN_FIELD", "topology contains unreviewed fields")


def validate_topology(topology: dict[str, Any]) -> None:
    require_exact_keys(
        topology,
        {
            "api_version",
            "kind",
            "topology_id",
            "version",
            "enabled",
            "environment",
            "domain",
            "tls",
            "secrets",
            "backup",
            "release_policy",
            "capabilities",
            "components",
        },
    )
    if topology.get("api_version") != "gbos.esan/v1" or topology.get("version") != 1:
        reject("TOPOLOGY_VERSION_UNSUPPORTED", "topology must use the approved version")
    if topology.get("kind") != "ProductionTopology" or topology.get("enabled") is not False:
        reject("TOPOLOGY_NOT_INERT", "topology must remain an inert production placeholder")

    environment = require_mapping(topology, "environment", "TOPOLOGY_ENVIRONMENT_REQUIRED")
    require_exact_keys(environment, {"name", "identity", "tenant_mode", "tenant_id"})
    if (
        environment.get("name") != "production"
        or environment.get("tenant_mode") != "single"
        or not isinstance(environment.get("identity"), str)
        or not environment.get("identity")
        or not isinstance(environment.get("tenant_id"), str)
        or not environment.get("tenant_id")
    ):
        reject("TOPOLOGY_ENVIRONMENT_REQUIRED", "production environment identity is incomplete")

    domain_config = require_mapping(topology, "domain", "TOPOLOGY_DOMAIN_REQUIRED")
    require_exact_keys(domain_config, {"primary"})
    domain = domain_config.get("primary")
    if not isinstance(domain, str) or not DOMAIN.fullmatch(domain):
        reject("TOPOLOGY_DOMAIN_REQUIRED", "a valid primary domain reference is required")

    tls = require_mapping(topology, "tls", "TOPOLOGY_TLS_REQUIRED")
    require_exact_keys(tls, {"secret_ref"})
    tls_ref = tls.get("secret_ref")
    if not isinstance(tls_ref, str) or not SECRET_REF.fullmatch(tls_ref):
        reject("TOPOLOGY_TLS_REQUIRED", "a TLS secret reference is required")

    secrets = require_mapping(topology, "secrets", "TOPOLOGY_SECRETS_REQUIRED")
    require_exact_keys(secrets, {"provider", "kms_key_ref", "required_refs"})
    required_refs = secrets.get("required_refs")
    kms_key_ref = secrets.get("kms_key_ref")
    if (
        secrets.get("provider") != "external-kms"
        or not isinstance(kms_key_ref, str)
        or not SECRET_REF.fullmatch(kms_key_ref)
        or not isinstance(required_refs, list)
        or not required_refs
        or not all(isinstance(item, str) and SECRET_REF.fullmatch(item) for item in required_refs)
    ):
        reject("TOPOLOGY_SECRETS_REQUIRED", "external KMS and secret references are required")

    backup = require_mapping(topology, "backup", "BACKUP_TARGET_REQUIRED")
    require_exact_keys(backup, {"target_ref", "identity", "kms_key_ref"})
    backup_target = backup.get("target_ref")
    backup_identity = backup.get("identity")
    backup_kms = backup.get("kms_key_ref")
    if (
        not isinstance(backup_target, str)
        or not BACKUP_REF.fullmatch(backup_target)
        or not isinstance(backup_identity, str)
        or not backup_identity
        or not isinstance(backup_kms, str)
        or not SECRET_REF.fullmatch(backup_kms)
    ):
        reject(
            "BACKUP_TARGET_REQUIRED", "isolated backup target, identity, and KMS reference required"
        )
    if backup_kms == kms_key_ref:
        reject("BACKUP_IDENTITY_NOT_ISOLATED", "backup and runtime KMS boundaries must differ")

    capabilities = require_mapping(topology, "capabilities", "CAPABILITY_POLICY_REQUIRED")
    if set(capabilities) != CAPABILITIES:
        reject("CAPABILITY_POLICY_REQUIRED", "all kill-switch-controlled capabilities are required")
    if any(capabilities[name] is not False for name in CAPABILITIES):
        reject("CAPABILITY_MUST_BE_DISABLED", "external and destructive capabilities must be false")

    components = require_mapping(topology, "components", "TOPOLOGY_COMPONENTS_REQUIRED")
    if set(components) != REQUIRED_COMPONENTS:
        reject("TOPOLOGY_COMPONENTS_REQUIRED", "all isolated production components are required")

    identities: list[str] = []
    for name in sorted(REQUIRED_COMPONENTS):
        component = components.get(name)
        if not isinstance(component, dict):
            reject("TOPOLOGY_COMPONENTS_REQUIRED", "each topology component must be an object")
        require_exact_keys(
            component,
            {"image", "identity", "network", "public_ports", "secret_refs"},
        )
        image = component.get("image")
        if not isinstance(image, str) or not PINNED_IMAGE.fullmatch(image):
            reject("FLOATING_IMAGE", "every component image must be pinned by sha256 digest")
        identity = component.get("identity")
        network = component.get("network")
        public_ports = component.get("public_ports")
        secret_refs = component.get("secret_refs")
        if (
            not isinstance(identity, str)
            or not identity
            or not isinstance(network, str)
            or not network
        ):
            reject("COMPONENT_BOUNDARY_REQUIRED", "each component needs an identity and network")
        if not isinstance(public_ports, list) or not all(
            isinstance(port, int) and not isinstance(port, bool) for port in public_ports
        ):
            reject("PUBLIC_PORT_POLICY_INVALID", "public port declarations must be integer arrays")
        if name in DATA_COMPONENTS and public_ports:
            reject(
                "PUBLIC_DATA_PORT", "database, cache, and object storage ports must remain private"
            )
        if name == "ingress_waf" and public_ports != [443]:
            reject("INGRESS_TLS_ONLY", "ingress/WAF may expose only TLS port 443")
        if name not in DATA_COMPONENTS | {"ingress_waf"} and public_ports:
            reject("PUBLIC_PORT_FORBIDDEN", "non-ingress components must not expose public ports")
        if not isinstance(secret_refs, list) or not all(
            isinstance(item, str) and SECRET_REF.fullmatch(item) for item in secret_refs
        ):
            reject("COMPONENT_SECRET_REFERENCE_INVALID", "component secrets must be references")
        if name == "monitoring" and secret_refs:
            reject(
                "COMPONENT_SECRET_REFERENCE_INVALID",
                "read-only monitoring must not receive runtime secret references",
            )
        if name == "ingress_waf" and secret_refs != [tls_ref]:
            reject(
                "COMPONENT_SECRET_REFERENCE_INVALID",
                "ingress must use only the declared TLS secret reference",
            )
        if name not in {"monitoring", "ingress_waf"} and (
            not secret_refs or any(item not in required_refs for item in secret_refs)
        ):
            reject(
                "COMPONENT_SECRET_REFERENCE_INVALID",
                "runtime components require declared secret references",
            )
        identities.append(identity)

    if len(identities) != len(set(identities)) or backup_identity in identities:
        reject("IDENTITY_BOUNDARY_NOT_ISOLATED", "component and backup identities must be unique")

    release_policy = require_mapping(topology, "release_policy", "RELEASE_POLICY_REQUIRED")
    require_exact_keys(
        release_policy,
        {
            "approved_release_identities",
            "minimum_production_approvals",
            "minimum_rollback_approvals",
            "approval_max_age_hours",
        },
    )
    approved_identities = release_policy.get("approved_release_identities")
    if (
        not isinstance(approved_identities, list)
        or not approved_identities
        or not all(isinstance(identity, str) and identity for identity in approved_identities)
        or release_policy.get("minimum_production_approvals") != 2
        or release_policy.get("minimum_rollback_approvals") != 2
        or not isinstance(release_policy.get("approval_max_age_hours"), int)
        or release_policy["approval_max_age_hours"] <= 0
    ):
        reject("RELEASE_POLICY_REQUIRED", "approved identity and two-person policy are required")


def approval_groups_before_schema(manifest: dict[str, Any]) -> None:
    approvals = manifest.get("approvals")
    if not isinstance(approvals, dict):
        return
    production = approvals.get("production")
    if isinstance(production, list) and len(production) < 2:
        reject(
            "TWO_PERSON_AUTHORIZATION_REQUIRED",
            "production release requires two distinct approvers",
        )
    rollback = approvals.get("rollback")
    if isinstance(rollback, list) and len(rollback) < 2:
        reject(
            "TWO_PERSON_AUTHORIZATION_REQUIRED",
            "rollback requires two distinct approvers",
        )


def validate_schema(manifest: dict[str, Any], schema: dict[str, Any]) -> None:
    try:
        Draft202012Validator.check_schema(schema)
    except Exception:
        reject("SCHEMA_INVALID", "release manifest schema is invalid")
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(manifest),
        key=lambda error: [str(part) for part in error.absolute_path],
    )
    if errors:
        path = ".".join(str(part) for part in errors[0].absolute_path) or "root"
        reject("MANIFEST_SCHEMA", f"release manifest violates schema at {path}")


def parse_datetime(value: str, code: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        reject(code, "timestamp must be an RFC 3339 date-time")
    if parsed.tzinfo is None:
        reject(code, "timestamp must include a timezone")
    return parsed.astimezone(UTC)


def validate_approval(
    approval: dict[str, Any],
    *,
    expected_role: str,
    release_id: str,
    now: datetime,
    max_age: timedelta,
) -> None:
    if approval["role"] != expected_role or approval["release_id"] != release_id:
        reject("APPROVAL_SCOPE_MISMATCH", "approval role or release scope does not match")
    approved_at = parse_datetime(approval["approved_at"], "APPROVAL_MALFORMED")
    if approved_at > now:
        reject("APPROVAL_MALFORMED", "approval timestamp cannot be in the future")
    if now - approved_at > max_age:
        reject("APPROVAL_STALE", "approval exceeds the configured freshness window")


def validate_approvals(manifest: dict[str, Any], topology: dict[str, Any], now: datetime) -> None:
    release_id = manifest["release_id"]
    approvals = manifest["approvals"]
    max_age = timedelta(hours=topology["release_policy"]["approval_max_age_hours"])
    validate_approval(
        approvals["privacy"],
        expected_role="privacy-approver",
        release_id=release_id,
        now=now,
        max_age=max_age,
    )
    production = approvals["production"]
    production_identities = {approval["identity"] for approval in production}
    if len(production_identities) < 2:
        reject(
            "TWO_PERSON_AUTHORIZATION_REQUIRED",
            "production release requires two distinct approvers",
        )
    for approval in production:
        validate_approval(
            approval,
            expected_role="release-approver",
            release_id=release_id,
            now=now,
            max_age=max_age,
        )

    rollback = approvals["rollback"]
    rollback_identities = {approval["identity"] for approval in rollback}
    if len(rollback_identities) < 2:
        reject(
            "TWO_PERSON_AUTHORIZATION_REQUIRED",
            "rollback requires two distinct approvers",
        )
    for approval in rollback:
        validate_approval(
            approval,
            expected_role="rollback-approver",
            release_id=release_id,
            now=now,
            max_age=max_age,
        )


def validate_manifest_binding(
    manifest: dict[str, Any], topology: dict[str, Any], topology_raw: bytes
) -> None:
    topology_binding = manifest["topology"]
    environment = manifest["environment"]
    topology_environment = topology["environment"]
    expected_pairs = (
        (topology_binding["id"], topology["topology_id"]),
        (topology_binding["primary_domain"], topology["domain"]["primary"]),
        (topology_binding["tls_secret_ref"], topology["tls"]["secret_ref"]),
        (topology_binding["backup_target_ref"], topology["backup"]["target_ref"]),
        (environment["name"], topology_environment["name"]),
        (environment["identity"], topology_environment["identity"]),
        (environment["tenant_id"], topology_environment["tenant_id"]),
    )
    if any(left != right for left, right in expected_pairs):
        reject("MANIFEST_TOPOLOGY_MISMATCH", "manifest does not match topology identity")
    if topology_binding["sha256"] != hashlib.sha256(topology_raw).hexdigest():
        reject("MANIFEST_TOPOLOGY_MISMATCH", "manifest topology checksum does not match")

    topology_images = {
        name: component["image"] for name, component in topology["components"].items()
    }
    if manifest["images"] != topology_images or manifest["flags"] != topology["capabilities"]:
        reject("MANIFEST_TOPOLOGY_MISMATCH", "manifest images or flags do not match topology")

    release_identity = environment["release_identity"]
    if release_identity not in topology["release_policy"]["approved_release_identities"]:
        reject("RELEASE_IDENTITY_UNAPPROVED", "release identity is not allowlisted")


def validate_time_window(manifest: dict[str, Any], now: datetime) -> None:
    issued_at = parse_datetime(manifest["issued_at"], "MANIFEST_TIME_INVALID")
    expires_at = parse_datetime(manifest["expires_at"], "MANIFEST_TIME_INVALID")
    if issued_at > now or expires_at <= now or expires_at <= issued_at:
        reject("MANIFEST_STALE", "release manifest is not within its validity window")


def validate_files(
    manifest_path: Path, topology_path: Path, schema_path: Path, now: datetime
) -> ValidatedRelease:
    manifest, _ = load_json(manifest_path, "manifest")
    topology, topology_raw = load_json(topology_path, "topology")
    schema, _ = load_json(schema_path, "schema")
    reject_embedded_secret_material(manifest)
    reject_embedded_secret_material(topology)
    validate_topology(topology)
    approval_groups_before_schema(manifest)
    validate_schema(manifest, schema)
    validate_time_window(manifest, now)
    validate_approvals(manifest, topology, now)
    validate_manifest_binding(manifest, topology, topology_raw)
    return ValidatedRelease(manifest=manifest, topology=topology)


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser(
        description="Validate Gate 6 production release inputs without mutating production."
    )
    argument_parser.add_argument("--manifest", required=True, type=Path)
    argument_parser.add_argument("--topology", required=True, type=Path)
    argument_parser.add_argument("--schema", required=True, type=Path)
    argument_parser.add_argument(
        "--now",
        help="RFC 3339 validation time; defaults to current UTC time",
    )
    return argument_parser


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        now = (
            parse_datetime(arguments.now, "NOW_INVALID") if arguments.now else datetime.now(tz=UTC)
        )
        validated = validate_files(
            arguments.manifest,
            arguments.topology,
            arguments.schema,
            now,
        )
    except ValidationFailure as failure:
        print(f"preflight failed: {failure.code}: {failure.message}", file=sys.stderr)
        return 2
    print(f"preflight passed: {validated.manifest['release_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
