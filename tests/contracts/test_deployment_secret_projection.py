from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).parents[2]
SCHEMA_PATH = ROOT / "contracts" / "gate6" / "deployment-secret-projection-v1.0.schema.json"
VALID_EXAMPLE_PATH = (
    ROOT / "contracts" / "examples" / "gate6" / "deployment-secret-projection-valid.json"
)
INVALID_SECRET_VALUE_PATH = (
    ROOT
    / "contracts"
    / "examples"
    / "gate6"
    / "deployment-secret-projection-invalid-secret-value.json"
)

APPROVED_PROJECTIONS = {
    "postgres_password": ("text", 1, 4096, None),
    "postgres_observer_password": ("text", 1, 4096, None),
    "postgres_context_password": ("text", 1, 4096, None),
    "postgres_agent_password": ("text", 1, 4096, None),
    "postgres_media_password": ("text", 1, 4096, None),
    "mariadb_root_password": ("text", 1, 4096, None),
    "frappe_admin_password": ("text", 1, 4096, None),
    "frappe_demo_password": ("text", 1, 4096, None),
    "agent_api_bearer": ("text", 1, 4096, None),
    "context_api_bearer": ("text", 1, 4096, None),
    "context_client_bearer": ("text", 1, 4096, None),
    "cursor_hmac_key": ("text", 32, 4096, None),
    "tokenizer_hmac_key": ("bytes", 32, 32, 32),
    "mapping_vault_key": ("bytes", 32, 32, 32),
    "trusted_phrase_lexicon": ("closed_json", 1, 65536, None),
    "media_runtime_key": ("text", 1, 4096, None),
    "deepseek_api_key": ("text", 1, 4096, None),
    "frappe_materializer_api_key": ("text", 1, 4096, None),
    "frappe_materializer_api_secret": ("text", 1, 4096, None),
    "identity_hmac_key": ("bytes", 32, 32, 32),
    "frappe_identity_resolver_api_key": ("text", 1, 4096, None),
    "frappe_identity_resolver_api_secret": ("text", 1, 4096, None),
    "email_credential": ("closed_json", 1, 65536, None),
    "wecom_credential": ("closed_json", 1, 65536, None),
    "whatsapp_credential": ("closed_json", 1, 65536, None),
    "cloudflared_tunnel": ("closed_json", 1, 65536, None),
}


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _errors(schema: dict[str, Any], value: dict[str, Any]) -> list[str]:
    return [error.message for error in Draft202012Validator(schema).iter_errors(value)]


def _projection(value: dict[str, Any], logical_name: str) -> dict[str, Any]:
    projections = value["projections"]
    assert isinstance(projections, list)
    matches = [item for item in projections if item.get("logical_name") == logical_name]
    assert len(matches) == 1
    return matches[0]


def test_schema_is_draft_2020_12_and_valid_example_is_closed_metadata_only() -> None:
    schema = _json(SCHEMA_PATH)
    example = _json(VALID_EXAMPLE_PATH)

    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert _errors(schema, example) == []
    assert set(example) == {"schema_version", "site_id", "environment", "projections"}
    assert set(example["projections"][0]) <= {
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


def test_every_approved_logical_name_has_one_fixed_runtime_projection() -> None:
    schema = _json(SCHEMA_PATH)
    example = _json(VALID_EXAMPLE_PATH)

    assert {item["logical_name"] for item in example["projections"]} == set(APPROVED_PROJECTIONS)
    for logical_name, (kind, minimum, maximum, exact) in APPROVED_PROJECTIONS.items():
        item = _projection(example, logical_name)
        assert item["target_filename"] == f"/run/secrets/{logical_name}"
        assert item["kind"] == kind
        assert item["minimum_bytes"] == minimum
        assert item["maximum_bytes"] == maximum
        assert item.get("exact_bytes") == exact

        for field, unsafe in (
            ("target_filename", f"/run/secrets/{logical_name}-other"),
            ("kind", "bytes" if kind != "bytes" else "text"),
            ("minimum_bytes", minimum + 1),
            ("maximum_bytes", maximum + 1),
        ):
            mutated = copy.deepcopy(example)
            _projection(mutated, logical_name)[field] = unsafe
            assert _errors(schema, mutated), (logical_name, field)

        mutated = copy.deepcopy(example)
        target = _projection(mutated, logical_name)
        if exact is None:
            target["exact_bytes"] = maximum
        else:
            target["exact_bytes"] = exact + 1
        assert _errors(schema, mutated), (logical_name, "exact_bytes")


@pytest.mark.parametrize(
    "field",
    [
        "site_id",
        "environment",
        "logical_name",
        "target_filename",
        "kind",
        "minimum_bytes",
        "maximum_bytes",
        "component",
        "required",
        "platform_version_id",
    ],
)
def test_required_projection_metadata_cannot_be_omitted(field: str) -> None:
    schema = _json(SCHEMA_PATH)
    mutated = copy.deepcopy(_json(VALID_EXAMPLE_PATH))
    if field in {"site_id", "environment"}:
        del mutated[field]
    else:
        del mutated["projections"][0][field]

    assert _errors(schema, mutated)


@pytest.mark.parametrize(
    "forbidden_field",
    [
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
        "arbitrary",
    ],
)
def test_secret_material_references_hashes_uris_and_arbitrary_fields_are_rejected(
    forbidden_field: str,
) -> None:
    schema = _json(SCHEMA_PATH)
    mutated = copy.deepcopy(_json(VALID_EXAMPLE_PATH))
    mutated["projections"][0][forbidden_field] = "forbidden-metadata"

    assert _errors(schema, mutated)


def test_invalid_secret_value_example_is_rejected_for_the_intended_reason() -> None:
    schema = _json(SCHEMA_PATH)
    invalid = _json(INVALID_SECRET_VALUE_PATH)

    errors = _errors(schema, invalid)
    assert errors
    assert any(
        "Additional properties are not allowed" in error and "value" in error for error in errors
    )


def test_unknown_duplicate_or_missing_logical_names_are_rejected() -> None:
    schema = _json(SCHEMA_PATH)
    example = _json(VALID_EXAMPLE_PATH)

    unknown = copy.deepcopy(example)
    unknown["projections"][0]["logical_name"] = "unapproved_secret"
    duplicate = copy.deepcopy(example)
    duplicate["projections"][0] = copy.deepcopy(duplicate["projections"][1])
    missing = copy.deepcopy(example)
    missing["projections"].pop()

    assert _errors(schema, unknown)
    assert _errors(schema, duplicate)
    assert _errors(schema, missing)
