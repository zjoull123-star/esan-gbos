"""Build deterministic, synthetic Gate 1 fixtures.

The canonical JSON is an auditable envelope with ``doctype``, ``name`` and an
explicit ``fields`` object.  ``frappe_payload.json`` is generated alongside it
and strips the envelope metadata, flattening fields exactly as Frappe expects;
the Sourcing Candidate child rows remain embedded under their Sourcing Event.

No wall clock, random UUID, network call, or external data source is used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

SEED = 20260806
DEMO_TIME = "2026-08-06T00:00:00Z"
SITE_ID = "gbos.localhost"
FIXTURE_SOURCE = "gate1-synthetic"
PARTY_COUNT = 500
ORGANIZATION_COUNT = PARTY_COUNT
TEAM_COUNT = 5
CHAIN_COUNT = 240
EXTRA_WORK_ITEM_COUNT = 40
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
GBOS_PREFIXES = {
    "GBOS Team": "TEM",
    "GBOS Party Profile": "PTY",
    "GBOS Product Brief": "PRB",
    "GBOS Sample Project": "SAM",
    "GBOS Sample Iteration": "ITR",
    "GBOS Sample Shipment": "SHP",
    "GBOS Sample Feedback": "FDB",
    "GBOS Demand Signal": "DEM",
    "GBOS Sourcing Event": "SRC",
    "GBOS Work Item": "WRK",
    "GBOS Review Case": "REV",
}
DEMO_USERS: tuple[tuple[str, str, tuple[str, ...], tuple[int, ...]], ...] = (
    (
        "synthetic.sales.1@example.invalid",
        "Synthetic Sales 1",
        ("Sales User",),
        (0,),
    ),
    (
        "synthetic.sales.2@example.invalid",
        "Synthetic Sales 2",
        ("Sales User",),
        (1,),
    ),
    (
        "synthetic.sales.3@example.invalid",
        "Synthetic Sales 3",
        ("Sales User",),
        (2,),
    ),
    (
        "synthetic.sales.4@example.invalid",
        "Synthetic Sales 4",
        ("Sales User",),
        (3,),
    ),
    (
        "synthetic.sales.5@example.invalid",
        "Synthetic Sales 5",
        ("Sales User",),
        (4,),
    ),
    (
        "synthetic.sales.manager@example.invalid",
        "Synthetic Sales Manager",
        ("Sales Manager",),
        (0,),
    ),
    (
        "synthetic.purchase.manager@example.invalid",
        "Synthetic Purchase Manager",
        ("Purchase Manager",),
        (0, 1, 2, 3, 4),
    ),
    (
        "synthetic.buyer@example.invalid",
        "Synthetic Buyer",
        ("Buyer",),
        (0, 1, 2, 3, 4),
    ),
    (
        "synthetic.product@example.invalid",
        "Synthetic Product",
        ("Product/R&D",),
        (0, 1, 2, 3, 4),
    ),
    (
        "synthetic.reviewer.1@example.invalid",
        "Synthetic Reviewer 1",
        ("Reviewer",),
        (),
    ),
    (
        "synthetic.reviewer.2@example.invalid",
        "Synthetic Reviewer 2",
        ("Reviewer",),
        (),
    ),
    (
        "synthetic.ceo@example.invalid",
        "Synthetic CEO",
        ("CEO",),
        (),
    ),
    (
        "synthetic.audit@example.invalid",
        "Synthetic Privacy Audit",
        ("Privacy/Audit",),
        (),
    ),
)

TOP_LEVEL_DOCTYPES: tuple[str, ...] = (
    "GBOS Team",
    "GBOS Party Profile",
    "CRM Organization",
    "Contact",
    "CRM Lead",
    "CRM Deal",
    "GBOS Product Brief",
    "GBOS Sample Project",
    "GBOS Sample Iteration",
    "GBOS Sample Shipment",
    "GBOS Sample Feedback",
    "GBOS Demand Signal",
    "GBOS Sourcing Event",
    "GBOS Work Item",
    "GBOS Review Case",
    "User",
)
CHILD_DOCTYPES: tuple[str, ...] = ("GBOS Sourcing Candidate",)

REVIEW_STATUSES: tuple[str, ...] = (
    "AI Draft",
    "Pending",
    "Approved",
    "Rejected",
    "Superseded",
)

BUSINESS_STATUS_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "GBOS Team": ("Active", "Inactive", "Archived"),
    "GBOS Party Profile": ("Active", "Inactive", "Archived"),
    "GBOS Product Brief": ("Draft", "Active", "Archived"),
    "GBOS Sample Project": (
        "Draft",
        "Designing",
        "Sampling",
        "Sent",
        "Feedback",
        "Approved",
        "Rejected",
        "Cancelled",
    ),
    "GBOS Sample Iteration": ("Draft", "Active", "Completed", "Cancelled"),
    "GBOS Sample Shipment": ("Draft", "In Transit", "Delivered", "Cancelled"),
    "GBOS Sample Feedback": ("Draft", "Received", "Archived"),
    "GBOS Demand Signal": ("Draft", "Confirmed", "Sourcing", "Fulfilled", "Cancelled"),
    "GBOS Sourcing Event": (
        "Draft",
        "Invited",
        "Collecting",
        "Evaluating",
        "Selected",
        "Closed",
        "Cancelled",
    ),
    "GBOS Work Item": ("Open", "In Progress", "Blocked", "Done", "Cancelled"),
    "GBOS Review Case": ("Pending", "Approved", "Rejected", "Superseded"),
}
CRM_STATUS_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "CRM Lead": (
        "New",
        "Contacted",
        "Qualified",
        "Converted",
        "Nurture",
        "Unqualified",
        "Junk",
    ),
    "CRM Deal": (
        "Qualification",
        "Proposal/Quotation",
        "Negotiation",
        "Ready to Close",
        "Won",
        "Lost",
    ),
}

_FILE_BY_DOCTYPE: dict[str, str] = {
    "GBOS Team": "teams.json",
    "GBOS Party Profile": "party_profiles.json",
    "CRM Organization": "crm_organizations.json",
    "Contact": "contacts.json",
    "CRM Lead": "crm_leads.json",
    "CRM Deal": "crm_deals.json",
    "GBOS Product Brief": "product_briefs.json",
    "GBOS Sample Project": "sample_projects.json",
    "GBOS Sample Iteration": "sample_iterations.json",
    "GBOS Sample Shipment": "sample_shipments.json",
    "GBOS Sample Feedback": "sample_feedback.json",
    "GBOS Demand Signal": "demand_signals.json",
    "GBOS Sourcing Event": "sourcing_events.json",
    "GBOS Work Item": "work_items.json",
    "GBOS Review Case": "review_cases.json",
    "User": "users.json",
}

# The allowlist mirrors the committed DocType field names.  It is used by the
# deterministic loader view to prove that envelope metadata never reaches a
# Frappe insert payload.
FRAPPE_FIELDS: dict[str, frozenset[str]] = {
    "GBOS Team": frozenset(
        {
            "team_name",
            "members",
            "origin",
            "origin_reference",
            "business_status",
            "review_status",
            "revision",
            "last_request_id",
        }
    ),
    "GBOS Party Profile": frozenset(
        {
            "party_name",
            "team",
            "crm_organization",
            "contact",
            "crm_lead",
            "crm_deal",
            "origin",
            "origin_reference",
            "business_status",
            "review_status",
            "revision",
            "last_request_id",
        }
    ),
    "GBOS Product Brief": frozenset(
        {
            "title",
            "team",
            "party_profile",
            "deal",
            "description",
            "target_quantity",
            "target_uom",
            "target_date",
            "origin",
            "origin_reference",
            "business_status",
            "review_status",
            "revision",
            "last_request_id",
        }
    ),
    "GBOS Sample Project": frozenset(
        {
            "title",
            "team",
            "party_profile",
            "product_brief",
            "owner_user",
            "origin",
            "origin_reference",
            "business_status",
            "review_status",
            "revision",
            "last_request_id",
        }
    ),
    "GBOS Sample Iteration": frozenset(
        {
            "team",
            "sample_project",
            "iteration_number",
            "summary",
            "started_on",
            "completed_on",
            "origin",
            "origin_reference",
            "business_status",
            "review_status",
            "revision",
            "last_request_id",
        }
    ),
    "GBOS Sample Shipment": frozenset(
        {
            "team",
            "sample_project",
            "sample_iteration",
            "carrier",
            "tracking_number",
            "shipped_on",
            "delivered_on",
            "origin",
            "origin_reference",
            "business_status",
            "review_status",
            "revision",
            "last_request_id",
        }
    ),
    "GBOS Sample Feedback": frozenset(
        {
            "team",
            "sample_project",
            "sample_iteration",
            "summary",
            "rating",
            "received_on",
            "received_from_contact",
            "origin",
            "origin_reference",
            "business_status",
            "review_status",
            "revision",
            "last_request_id",
        }
    ),
    "GBOS Demand Signal": frozenset(
        {
            "title",
            "team",
            "party_profile",
            "product_brief",
            "quantity",
            "uom",
            "needed_by",
            "origin",
            "origin_reference",
            "business_status",
            "review_status",
            "revision",
            "last_request_id",
        }
    ),
    "GBOS Sourcing Event": frozenset(
        {
            "title",
            "team",
            "demand_signal",
            "candidates",
            "selected_supplier",
            "owner_user",
            "origin",
            "origin_reference",
            "business_status",
            "review_status",
            "revision",
            "last_request_id",
        }
    ),
    "GBOS Work Item": frozenset(
        {
            "title",
            "team",
            "assigned_to",
            "priority",
            "due_date",
            "reference_doctype",
            "reference_name",
            "blocked_reason",
            "origin",
            "origin_reference",
            "business_status",
            "review_status",
            "revision",
            "last_request_id",
        }
    ),
    "GBOS Review Case": frozenset(
        {
            "title",
            "team",
            "assigned_reviewer",
            "subject_doctype",
            "subject_name",
            "decision_note",
            "decided_at",
            "origin",
            "origin_reference",
            "business_status",
            "review_status",
            "revision",
            "last_request_id",
        }
    ),
    "GBOS Sourcing Candidate": frozenset(
        {
            "supplier_name",
            "external_supplier_id",
            "quoted_price",
            "currency",
            "lead_time_days",
            "candidate_status",
            "notes",
        }
    ),
    "CRM Organization": frozenset(
        {"organization_name", "website", "custom_esan_team", "custom_esan_origin"}
    ),
    "Contact": frozenset(
        {"first_name", "last_name", "email_id", "mobile_no", "organization", "custom_esan_team"}
    ),
    "CRM Lead": frozenset(
        {
            "first_name",
            "last_name",
            "lead_name",
            "organization",
            "email",
            "mobile_no",
            "status",
            "lost_reason",
            "custom_esan_team",
        }
    ),
    "CRM Deal": frozenset(
        {
            "organization",
            "contact",
            "lead",
            "status",
            "lost_reason",
            "expected_deal_value",
            "custom_esan_team",
            "custom_esan_party_profile",
        }
    ),
    "User": frozenset(
        {
            "email",
            "first_name",
            "enabled",
            "user_type",
            "roles",
            "send_welcome_email",
        }
    ),
}


def _id(prefix: str, index: int) -> str:
    return f"{prefix}-{index + 1:04d}"


def _encode_crockford(value: int, length: int) -> str:
    result = ["0"] * length
    for position in range(length - 1, -1, -1):
        value, remainder = divmod(value, 32)
        result[position] = _CROCKFORD[remainder]
    return "".join(result)


def _gbos_id(doctype: str, index: int) -> str:
    timestamp_ms = int(datetime.fromisoformat(DEMO_TIME.replace("Z", "+00:00")).timestamp() * 1000)
    entropy = int.from_bytes(
        hashlib.sha256(f"{SEED}:{doctype}:{index}".encode()).digest()[:10],
        "big",
    )
    return f"{GBOS_PREFIXES[doctype]}-{_encode_crockford((timestamp_ms << 80) | entropy, 26)}"


def _cycle(values: tuple[str, ...], index: int) -> str:
    return values[index % len(values)]


def _gbos_fields(
    doctype: str,
    business_status: str,
    *,
    team: str,
    review_status: str,
    origin: str = "Fixture",
    origin_reference: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    if doctype not in BUSINESS_STATUS_ALLOWLIST:
        raise ValueError(f"unknown GBOS doctype: {doctype}")
    if business_status not in BUSINESS_STATUS_ALLOWLIST[doctype]:
        raise ValueError(f"unsupported business status {business_status!r} for {doctype}")
    if review_status not in REVIEW_STATUSES:
        raise ValueError(f"unsupported review status {review_status!r}")
    values: dict[str, Any] = {
        "origin": origin,
        "origin_reference": origin_reference or f"fixture://gate1/{doctype}",
        "business_status": business_status,
        "review_status": review_status,
        "revision": 1,
        "last_request_id": "req-gate1-synthetic-0001",
    }
    if doctype != "GBOS Team":
        values["team"] = team
    values.update(fields)
    unknown = set(values) - FRAPPE_FIELDS[doctype]
    if unknown:
        raise ValueError(f"unknown {doctype} fields: {sorted(unknown)}")
    return values


def _record(doctype: str, name: str, fields: Mapping[str, Any]) -> dict[str, Any]:
    """Wrap insertable fields with deterministic fixture-only metadata."""

    unknown = set(fields) - FRAPPE_FIELDS[doctype]
    if unknown:
        raise ValueError(f"unknown {doctype} fields: {sorted(unknown)}")
    return {
        "doctype": doctype,
        "name": name,
        "synthetic": True,
        "demo": True,
        "fixture_source": FIXTURE_SOURCE,
        "seed": SEED,
        "site_id": SITE_ID,
        "generated_at": DEMO_TIME,
        "fields": dict(fields),
    }


def _child_record(name: str, fields: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(fields) - FRAPPE_FIELDS["GBOS Sourcing Candidate"]
    if unknown:
        raise ValueError(f"unknown child fields: {sorted(unknown)}")
    return {
        "doctype": "GBOS Sourcing Candidate",
        "name": name,
        "synthetic": True,
        "demo": True,
        "fixture_source": FIXTURE_SOURCE,
        "seed": SEED,
        "fields": dict(fields),
    }


def _make_records(seed: int) -> tuple[OrderedDict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    records: OrderedDict[str, list[dict[str, Any]]] = OrderedDict(
        (doctype, []) for doctype in TOP_LEVEL_DOCTYPES
    )
    team_ids = [_gbos_id("GBOS Team", index) for index in range(TEAM_COUNT)]
    party_ids = [_gbos_id("GBOS Party Profile", index) for index in range(PARTY_COUNT)]
    organization_ids = [_id("CRM-ORG", index) for index in range(ORGANIZATION_COUNT)]
    contact_ids = [_id("CRM-CONTACT", index) for index in range(PARTY_COUNT)]
    lead_ids = [_id("CRM-LEAD", index) for index in range(PARTY_COUNT)]
    deal_ids = [_id("CRM-DEAL", index) for index in range(PARTY_COUNT)]
    brief_ids = [_gbos_id("GBOS Product Brief", index) for index in range(CHAIN_COUNT)]
    project_ids = [_gbos_id("GBOS Sample Project", index) for index in range(CHAIN_COUNT)]
    iteration_ids = [_gbos_id("GBOS Sample Iteration", index) for index in range(CHAIN_COUNT)]
    shipment_ids = [_gbos_id("GBOS Sample Shipment", index) for index in range(CHAIN_COUNT)]
    feedback_ids = [_gbos_id("GBOS Sample Feedback", index) for index in range(CHAIN_COUNT)]
    demand_ids = [_gbos_id("GBOS Demand Signal", index) for index in range(CHAIN_COUNT)]
    sourcing_ids = [_gbos_id("GBOS Sourcing Event", index) for index in range(CHAIN_COUNT)]
    work_ids = [
        _gbos_id("GBOS Work Item", index) for index in range(CHAIN_COUNT + EXTRA_WORK_ITEM_COUNT)
    ]
    review_ids = [
        _gbos_id("GBOS Review Case", index) for index in range(CHAIN_COUNT + EXTRA_WORK_ITEM_COUNT)
    ]
    reviewer_ids = [email for email, _, roles, _ in DEMO_USERS if "Reviewer" in roles]
    child_records: list[dict[str, Any]] = []

    for user_id, first_name, roles, _ in DEMO_USERS:
        records["User"].append(
            _record(
                "User",
                user_id,
                {
                    "email": user_id,
                    "first_name": first_name,
                    "enabled": 1,
                    "user_type": "System User",
                    "roles": [{"role": role} for role in roles],
                    "send_welcome_email": 0,
                },
            )
        )
    for index, team_id in enumerate(team_ids):
        members = [
            {
                "user": email,
                "team_role": roles[0],
                "enabled": 1,
            }
            for email, _, roles, team_indexes in DEMO_USERS
            if index in team_indexes
        ]
        records["GBOS Team"].append(
            _record(
                "GBOS Team",
                team_id,
                _gbos_fields(
                    "GBOS Team",
                    "Active",
                    team=team_id,
                    review_status="Approved",
                    team_name=f"Synthetic Team {index + 1:04d}",
                    members=members,
                ),
            )
        )

    for index, party_id in enumerate(party_ids):
        records["GBOS Party Profile"].append(
            _record(
                "GBOS Party Profile",
                party_id,
                _gbos_fields(
                    "GBOS Party Profile",
                    "Active"
                    if index < CHAIN_COUNT
                    else _cycle(BUSINESS_STATUS_ALLOWLIST["GBOS Party Profile"], index),
                    team=team_ids[index % TEAM_COUNT],
                    review_status=_cycle(REVIEW_STATUSES, index),
                    party_name=f"Synthetic Party {index + 1:04d}",
                    crm_organization=organization_ids[index],
                    contact=contact_ids[index],
                    crm_lead=lead_ids[index],
                    crm_deal=deal_ids[index],
                ),
            )
        )
    for index, organization_id in enumerate(organization_ids):
        records["CRM Organization"].append(
            _record(
                "CRM Organization",
                organization_id,
                {
                    "organization_name": f"Synthetic Organization {index + 1:04d}",
                    "website": f"https://org-{index + 1:04d}.example.invalid",
                    "custom_esan_team": team_ids[index % TEAM_COUNT],
                    "custom_esan_origin": "Fixture",
                },
            )
        )
    for index, contact_id in enumerate(contact_ids):
        records["Contact"].append(
            _record(
                "Contact",
                contact_id,
                {
                    "first_name": f"Synthetic Contact {index + 1:04d}",
                    "last_name": "Fixture",
                    "email_id": contact_id.lower() + "@example.invalid",
                    "mobile_no": f"+000-{index // 10000:04d}-{index + 1:04d}",
                    "custom_esan_team": team_ids[index % TEAM_COUNT],
                },
            )
        )
    for index, lead_id in enumerate(lead_ids):
        lead_status = (
            "Qualified" if index < CHAIN_COUNT else _cycle(CRM_STATUS_ALLOWLIST["CRM Lead"], index)
        )
        lead_fields = {
            "first_name": f"Synthetic Lead {index + 1:04d}",
            "last_name": "Fixture",
            "lead_name": f"Synthetic Lead {index + 1:04d}",
            "organization": organization_ids[index],
            "email": lead_id.lower() + "@example.invalid",
            "mobile_no": f"+000-{index // 10000:04d}-{index + 1:04d}",
            "status": lead_status,
            "custom_esan_team": team_ids[index % TEAM_COUNT],
        }
        if lead_status in {"Unqualified", "Junk"}:
            lead_fields["lost_reason"] = "Poor Fit"
        records["CRM Lead"].append(
            _record(
                "CRM Lead",
                lead_id,
                lead_fields,
            )
        )
    for index, deal_id in enumerate(deal_ids):
        deal_status = (
            "Won" if index < CHAIN_COUNT else _cycle(CRM_STATUS_ALLOWLIST["CRM Deal"], index)
        )
        deal_fields: dict[str, Any] = {
            "organization": organization_ids[index],
            "contact": contact_ids[index],
            "lead": lead_ids[index],
            "status": deal_status,
            "expected_deal_value": 10000 + index * 10,
            "custom_esan_team": team_ids[index % TEAM_COUNT],
            "custom_esan_party_profile": party_ids[index],
        }
        if deal_status == "Lost":
            deal_fields["lost_reason"] = "Competition"
        records["CRM Deal"].append(
            _record(
                "CRM Deal",
                deal_id,
                deal_fields,
            )
        )

    for index in range(CHAIN_COUNT):
        team = team_ids[index % TEAM_COUNT]
        party = party_ids[index]
        deal = deal_ids[index]
        brief = brief_ids[index]
        project = project_ids[index]
        iteration = iteration_ids[index]
        shipment = shipment_ids[index]
        feedback = feedback_ids[index]
        demand = demand_ids[index]
        sourcing = sourcing_ids[index]
        work = work_ids[index]
        review = review_ids[index]
        reviewer = reviewer_ids[index % len(reviewer_ids)]
        sourcing_status = "Selected" if index % 4 == 0 else "Evaluating"
        candidate_rows: list[dict[str, Any]] = []
        for offset in range(3):
            candidate_id = _id("GBOS-CANDIDATE", index * 3 + offset)
            candidate = _child_record(
                candidate_id,
                {
                    "supplier_name": f"Synthetic Supplier {index + 1:04d}-{offset + 1}",
                    "external_supplier_id": f"KD-SYNTH-SUPPLIER-{index * 3 + offset + 1:04d}",
                    "quoted_price": 10.0 + offset,
                    "currency": "CNY",
                    "lead_time_days": 12 + offset,
                    "candidate_status": (
                        "Selected"
                        if sourcing_status == "Selected" and offset == 0
                        else "Shortlisted"
                    ),
                    "notes": "Synthetic candidate row; no supplier relationship is implied",
                },
            )
            child_records.append(candidate)
            candidate_rows.append(candidate)
        records["GBOS Product Brief"].append(
            _record(
                "GBOS Product Brief",
                brief,
                _gbos_fields(
                    "GBOS Product Brief",
                    "Active",
                    team=team,
                    review_status=_cycle(REVIEW_STATUSES, index),
                    title=f"Synthetic Product Brief {index + 1:04d}",
                    party_profile=party,
                    deal=deal,
                    description="Gate 1 synthetic product requirement",
                    target_quantity=100 + index,
                    target_date=f"2026-09-{index % 28 + 1:02d}",
                ),
            )
        )
        records["GBOS Sample Project"].append(
            _record(
                "GBOS Sample Project",
                project,
                _gbos_fields(
                    "GBOS Sample Project",
                    _cycle(BUSINESS_STATUS_ALLOWLIST["GBOS Sample Project"], index),
                    team=team,
                    review_status=_cycle(REVIEW_STATUSES, index + 1),
                    title=f"Synthetic Sample Project {index + 1:04d}",
                    party_profile=party,
                    product_brief=brief,
                    owner_user=reviewer,
                ),
            )
        )
        records["GBOS Sample Iteration"].append(
            _record(
                "GBOS Sample Iteration",
                iteration,
                _gbos_fields(
                    "GBOS Sample Iteration",
                    "Completed" if index % 3 == 0 else "Active",
                    team=team,
                    review_status=_cycle(REVIEW_STATUSES, index + 2),
                    sample_project=project,
                    iteration_number=1,
                    summary=f"Synthetic iteration summary {index + 1:04d}",
                    started_on="2026-08-01",
                    completed_on="2026-08-05" if index % 3 == 0 else None,
                ),
            )
        )
        records["GBOS Sample Shipment"].append(
            _record(
                "GBOS Sample Shipment",
                shipment,
                _gbos_fields(
                    "GBOS Sample Shipment",
                    "Delivered" if index % 4 == 0 else "In Transit",
                    team=team,
                    review_status=_cycle(REVIEW_STATUSES, index + 3),
                    sample_project=project,
                    sample_iteration=iteration,
                    carrier="Synthetic Carrier",
                    tracking_number=f"SYNTH-TRACK-{index + 1:04d}",
                    shipped_on="2026-08-02",
                    delivered_on="2026-08-05" if index % 4 == 0 else None,
                ),
            )
        )
        records["GBOS Sample Feedback"].append(
            _record(
                "GBOS Sample Feedback",
                feedback,
                _gbos_fields(
                    "GBOS Sample Feedback",
                    "Received" if index % 4 else "Archived",
                    team=team,
                    review_status=_cycle(REVIEW_STATUSES, index + 4),
                    sample_project=project,
                    sample_iteration=iteration,
                    summary=f"Synthetic feedback summary {index + 1:04d}",
                    rating=4 if index % 4 else 5,
                    received_on="2026-08-06",
                    received_from_contact=contact_ids[index],
                ),
            )
        )
        records["GBOS Demand Signal"].append(
            _record(
                "GBOS Demand Signal",
                demand,
                _gbos_fields(
                    "GBOS Demand Signal",
                    "Sourcing" if index % 4 == 0 else "Confirmed",
                    team=team,
                    review_status=_cycle(REVIEW_STATUSES, index + 5),
                    origin_reference=f"fixture://gate1/GBOS Sample Feedback/{feedback}",
                    title=f"Synthetic Demand Signal {index + 1:04d}",
                    party_profile=party,
                    product_brief=brief,
                    quantity=100 + index,
                    needed_by=f"2026-09-{index % 28 + 1:02d}",
                ),
            )
        )
        records["GBOS Sourcing Event"].append(
            _record(
                "GBOS Sourcing Event",
                sourcing,
                _gbos_fields(
                    "GBOS Sourcing Event",
                    sourcing_status,
                    team=team,
                    review_status=_cycle(REVIEW_STATUSES, index + 6),
                    title=f"Synthetic Sourcing Event {index + 1:04d}",
                    demand_signal=demand,
                    candidates=candidate_rows,
                    selected_supplier=candidate_rows[0]["fields"]["supplier_name"]
                    if sourcing_status == "Selected"
                    else None,
                    owner_user=reviewer,
                ),
            )
        )
        records["GBOS Work Item"].append(
            _record(
                "GBOS Work Item",
                work,
                _gbos_fields(
                    "GBOS Work Item",
                    "Done" if index % 4 == 0 else "In Progress",
                    team=team,
                    review_status="Pending" if index % 9 == 0 else "Approved",
                    title=f"Synthetic sourcing work item {index + 1:04d}",
                    assigned_to=reviewer,
                    priority="High" if index % 3 == 0 else "Medium",
                    due_date=f"2026-09-{index % 28 + 1:02d}",
                    reference_doctype="GBOS Sourcing Event",
                    reference_name=sourcing,
                ),
            )
        )
        records["GBOS Review Case"].append(
            _record(
                "GBOS Review Case",
                review,
                _gbos_fields(
                    "GBOS Review Case",
                    "Approved" if index % 4 == 0 else "Pending",
                    team=team,
                    review_status="Approved" if index % 4 == 0 else "Pending",
                    title=f"Synthetic review case {index + 1:04d}",
                    assigned_reviewer=reviewer,
                    subject_doctype="GBOS Work Item",
                    subject_name=work,
                    decision_note="Synthetic human-review outcome",
                    decided_at=DEMO_TIME if index % 4 == 0 else None,
                ),
            )
        )

    for index in range(EXTRA_WORK_ITEM_COUNT):
        work_index = CHAIN_COUNT + index
        work = work_ids[work_index]
        review = review_ids[work_index]
        party = party_ids[work_index]
        team = team_ids[work_index % TEAM_COUNT]
        reviewer = reviewer_ids[work_index % len(reviewer_ids)]
        records["GBOS Work Item"].append(
            _record(
                "GBOS Work Item",
                work,
                _gbos_fields(
                    "GBOS Work Item",
                    "Open" if index % 2 == 0 else "Blocked",
                    team=team,
                    review_status="AI Draft",
                    title=f"Synthetic follow-up work item {work_index + 1:04d}",
                    assigned_to=reviewer,
                    priority="Low",
                    due_date=f"2026-10-{index + 1:02d}",
                    reference_doctype="GBOS Party Profile",
                    reference_name=party,
                ),
            )
        )
        records["GBOS Review Case"].append(
            _record(
                "GBOS Review Case",
                review,
                _gbos_fields(
                    "GBOS Review Case",
                    "Pending",
                    team=team,
                    review_status="AI Draft",
                    title=f"Synthetic follow-up review case {work_index + 1:04d}",
                    assigned_reviewer=reviewer,
                    subject_doctype="GBOS Work Item",
                    subject_name=work,
                ),
            )
        )

    # The seed is part of the public API, but does not alter the stable demo
    # timestamp or identifiers.  Keeping it in this branch makes accidental
    # non-integer seed values fail early while preserving deterministic output.
    if seed != SEED:
        for record in [item for rows in records.values() for item in rows] + child_records:
            record["seed"] = seed
    return records, child_records


def to_frappe_payload(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Strip fixture metadata and flatten fields for a Frappe insert batch."""

    payload: list[dict[str, Any]] = []
    for record in records:
        doctype = record["doctype"]
        fields = dict(record["fields"])
        if doctype == "GBOS Sourcing Event":
            children = []
            for child in fields.get("candidates", []):
                child_payload = {"doctype": child["doctype"], "name": child["name"]}
                child_payload.update(child["fields"])
                children.append(child_payload)
            fields["candidates"] = children
        item = {"doctype": doctype, "name": record["name"]}
        item.update(fields)
        unknown = set(item) - {"doctype", "name"} - FRAPPE_FIELDS[doctype]
        if unknown:
            raise ValueError(f"unknown Frappe payload fields for {doctype}: {sorted(unknown)}")
        payload.append(item)
    return payload


def build_dataset(seed: int = SEED) -> dict[str, Any]:
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise TypeError("seed must be an integer")
    grouped, children = _make_records(seed)
    flattened = [record for rows in grouped.values() for record in rows]
    child_counts = {doctype: 0 for doctype in CHILD_DOCTYPES}
    child_counts["GBOS Sourcing Candidate"] = len(children)
    frappe_payload = to_frappe_payload(flattened)
    manifest = {
        "schema_version": "1.0",
        "dataset": "gate1",
        "seed": seed,
        "generated_at": DEMO_TIME,
        "site_id": SITE_ID,
        "synthetic": True,
        "demo": True,
        "fixture_source": FIXTURE_SOURCE,
        "record_counts": {doctype: len(rows) for doctype, rows in grouped.items()},
        "child_record_counts": child_counts,
        "files": {
            "records": "records.json",
            "frappe_payload": "frappe_payload.json",
            "status_allowlist": "status_allowlist.json",
            "children": {"GBOS Sourcing Candidate": "sourcing_candidates.json"},
            **_FILE_BY_DOCTYPE,
        },
        "frappe_payload_sha256": hashlib.sha256(_json_bytes(frappe_payload)).hexdigest(),
    }
    return {
        "manifest": manifest,
        "status_allowlist": {
            "business_status": {
                doctype: list(statuses) for doctype, statuses in BUSINESS_STATUS_ALLOWLIST.items()
            },
            "review_status": list(REVIEW_STATUSES),
            "crm_status": {
                doctype: list(statuses) for doctype, statuses in CRM_STATUS_ALLOWLIST.items()
            },
            "origin": ["Manual", "Fixture", "Integration", "AI"],
        },
        "records": flattened,
        "children": children,
        "grouped": dict(grouped),
        "frappe_payload": frappe_payload,
    }


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _dump_json(path: Path, value: Any) -> None:
    path.write_bytes(_json_bytes(value))


def write_fixtures(output_dir: str | Path | None = None, *, seed: int = SEED) -> tuple[Path, ...]:
    """Write deterministic JSON assets and return paths relative to output_dir."""

    target = Path(output_dir) if output_dir is not None else Path(__file__).resolve().parent
    target.mkdir(parents=True, exist_ok=True)
    dataset = build_dataset(seed)
    grouped = dataset["grouped"]
    outputs: list[Path] = []
    for filename, value in (
        ("manifest.json", dataset["manifest"]),
        ("status_allowlist.json", dataset["status_allowlist"]),
        ("records.json", dataset["records"]),
        ("frappe_payload.json", dataset["frappe_payload"]),
        ("sourcing_candidates.json", dataset["children"]),
    ):
        _dump_json(target / filename, value)
        outputs.append(Path(filename))
    for doctype in TOP_LEVEL_DOCTYPES:
        filename = _FILE_BY_DOCTYPE[doctype]
        _dump_json(target / filename, grouped[doctype])
        outputs.append(Path(filename))
    return tuple(outputs)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="directory for deterministic JSON assets (default: this directory)",
    )
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args(list(argv) if argv is not None else None)
    paths = write_fixtures(args.output_dir, seed=args.seed)
    print(f"wrote {len(paths)} Gate 1 fixture files to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
