from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

ROOT = Path(__file__).parents[2]
CONTRACTS = ROOT / "contracts"
LOCAL_PILOT = CONTRACTS / "local_pilot"

SCHEMAS = {
    "canonical-observation-event-v1.1.schema.json",
    "connector-checkpoint-v1.1.schema.json",
    "inbound-delivery-v1.0.schema.json",
    "model-invocation-v1.0.schema.json",
    "local-pilot-manifest-v1.0.schema.json",
    "tokenization-receipt-v1.0.schema.json",
    "upload-receipt-v1.0.schema.json",
    "transcript-segments-v1.0.schema.json",
    "sales-proposal-v1.0.schema.json",
    "purchase-proposal-v1.0.schema.json",
    "product-proposal-v1.0.schema.json",
    "ceo-proposal-v1.0.schema.json",
}


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _validator(filename: str) -> Draft202012Validator:
    schema = _load(LOCAL_PILOT / filename)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def test_frozen_v1_contracts_remain_byte_identical_and_valid() -> None:
    expected_digests = {
        "canonical-observation-event.schema.json": (
            "56e9796c8f05ad00b408f70d10a3c9c17401333617b2ffe29f223a8d845a3de9"
        ),
        "connector-checkpoint.schema.json": (
            "d584c875976da6b86adfcc6e978d10a73bc847df20181381943accc3a4524ad0"
        ),
    }
    for filename, expected in expected_digests.items():
        raw = (CONTRACTS / filename).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == expected
        Draft202012Validator.check_schema(json.loads(raw))


def test_local_pilot_contract_set_is_complete_and_valid_2020_12() -> None:
    actual = {path.name for path in LOCAL_PILOT.glob("*.schema.json")}
    assert actual == SCHEMAS
    for filename in actual:
        schema = _load(LOCAL_PILOT / filename)
        Draft202012Validator.check_schema(schema)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"


@pytest.mark.parametrize("filename", sorted(SCHEMAS))
def test_valid_examples_pass_and_invalid_examples_fail(filename: str) -> None:
    stem = filename.removesuffix(".schema.json")
    validator = _validator(filename)

    validator.validate(_load(LOCAL_PILOT / "examples" / "valid" / f"{stem}.json"))
    with pytest.raises(ValidationError):
        validator.validate(_load(LOCAL_PILOT / "examples" / "invalid" / f"{stem}.json"))


def test_v11_observation_preserves_v1_enums_and_adds_only_pilot_consent() -> None:
    v1 = _load(CONTRACTS / "canonical-observation-event.schema.json")
    v11 = _load(LOCAL_PILOT / "canonical-observation-event-v1.1.schema.json")

    assert v11["properties"]["connector"] == v1["properties"]["connector"]
    assert v11["properties"]["channel"] == v1["properties"]["channel"]
    assert set(v11["properties"]["consent_basis"]["enum"]) == {
        *v1["properties"]["consent_basis"]["enum"],
        "pilot_deferred_review",
    }
    assert v11["properties"]["schema_version"] == {"const": "1.1"}
    assert "connector_instance_id" in v11["required"]


def test_v11_checkpoint_requires_instance_and_non_negative_version() -> None:
    valid = _load(LOCAL_PILOT / "examples" / "valid" / "connector-checkpoint-v1.1.json")
    validator = _validator("connector-checkpoint-v1.1.schema.json")

    without_instance = dict(valid)
    without_instance.pop("connector_instance_id")
    with pytest.raises(ValidationError):
        validator.validate(without_instance)

    negative_version = dict(valid, checkpoint_version=-1)
    with pytest.raises(ValidationError):
        validator.validate(negative_version)


def test_local_pilot_manifest_keeps_deferred_capabilities_closed_and_budget_explicit() -> None:
    manifest = _load(LOCAL_PILOT / "examples" / "valid" / "local-pilot-manifest-v1.0.json")
    validator = _validator("local-pilot-manifest-v1.0.schema.json")

    validator.validate(manifest)
    assert manifest["compliance_state"] == "pilot_deferred_review"
    assert manifest["retention_days"] == 30
    assert manifest["production_go"] is False
    assert manifest["capabilities"] == {
        "kingdee": False,
        "cloud_server": False,
        "cloud_business_storage": False,
        "external_send": False,
        "formal_business_commands": False,
    }
    assert manifest["deepseek"]["base_url"] == "https://api.deepseek.com"
    assert manifest["deepseek"]["model"] == "deepseek-v4-flash"
    assert manifest["deepseek"]["soft_limit_usd"] == 50
    assert manifest["deepseek"]["hard_limit_usd"] == 100
    for channel in ("email", "wecom", "whatsapp"):
        assert manifest["channels"][channel]["backfill_history"] is False


def test_model_invocation_allows_unknown_tokens_but_not_implicit_zero_or_content() -> None:
    valid = _load(LOCAL_PILOT / "examples" / "valid" / "model-invocation-v1.0.json")
    validator = _validator("model-invocation-v1.0.schema.json")
    assert valid["token_usage"] == {"status": "unknown"}
    validator.validate(valid)

    for forbidden in ("prompt", "response", "pii", "secret", "api_key"):
        with pytest.raises(ValidationError):
            validator.validate({**valid, forbidden: "must not cross this boundary"})

    missing_status = {**valid, "token_usage": {}}
    with pytest.raises(ValidationError):
        validator.validate(missing_status)


def test_delivery_and_token_receipt_exclude_raw_and_plaintext_material() -> None:
    cases = (
        (
            "inbound-delivery-v1.0.schema.json",
            "inbound-delivery-v1.0.json",
            ("raw_body", "body", "payload"),
        ),
        (
            "tokenization-receipt-v1.0.schema.json",
            "tokenization-receipt-v1.0.json",
            ("plaintext_mapping", "mapping", "token_values"),
        ),
    )
    for schema_name, example_name, forbidden_fields in cases:
        valid = _load(LOCAL_PILOT / "examples" / "valid" / example_name)
        validator = _validator(schema_name)
        for field in forbidden_fields:
            with pytest.raises(ValidationError):
                validator.validate({**valid, field: "forbidden"})


@pytest.mark.parametrize(
    ("schema_name", "example_name", "action_type"),
    (
        (
            "sales-proposal-v1.0.schema.json",
            "sales-proposal-v1.0.json",
            "internal.work_item.propose",
        ),
        (
            "purchase-proposal-v1.0.schema.json",
            "purchase-proposal-v1.0.json",
            "internal.review_case.propose",
        ),
        (
            "product-proposal-v1.0.schema.json",
            "product-proposal-v1.0.json",
            "internal.work_item.propose",
        ),
        (
            "ceo-proposal-v1.0.schema.json",
            "ceo-proposal-v1.0.json",
            "internal.ai_draft.propose",
        ),
    ),
)
def test_agent_outputs_are_closed_internal_proposals(
    schema_name: str,
    example_name: str,
    action_type: str,
) -> None:
    valid = _load(LOCAL_PILOT / "examples" / "valid" / example_name)
    validator = _validator(schema_name)
    validator.validate(valid)
    assert valid["action_type"] == action_type

    forbidden_fields = (
        "external_send",
        "formal_price",
        "payment_terms",
        "delivery_date",
        "order",
        "won_lost",
        "official_kpi",
    )
    for field in forbidden_fields:
        with pytest.raises(ValidationError):
            validator.validate({**valid, field: "forbidden"})
        with pytest.raises(ValidationError):
            validator.validate({**valid, "payload": {**valid["payload"], field: "forbidden"}})
