from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

ROOT = Path(__file__).parents[2]
EMAIL_GATEWAY = ROOT / "contracts" / "email_gateway"
EXAMPLES = EMAIL_GATEWAY / "examples" / "provider-neutral-v1.json"
WECOM_FIXTURES = (
    ROOT / "tests" / "fixtures" / "wecom_app_mail" / "official-inbound-fixtures-v1.json"
)

SCHEMAS = {
    "email-message-publication-v1.0.schema.json",
    "mailbox-connector-projection-v1.0.schema.json",
    "frappe-identity-projection-v1.0.schema.json",
    "frappe-route-authority-v1.0.schema.json",
    "email-address-match-attestation-v1.0.schema.json",
}

STANDALONE_SCHEMAS = {
    "mailbox-sla-policy-v1.0.schema.json",
    "mailbox-connector-projection-v2.0.schema.json",
    "email-send-approved-command-v2.0.schema.json",
}

WECOM_SCHEMAS = {
    "wecom-app-mail-callback-v1.0.schema.json",
    "wecom-app-mail-error-v1.0.schema.json",
    "wecom-app-mail-list-v1.0.schema.json",
    "wecom-app-mail-message-v1.0.schema.json",
    "wecom-app-mail-token-v1.0.schema.json",
}

WECOM_OFFICIAL_SOURCES = {
    "mail_overview": "https://developer.work.weixin.qq.com/document/path/95486",
    "callback_configuration": "https://developer.work.weixin.qq.com/document/path/90930",
    "receive_event": "https://developer.work.weixin.qq.com/document/path/97495",
    "access_token": "https://developer.work.weixin.qq.com/document/path/91039",
    "mail_list": "https://developer.work.weixin.qq.com/document/path/97369",
    "read_mail": "https://developer.work.weixin.qq.com/document/path/97979",
    "frequency_limits": "https://developer.work.weixin.qq.com/document/path/90312",
    "global_errors": "https://developer.work.weixin.qq.com/document/path/90313",
}

WECOM_EXPECTED_CASES = {
    "wecom-app-mail-callback-v1.0.schema.json": {
        "valid": {
            "url_verification",
            "encrypted_receive_event",
            "replayed_receive_event",
        },
        "invalid": {
            "malformed_signature",
            "malformed_encrypted_xml",
            "malformed_decrypted_xml",
            "unknown_event",
            "oversized_encrypt",
            "receive_event_claims_mail_id",
            "additional_field",
        },
    },
    "wecom-app-mail-token-v1.0.schema.json": {
        "valid": {"successful_token"},
        "invalid": {
            "nonzero_errcode",
            "malformed_token",
            "oversized_token",
            "additional_field",
        },
    },
    "wecom-app-mail-list-v1.0.schema.json": {
        "valid": {"paginated_page", "empty_page"},
        "invalid": {
            "missing_mail_id",
            "unknown_has_more",
            "oversized_cursor",
            "malformed_mail_list",
            "item_additional_field",
            "additional_field",
        },
    },
    "wecom-app-mail-message-v1.0.schema.json": {
        "valid": {"full_eml"},
        "invalid": {
            "malformed_errcode",
            "malformed_eml",
            "oversized_eml",
            "additional_field",
        },
    },
    "wecom-app-mail-error-v1.0.schema.json": {
        "valid": {
            "rate_limit_45009",
            "invalid_token_40014",
            "expired_token_42001",
            "authorization_invalid_48004",
            "permission_reclaimed_48006",
            "application_disabled_50003",
            "application_prohibited_60031",
        },
        "invalid": {
            "success_errcode",
            "unknown_errcode",
            "oversized_errmsg",
            "invented_http_429",
            "invented_retry_after",
            "additional_field",
        },
    },
}

EXPECTED_CASES = {
    "email-message-publication-v1.0.schema.json": {
        "valid": {"subject_projection", "subject_digest"},
        "invalid": {
            "raw_email",
            "raw_phone",
            "raw_provider_id",
            "duplicate_address_role",
            "unknown_role",
            "unbounded_subject",
            "bad_publication_ulid",
            "bad_digest",
            "bad_revision",
            "additional_property",
        },
    },
    "mailbox-connector-projection-v1.0.schema.json": {
        "valid": {"primary_inbound"},
        "invalid": {
            "unknown_provider",
            "unknown_entry_role",
            "bad_config_revision",
            "bad_mailbox_ulid",
            "unbounded_string",
            "additional_property",
        },
    },
    "frappe-identity-projection-v1.0.schema.json": {
        "valid": {"employee", "customer"},
        "invalid": {
            "raw_address",
            "raw_subject",
            "unknown_identity_type",
            "bad_external_identity_ulid",
            "bad_revision",
            "unknown_purpose",
            "unbounded_team",
            "additional_property",
        },
    },
    "frappe-route-authority-v1.0.schema.json": {
        "valid": {"assigned", "unassigned"},
        "invalid": {
            "hybrid_shape",
            "unknown_status",
            "unknown_reason",
            "bad_party_ulid",
            "bad_revision",
            "bad_digest",
            "additional_property",
        },
    },
    "email-address-match-attestation-v1.0.schema.json": {
        "valid": {"matched", "not_matched"},
        "invalid": {
            "raw_email",
            "raw_phone",
            "raw_provider_id",
            "unknown_target_type",
            "bad_target_ulid",
            "bad_digest",
            "unknown_normalization",
            "unbounded_evidence",
            "additional_property",
        },
    },
}


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _schema(filename: str) -> dict[str, Any]:
    return _load(EMAIL_GATEWAY / filename)


def _validator(filename: str) -> Draft202012Validator:
    schema = _schema(filename)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _examples() -> dict[str, Any]:
    return _load(EXAMPLES)


def _wecom_fixtures() -> dict[str, Any]:
    return _load(WECOM_FIXTURES)


def _case(filename: str, outcome: str, name: str) -> dict[str, Any]:
    value = _examples()["cases"][filename][outcome][name]
    assert isinstance(value, dict)
    return copy.deepcopy(value)


def _walk_schema(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_schema(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_schema(value)


def _scoped_validator(
    filename: str, *, site_id: str, team_ref: str, processing_purpose: str
) -> Draft202012Validator:
    scope_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "allOf": [
            _schema(filename),
            {
                "type": "object",
                "properties": {
                    "site_id": {"const": site_id},
                    "team_ref": {"const": team_ref},
                    "business_purpose": {"const": processing_purpose},
                    "processing_purpose": {"const": processing_purpose},
                },
            },
        ],
    }
    return Draft202012Validator(scope_schema, format_checker=FormatChecker())


def test_email_gateway_contract_set_is_exact_and_valid_2020_12() -> None:
    actual = {path.name for path in EMAIL_GATEWAY.glob("*.schema.json")}
    assert actual == SCHEMAS | STANDALONE_SCHEMAS | WECOM_SCHEMAS

    for filename in sorted(actual):
        schema = _schema(filename)
        Draft202012Validator.check_schema(schema)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_all_object_shapes_are_closed_and_all_strings_are_bounded() -> None:
    for filename in sorted(SCHEMAS | WECOM_SCHEMAS):
        for node in _walk_schema(_schema(filename)):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False, (filename, node)
            if node.get("type") == "string":
                assert "maxLength" in node, (filename, node)
                assert node["maxLength"] > 0


def test_wecom_contract_artifacts_exist_before_the_freeze_can_be_green() -> None:
    missing = [
        str(path.relative_to(ROOT))
        for path in [
            *(EMAIL_GATEWAY / filename for filename in sorted(WECOM_SCHEMAS)),
            WECOM_FIXTURES,
        ]
        if not path.is_file()
    ]
    assert missing == [], f"missing WeCom contract artifacts: {missing}"


def test_wecom_fixture_manifest_has_exact_first_party_provenance() -> None:
    fixtures = _wecom_fixtures()
    assert set(fixtures) == {"schema_version", "provenance", "sanitization", "cases"}
    assert fixtures["schema_version"] == "1.0"
    assert fixtures["provenance"]["authority"] == "first-party WeCom documentation"
    assert fixtures["provenance"]["accessed_on"] == "2026-08-14"
    assert fixtures["provenance"]["timezone"] == "Asia/Shanghai"
    assert fixtures["provenance"]["sources"] == WECOM_OFFICIAL_SOURCES

    sanitization = fixtures["sanitization"]
    assert sanitization == {
        "synthetic_values_only": True,
        "contains_real_corp_or_app_id": False,
        "contains_real_secret_or_token": False,
        "contains_real_mailbox_address": False,
        "contains_customer_or_real_eml": False,
    }


def test_wecom_fixture_manifest_has_exact_named_cases() -> None:
    cases = _wecom_fixtures()["cases"]
    assert set(cases) == WECOM_SCHEMAS

    for filename, expected in WECOM_EXPECTED_CASES.items():
        assert set(cases[filename]) == {"valid", "invalid"}
        assert set(cases[filename]["valid"]) == expected["valid"]
        assert set(cases[filename]["invalid"]) == expected["invalid"]


@pytest.mark.parametrize("filename", sorted(WECOM_SCHEMAS))
def test_wecom_named_valid_fixtures_pass_and_invalid_fixtures_fail(filename: str) -> None:
    validator = _validator(filename)
    cases = _wecom_fixtures()["cases"][filename]

    for name, value in cases["valid"].items():
        errors = list(validator.iter_errors(value))
        assert errors == [], (name, errors)

    for _name, value in cases["invalid"].items():
        with pytest.raises(ValidationError, match=".+"):
            validator.validate(value)


def test_wecom_callback_is_an_encrypted_xml_count_only_wake_signal() -> None:
    callback_schema = _schema("wecom-app-mail-callback-v1.0.schema.json")
    assert callback_schema["$comment"] == (
        "Wake signal only: Amount is a count hint; no callback field is a delivery or cursor."
    )

    callback_cases = _wecom_fixtures()["cases"][callback_schema["$id"].rsplit("/", 1)[-1]]
    receive_event = callback_cases["valid"]["encrypted_receive_event"]
    assert receive_event["http_method"] == "POST"
    assert receive_event["content_type"] == "application/xml"
    assert receive_event["decrypted_event"]["Amount"] == 2
    assert "mail_id" not in receive_event["decrypted_event"]
    assert "cursor" not in receive_event["decrypted_event"]


def test_only_pulled_mail_list_entries_supply_stable_delivery_ids() -> None:
    cases = _wecom_fixtures()["cases"]["wecom-app-mail-list-v1.0.schema.json"]
    page = cases["valid"]["paginated_page"]
    empty = cases["valid"]["empty_page"]

    assert page["has_more"] == 1
    assert page["next_cursor"]
    assert [item["mail_id"] for item in page["mail_list"]] == [
        "synthetic-mail-id-0001",
        "synthetic-mail-id-0002",
    ]
    assert empty == {
        "errcode": 0,
        "errmsg": "ok",
        "next_cursor": "",
        "has_more": 0,
        "mail_list": [],
    }


def test_45009_is_json_only_and_freezes_no_retry_timing() -> None:
    cases = _wecom_fixtures()["cases"]["wecom-app-mail-error-v1.0.schema.json"]
    rate_limit = cases["valid"]["rate_limit_45009"]
    assert rate_limit == {"errcode": 45009, "errmsg": "synthetic rate limit message"}
    assert "http_status" not in rate_limit
    assert "retry_after" not in rate_limit

    validator = _validator("wecom-app-mail-error-v1.0.schema.json")
    for case_name in ("invented_http_429", "invented_retry_after"):
        with pytest.raises(ValidationError):
            validator.validate(cases["invalid"][case_name])


def test_wecom_wire_bounds_reject_actual_oversized_values() -> None:
    fixtures = _wecom_fixtures()["cases"]
    mutations = (
        (
            "wecom-app-mail-callback-v1.0.schema.json",
            "encrypted_receive_event",
            "encrypted_envelope_xml",
            "x" * 65537,
        ),
        (
            "wecom-app-mail-token-v1.0.schema.json",
            "successful_token",
            "access_token",
            "x" * 513,
        ),
        (
            "wecom-app-mail-list-v1.0.schema.json",
            "paginated_page",
            "next_cursor",
            "x" * 257,
        ),
        (
            "wecom-app-mail-message-v1.0.schema.json",
            "full_eml",
            "mail_data",
            "x" * 1025,
        ),
        (
            "wecom-app-mail-error-v1.0.schema.json",
            "rate_limit_45009",
            "errmsg",
            "x" * 1025,
        ),
    )
    for filename, case_name, field_name, oversized in mutations:
        value = copy.deepcopy(fixtures[filename]["valid"][case_name])
        value[field_name] = oversized
        with pytest.raises(ValidationError):
            _validator(filename).validate(value)


def test_example_manifest_has_exact_named_cases_for_every_schema() -> None:
    examples = _examples()
    assert examples["schema_version"] == "1.0"
    assert set(examples) == {"schema_version", "cases"}
    assert set(examples["cases"]) == SCHEMAS

    for filename, outcomes in EXPECTED_CASES.items():
        assert set(examples["cases"][filename]) == {"valid", "invalid"}
        assert set(examples["cases"][filename]["valid"]) == outcomes["valid"]
        assert set(examples["cases"][filename]["invalid"]) == outcomes["invalid"]


@pytest.mark.parametrize("filename", sorted(SCHEMAS))
def test_named_valid_examples_pass_and_invalid_examples_fail(filename: str) -> None:
    validator = _validator(filename)
    cases = _examples()["cases"][filename]

    for name, value in cases["valid"].items():
        errors = list(validator.iter_errors(value))
        assert errors == [], (name, errors)

    for value in cases["invalid"].values():
        with pytest.raises(ValidationError, match=".+"):
            validator.validate(value)


def test_publication_has_only_the_frozen_content_minimized_fields() -> None:
    schema = _schema("email-message-publication-v1.0.schema.json")
    assert set(schema["properties"]) == {
        "publication_id",
        "site_id",
        "mailbox_id",
        "mailbox_config_revision",
        "observer_connector_instance_ref",
        "observer_delivery_ref",
        "received_at",
        "participants",
        "subject_projection",
        "subject_digest",
        "header_digests",
        "evidence_refs",
        "publication_revision",
        "idempotency_key",
    }
    participant = schema["properties"]["participants"]["items"]
    assert set(participant["properties"]) == {"address_role", "identity_ref"}


def test_publication_rejects_raw_addresses_provider_ids_and_duplicate_roles() -> None:
    validator = _validator("email-message-publication-v1.0.schema.json")
    valid = _case("email-message-publication-v1.0.schema.json", "valid", "subject_projection")

    invalid_identity_refs = (
        "sales@example.invalid",
        "+8613800138000",
        "provider-message-000123",
    )
    for identity_ref in invalid_identity_refs:
        invalid = copy.deepcopy(valid)
        invalid["participants"][0]["identity_ref"] = identity_ref
        with pytest.raises(ValidationError):
            validator.validate(invalid)

    duplicate = copy.deepcopy(valid)
    duplicate["participants"].append(copy.deepcopy(duplicate["participants"][0]))
    with pytest.raises(ValidationError):
        validator.validate(duplicate)


def test_publication_allows_only_digest_headers_and_one_bounded_subject_form() -> None:
    validator = _validator("email-message-publication-v1.0.schema.json")
    valid = _case("email-message-publication-v1.0.schema.json", "valid", "subject_projection")

    raw_header = copy.deepcopy(valid)
    raw_header["header_digests"]["message_id"] = "<provider-message@example.invalid>"
    with pytest.raises(ValidationError):
        validator.validate(raw_header)

    both_subject_forms = copy.deepcopy(valid)
    both_subject_forms["subject_digest"] = "sha256:" + "a" * 64
    with pytest.raises(ValidationError):
        validator.validate(both_subject_forms)

    no_subject_form = copy.deepcopy(valid)
    no_subject_form.pop("subject_projection")
    with pytest.raises(ValidationError):
        validator.validate(no_subject_form)


def test_mailbox_activation_watermark_owns_the_exact_mailbox_config_revision() -> None:
    schema = _schema("mailbox-connector-projection-v1.0.schema.json")
    watermark = schema["properties"]["activation_watermark"]
    assert set(watermark["properties"]) == {
        "mailbox_id",
        "mailbox_config_revision",
        "not_before",
    }
    assert set(watermark["required"]) == set(watermark["properties"])
    assert "mailbox_config_revision" not in schema["properties"]

    validator = _validator("mailbox-connector-projection-v1.0.schema.json")
    valid = _case("mailbox-connector-projection-v1.0.schema.json", "valid", "primary_inbound")
    invalid = copy.deepcopy(valid)
    invalid["activation_watermark"]["mailbox_config_revision"] = 0
    with pytest.raises(ValidationError):
        validator.validate(invalid)


def test_mailbox_connector_projection_v2_only_adds_required_opaque_mailbox_identity() -> None:
    frozen_v1 = _schema("mailbox-connector-projection-v1.0.schema.json")
    v2 = _schema("mailbox-connector-projection-v2.0.schema.json")

    assert set(v2["properties"]) == {
        *frozen_v1["properties"],
        "mailbox_address_identity_ref",
    }
    assert set(v2["required"]) == {
        *frozen_v1["required"],
        "mailbox_address_identity_ref",
    }
    assert v2["properties"]["mailbox_address_identity_ref"] == {
        "type": "string",
        "minLength": 58,
        "maxLength": 58,
        "pattern": "^extid:v1:email:[A-Za-z0-9_-]{43}$",
    }
    validator = _validator("mailbox-connector-projection-v2.0.schema.json")
    valid = _case("mailbox-connector-projection-v1.0.schema.json", "valid", "primary_inbound")
    validator.validate(
        {
            **valid,
            "mailbox_address_identity_ref": "extid:v1:email:" + "M" * 43,
        }
    )
    with pytest.raises(ValidationError):
        validator.validate(valid)


def test_identity_projection_is_closed_and_never_contains_raw_subject_or_address() -> None:
    schema = _schema("frappe-identity-projection-v1.0.schema.json")
    assert set(schema["properties"]) == {
        "site_id",
        "processing_purpose",
        "opaque_address_ref",
        "external_identity_ref",
        "external_identity_revision",
        "identity_type",
        "team_ref",
        "status",
        "projection_receipt",
        "observed_at",
    }
    forbidden_names = {"email", "phone", "address", "subject", "external_subject", "target_ref"}
    assert forbidden_names.isdisjoint(schema["properties"])

    validator = _validator("frappe-identity-projection-v1.0.schema.json")
    valid = _case("frappe-identity-projection-v1.0.schema.json", "valid", "employee")
    for field, raw_value in (
        ("address", "sales@example.invalid"),
        ("phone", "+8613800138000"),
        ("external_subject", "provider-user-123"),
    ):
        with pytest.raises(ValidationError):
            validator.validate({**valid, field: raw_value})


def test_identity_projection_reuses_governed_purposes_and_closed_target_types() -> None:
    validator = _validator("frappe-identity-projection-v1.0.schema.json")
    employee = _case("frappe-identity-projection-v1.0.schema.json", "valid", "employee")
    customer = _case("frappe-identity-projection-v1.0.schema.json", "valid", "customer")

    with pytest.raises(ValidationError):
        validator.validate({**employee, "identity_type": "Contact"})
    with pytest.raises(ValidationError):
        validator.validate({**customer, "processing_purpose": "arbitrary_email_use"})


@pytest.mark.parametrize(
    ("filename", "valid_case", "purpose_field"),
    [
        ("mailbox-connector-projection-v1.0.schema.json", "primary_inbound", "business_purpose"),
        (
            "frappe-identity-projection-v1.0.schema.json",
            "employee",
            "processing_purpose",
        ),
    ],
)
def test_consumer_scope_rejects_mismatched_site_team_or_purpose(
    filename: str, valid_case: str, purpose_field: str
) -> None:
    value = _case(filename, "valid", valid_case)
    purpose = value[purpose_field]
    validator = _scoped_validator(
        filename,
        site_id=value["site_id"],
        team_ref=value["team_ref"],
        processing_purpose=purpose,
    )
    validator.validate(value)

    for field, mismatched in (
        ("site_id", "other.example"),
        ("team_ref", "TEM-01KZQB094BY8XFHEBB3ENN6TAX"),
        (purpose_field, "customer_service"),
    ):
        invalid = {**value, field: mismatched}
        with pytest.raises(ValidationError):
            validator.validate(invalid)


def test_route_authority_is_exactly_one_closed_assigned_or_unassigned_shape() -> None:
    schema = _schema("frappe-route-authority-v1.0.schema.json")
    assert set(schema) >= {"$schema", "$id", "title", "description", "oneOf"}
    assert len(schema["oneOf"]) == 2
    assert all(branch["additionalProperties"] is False for branch in schema["oneOf"])

    assigned = schema["oneOf"][0]
    unassigned = schema["oneOf"][1]
    assert set(assigned["properties"]) == {
        "route_status",
        "party_ref",
        "party_revision",
        "team_ref",
        "team_revision",
        "owner_user_ref",
        "owner_eligibility_revision",
        "resolved_at",
    }
    assert set(unassigned["properties"]) == {
        "route_status",
        "safe_reason_code",
        "resolved_at",
    }


def test_route_authority_rejects_hybrid_and_unknown_safe_reason_shapes() -> None:
    validator = _validator("frappe-route-authority-v1.0.schema.json")
    assigned = _case("frappe-route-authority-v1.0.schema.json", "valid", "assigned")
    unassigned = _case("frappe-route-authority-v1.0.schema.json", "valid", "unassigned")

    with pytest.raises(ValidationError):
        validator.validate({**assigned, "safe_reason_code": "owner_unavailable"})
    with pytest.raises(ValidationError):
        validator.validate({**unassigned, "safe_reason_code": "provider said alice@example.com"})


def test_address_match_attestation_has_only_opaque_minimum_fields() -> None:
    schema = _schema("email-address-match-attestation-v1.0.schema.json")
    assert set(schema["properties"]) == {
        "opaque_address_ref",
        "candidate_target_ref",
        "candidate_target_type",
        "evidence_ref",
        "normalization_version",
        "matched",
        "observed_at",
        "expires_at",
        "digest",
    }
    assert set(schema["required"]) == set(schema["properties"])

    validator = _validator("email-address-match-attestation-v1.0.schema.json")
    valid = _case("email-address-match-attestation-v1.0.schema.json", "valid", "matched")
    for field, raw_value in (
        ("opaque_address_ref", "sales@example.invalid"),
        ("opaque_address_ref", "+8613800138000"),
        ("candidate_target_ref", "provider-user-123"),
    ):
        invalid = {**valid, field: raw_value}
        with pytest.raises(ValidationError):
            validator.validate(invalid)
