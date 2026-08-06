from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).parents[2]
FIXTURES_DIR = REPO_ROOT / "fixtures" / "gate1"
KINGDEE_DIR = REPO_ROOT / "fixtures" / "kingdee" / "gate1"
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))


def read_json(path: Path) -> Any:
    assert path.is_file(), f"missing fixture asset: {path.relative_to(REPO_ROOT)}"
    return json.loads(path.read_text(encoding="utf-8"))


def load_records() -> list[dict[str, Any]]:
    return read_json(FIXTURES_DIR / "records.json")


def record_fields(record: dict[str, Any]) -> dict[str, Any]:
    fields = record.get("fields")
    assert isinstance(fields, dict), f"record has no fields object: {record!r}"
    return fields


def records_by_doctype() -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for record in load_records():
        result.setdefault(record["doctype"], []).append(record)
    return result


def test_gate1_manifest_and_record_counts_are_synthetic() -> None:
    manifest = read_json(FIXTURES_DIR / "manifest.json")
    grouped = records_by_doctype()

    assert manifest["dataset"] == "gate1"
    assert manifest["synthetic"] is True
    assert manifest["demo"] is True
    assert manifest["seed"] == 20260806
    assert manifest["generated_at"] == "2026-08-06T00:00:00Z"
    assert (
        manifest["frappe_payload_sha256"]
        == hashlib.sha256((FIXTURES_DIR / "frappe_payload.json").read_bytes()).hexdigest()
    )
    assert len(grouped["GBOS Party Profile"]) >= 500
    assert len(grouped["GBOS Work Item"]) >= 200
    assert len(grouped["CRM Organization"]) == len(grouped["GBOS Party Profile"])

    for record in load_records():
        assert record["synthetic"] is True
        assert record["demo"] is True
        assert record["fixture_source"] == "gate1-synthetic"
        assert not {"synthetic", "demo", "site_id", "seed", "generated_at"}.intersection(
            record["fields"]
        )


def test_gate1_generation_is_byte_deterministic(tmp_path: Path) -> None:
    from fixtures.gate1.generate import write_fixtures

    first = write_fixtures(tmp_path / "first")
    second = write_fixtures(tmp_path / "second")

    assert first == second
    for relative_path in first:
        assert (tmp_path / "first" / relative_path).read_bytes() == (
            tmp_path / "second" / relative_path
        ).read_bytes()


def test_gate1_all_relationships_point_to_existing_records() -> None:
    records = load_records()
    known_ids = {record["name"] for record in records}
    for record in records:
        for child in record["fields"].get("candidates", []):
            known_ids.add(child["name"])
    link_fields = {
        "team",
        "crm_organization",
        "contact",
        "crm_lead",
        "crm_deal",
        "organization",
        "lead",
        "deal",
        "party_profile",
        "product_brief",
        "sample_project",
        "sample_iteration",
        "demand_signal",
        "received_from_contact",
        "reference_name",
        "subject_name",
        "assigned_to",
        "assigned_reviewer",
        "owner_user",
        "custom_esan_team",
        "custom_esan_party_profile",
    }

    for record in records:
        fields = record_fields(record)
        for field_name in link_fields:
            value = fields.get(field_name)
            if value is None:
                continue
            values = value if isinstance(value, list) else [value]
            assert all(item in known_ids for item in values), (
                record["name"],
                field_name,
                value,
            )


def test_gate1_contains_representative_closed_loop() -> None:
    grouped = records_by_doctype()
    deal = grouped["CRM Deal"][0]
    deal_name = deal["name"]
    brief = next(
        item for item in grouped["GBOS Product Brief"] if record_fields(item)["deal"] == deal_name
    )
    project = next(
        item
        for item in grouped["GBOS Sample Project"]
        if record_fields(item)["product_brief"] == brief["name"]
    )
    iteration = next(
        item
        for item in grouped["GBOS Sample Iteration"]
        if record_fields(item)["sample_project"] == project["name"]
    )
    shipment = next(
        item
        for item in grouped["GBOS Sample Shipment"]
        if record_fields(item)["sample_iteration"] == iteration["name"]
    )
    feedback = next(
        item
        for item in grouped["GBOS Sample Feedback"]
        if record_fields(item)["sample_iteration"] == iteration["name"]
    )
    demand = next(
        item
        for item in grouped["GBOS Demand Signal"]
        if record_fields(item)["origin_reference"].endswith(feedback["name"])
    )
    sourcing = next(
        item
        for item in grouped["GBOS Sourcing Event"]
        if record_fields(item)["demand_signal"] == demand["name"]
    )
    work = next(
        item
        for item in grouped["GBOS Work Item"]
        if record_fields(item)["reference_name"] == sourcing["name"]
    )
    review = next(
        item
        for item in grouped["GBOS Review Case"]
        if record_fields(item)["subject_name"] == work["name"]
    )
    assert shipment and feedback and demand and sourcing and review


def test_gate1_statuses_are_scoped_and_review_is_separate() -> None:
    allowed = read_json(FIXTURES_DIR / "status_allowlist.json")
    assert set(allowed) >= {"business_status", "review_status"}
    assert allowed["review_status"]

    observed_difference = False
    for record in load_records():
        fields = record_fields(record)
        if "business_status" in fields:
            assert fields["business_status"] in allowed["business_status"][record["doctype"]]
            assert fields["review_status"] in allowed["review_status"]
        if fields.get("business_status") != fields.get("review_status"):
            observed_difference = True
    assert observed_difference


def test_gate1_identifiers_are_synthetic_and_not_real_pii() -> None:
    records = load_records()
    email_pattern = re.compile(r"^[^@\s]+@example\.invalid$")
    phone_pattern = re.compile(r"^\+000-[0-9]{4}-[0-9]{4}$")
    gbos_name_pattern = re.compile(
        r"^(TEM|PTY|PRB|SAM|ITR|SHP|FDB|DEM|SRC|WRK|REV)-"
        r"[0-9A-HJKMNP-TV-Z]{26}$"
    )

    for record in records:
        if record["doctype"].startswith("GBOS "):
            assert gbos_name_pattern.fullmatch(record["name"])
        else:
            assert record["name"].startswith("CRM-") or record["doctype"] == "User"
        fields = record_fields(record)
        for key, value in fields.items():
            if key in {"email", "email_id"}:
                assert email_pattern.fullmatch(value)
            if key == "mobile_no":
                assert phone_pattern.fullmatch(value)
            if isinstance(value, str):
                assert "@gmail.com" not in value.lower()
                assert "@qq.com" not in value.lower()
                assert "@163.com" not in value.lower()


def test_fixture_business_dates_are_valid_iso_calendar_dates() -> None:
    date_fields = {
        "target_date",
        "started_on",
        "completed_on",
        "shipped_on",
        "delivered_on",
        "received_on",
        "needed_by",
        "due_date",
    }
    for record in load_records():
        for field_name in date_fields:
            value = record_fields(record).get(field_name)
            if value is not None:
                assert date.fromisoformat(value).isoformat() == value


def test_fixture_business_datetimes_use_frappe_database_format() -> None:
    for record in records_by_doctype()["GBOS Review Case"]:
        value = record_fields(record).get("decided_at")
        if value is not None:
            assert (
                datetime.strptime(value, "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S") == value
            )


def test_crm_payload_uses_only_frozen_v1_fields_and_statuses() -> None:
    grouped = records_by_doctype()

    for lead in grouped["CRM Lead"]:
        fields = record_fields(lead)
        assert "first_name" in fields
        assert "email" in fields
        assert "email_id" not in fields
        assert "contact" not in fields
        assert fields["status"] in {
            "Contacted",
            "Converted",
            "Junk",
            "New",
            "Nurture",
            "Qualified",
            "Unqualified",
        }
        if fields["status"] in {"Unqualified", "Junk"}:
            assert fields["lost_reason"] == "Poor Fit"
        else:
            assert "lost_reason" not in fields
    for deal in grouped["CRM Deal"]:
        fields = record_fields(deal)
        assert "deal_name" not in fields
        assert "deal_stage" not in fields
        assert fields["status"] in {
            "Demo/Making",
            "Lost",
            "Negotiation",
            "Proposal/Quotation",
            "Qualification",
            "Ready to Close",
            "Won",
        }
        if fields["status"] == "Lost":
            assert fields["lost_reason"] == "Competition"
        else:
            assert "lost_reason" not in fields
    for contact in grouped["Contact"]:
        assert "organization" not in record_fields(contact)


def test_fixture_users_have_roles_and_teams_have_enabled_members() -> None:
    grouped = records_by_doctype()
    users = {record["name"]: record_fields(record) for record in grouped["User"]}
    teams = grouped["GBOS Team"]

    required_roles = {
        "CEO",
        "Sales Manager",
        "Sales User",
        "Purchase Manager",
        "Buyer",
        "Product/R&D",
        "Reviewer",
        "Privacy/Audit",
    }
    observed_roles = {role["role"] for fields in users.values() for role in fields.get("roles", [])}
    assert observed_roles >= required_roles
    assert all(record_fields(team).get("members") for team in teams)
    assert all(
        member["enabled"] == 1 and member["user"] in users
        for team in teams
        for member in record_fields(team)["members"]
    )
    assert all(fields["send_welcome_email"] == 0 for fields in users.values())


def test_frappe_payload_strips_envelope_metadata_and_embeds_child_rows() -> None:
    from fixtures.gate1.generate import FRAPPE_FIELDS, build_dataset

    dataset = build_dataset()
    payload = dataset["frappe_payload"]
    assert payload
    assert not any(
        set(record).intersection({"synthetic", "demo", "fixture_source", "site_id", "seed"})
        for record in payload
    )
    event = next(record for record in payload if record["doctype"] == "GBOS Sourcing Event")
    assert len(event["candidates"]) == 3
    assert all(child["doctype"] == "GBOS Sourcing Candidate" for child in event["candidates"])
    for record in payload:
        allowed = FRAPPE_FIELDS[record["doctype"]] | {"doctype", "name"}
        assert set(record) <= allowed


def test_sourcing_fixture_selection_matches_the_server_state_rule() -> None:
    for event in records_by_doctype()["GBOS Sourcing Event"]:
        fields = record_fields(event)
        selected_rows = [
            candidate
            for candidate in fields["candidates"]
            if record_fields(candidate)["candidate_status"] == "Selected"
        ]
        if fields["business_status"] in {"Selected", "Closed"}:
            assert len(selected_rows) == 1
            assert fields["selected_supplier"] == record_fields(selected_rows[0])["supplier_name"]
        else:
            assert fields.get("selected_supplier") is None
            assert selected_rows == []


def test_kingdee_mock_exposes_only_read_methods_and_validates_payload() -> None:
    from fixtures.kingdee.gate1.mock import (
        READ_ONLY_METHODS,
        REQUEST_FIELDS,
        KingdeeMock,
    )

    assert len(READ_ONLY_METHODS) == 8
    assert not any(
        name.lower()
        in {"save", "submit", "audit", "unaudit", "delete", "execute_operation", "push"}
        for name in READ_ONLY_METHODS
    )
    assert set(REQUEST_FIELDS) >= {
        "request_id",
        "user_id",
        "tenant_id",
        "form_id",
        "field_keys",
        "filter",
        "order_by",
        "limit",
        "start_row",
        "permission_context",
    }

    mock = KingdeeMock()
    request = mock.default_request(form_id="BD_MATERIAL")
    response = mock.query_bill(request)
    assert response["success"] is True
    assert response["request_id"] == request["request_id"]
    assert response["source"] == {
        "system": "kingdee-gate1-synthetic",
        "form_id": "BD_MATERIAL",
        "query_time": "2026-08-06T00:00:00Z",
    }
    assert set(response) >= {"columns", "rows", "page"}
    assert response["page"]["limit"] == request["limit"]
    assert response["page"]["start_row"] == request["start_row"]

    with pytest.raises(ValueError):
        mock.invoke("save", request)
    with pytest.raises(ValueError):
        mock.query_bill({**request, "field_keys": []})
    with pytest.raises(ValueError):
        mock.query_bill({**request, "limit": 501})
    with pytest.raises(ValueError):
        mock.query_bill({**request, "start_row": -1})


def test_kingdee_mock_read_methods_return_controlled_responses() -> None:
    from fixtures.kingdee.gate1.mock import READ_ONLY_METHODS, KingdeeMock

    mock = KingdeeMock()
    request = mock.default_request(form_id="BD_MATERIAL")
    for method in sorted(READ_ONLY_METHODS):
        response = mock.invoke(method, request)
        assert response["success"] is True
        assert response["request_id"] == request["request_id"]
        assert response["source"]["query_time"] == "2026-08-06T00:00:00Z"
        assert isinstance(response["columns"], list)
        assert isinstance(response["rows"], list)
        assert set(response["page"]) == {"limit", "start_row", "has_more"}
