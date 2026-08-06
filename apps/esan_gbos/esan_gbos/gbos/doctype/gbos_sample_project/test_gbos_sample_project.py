from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from esan_gbos.api.v1.audit import run_idempotent
from esan_gbos.api.v1.common import BFFError
from esan_gbos.api.v1.party import get_360
from esan_gbos.api.v1.sample import create_project
from esan_gbos.api.v1.sourcing import create_from_demand, get_board
from esan_gbos.api.v1.work_item import list as list_work_items
from esan_gbos.api.v1.work_item import transition
from esan_gbos.permissions import has_gbos_permission

TEST_USERS = (
    "gbos-member@example.invalid",
    "gbos-outsider@example.invalid",
    "gbos-reviewer-a@example.invalid",
    "gbos-reviewer-b@example.invalid",
    "gbos-auditor@example.invalid",
    "gbos-buyer@example.invalid",
    "gbos-purchase-manager@example.invalid",
    "gbos-product@example.invalid",
)

# This suite creates every dependency explicitly.  Letting Frappe synthesize
# linked User records recursively pulls ERPNext Company/Item fixtures, which
# correctly collide with the GBOS V1 transaction guard.
IGNORE_TEST_RECORD_DEPENDENCIES = [
    "GBOS Team",
    "GBOS Party Profile",
    "GBOS Product Brief",
    "User",
]


class TestGBOSGateOneDomain(IntegrationTestCase):
    def setUp(self) -> None:
        frappe.set_user("Administrator")
        self.member = self._user(TEST_USERS[0], "Sales User")
        self.outsider = self._user(TEST_USERS[1], "Sales User")
        self.reviewer = self._user(TEST_USERS[2], "Reviewer")
        self.other_reviewer = self._user(TEST_USERS[3], "Reviewer")
        self.auditor = self._user(TEST_USERS[4], "Privacy/Audit")
        self.buyer = self._user(TEST_USERS[5], "Buyer")
        self.purchase_manager = self._user(TEST_USERS[6], "Purchase Manager")
        self.product = self._user(TEST_USERS[7], "Product/R&D")
        self.team = frappe.get_doc(
            {
                "doctype": "GBOS Team",
                "team_name": "Gate 1 Test Team",
                "members": [
                    {"user": self.member, "team_role": "Owner", "enabled": 1},
                    {"user": self.buyer, "team_role": "Member", "enabled": 1},
                    {
                        "user": self.purchase_manager,
                        "team_role": "Manager",
                        "enabled": 1,
                    },
                    {"user": self.product, "team_role": "Member", "enabled": 1},
                ],
            }
        ).insert(ignore_permissions=True)

    def tearDown(self) -> None:
        frappe.set_user("Administrator")

    def _user(self, email: str, *roles: str) -> str:
        if not frappe.db.exists("User", email):
            frappe.get_doc(
                {
                    "doctype": "User",
                    "email": email,
                    "first_name": email.split("@", 1)[0],
                    "send_welcome_email": 0,
                }
            ).insert(ignore_permissions=True)
        user = frappe.get_doc("User", email)
        for role in roles:
            if role not in frappe.get_roles(email):
                user.add_roles(role)
        return email

    def _work_item(self) -> object:
        return frappe.get_doc(
            {
                "doctype": "GBOS Work Item",
                "title": "Gate 1 integration work",
                "team": self.team.name,
                "business_status": "Open",
                "review_status": "Pending",
            }
        ).insert(ignore_permissions=True)

    def test_illegal_transition_is_rejected_server_side(self) -> None:
        work = self._work_item()
        work.business_status = "Done"

        with self.assertRaises(frappe.ValidationError):
            work.save(ignore_permissions=True)

    def test_non_initial_workflow_status_is_rejected_on_insert(self) -> None:
        with self.assertRaises(frappe.ValidationError):
            frappe.get_doc(
                {
                    "doctype": "GBOS Sample Project",
                    "title": "Invalid direct approval",
                    "team": self.team.name,
                    "origin": "Manual",
                    "business_status": "Approved",
                    "review_status": "Pending",
                }
            ).insert(ignore_permissions=True)

    def test_ai_origin_cannot_insert_an_approved_record(self) -> None:
        with self.assertRaises(frappe.ValidationError):
            frappe.get_doc(
                {
                    "doctype": "GBOS Product Brief",
                    "title": "Invalid AI approval",
                    "team": self.team.name,
                    "origin": "AI",
                    "business_status": "Draft",
                    "review_status": "Approved",
                }
            ).insert(ignore_permissions=True)

    def test_stale_revision_is_rejected_server_side(self) -> None:
        work = self._work_item()
        stale = frappe.get_doc("GBOS Work Item", work.name)
        current = frappe.get_doc("GBOS Work Item", work.name)
        current.business_status = "In Progress"
        current.save(ignore_permissions=True)
        stale.priority = "High"

        with self.assertRaises(frappe.ValidationError):
            stale.save(ignore_permissions=True)

    def test_composite_external_keys_are_unique(self) -> None:
        values = {
            "doctype": "GBOS External Crosswalk",
            "team": self.team.name,
            "external_system": "fixture-system",
            "account_set": "fixture-account",
            "object_type": "Material",
            "external_id": f"fixture-id-{self.team.name}",
            "target_doctype": "GBOS Team",
            "target_name": self.team.name,
        }
        frappe.get_doc(values).insert(ignore_permissions=True)

        with self.assertRaises(frappe.UniqueValidationError):
            frappe.get_doc(values).insert(ignore_permissions=True)

    def test_list_and_document_permissions_use_the_same_team_scope(self) -> None:
        work = self._work_item()
        self.assertTrue(has_gbos_permission(work, self.member, "read"))
        self.assertFalse(has_gbos_permission(work, self.outsider, "read"))

        frappe.set_user(self.member)
        visible = frappe.get_list(
            "GBOS Work Item",
            filters={"name": work.name},
            pluck="name",
        )
        self.assertEqual(visible, [work.name])

        frappe.set_user(self.outsider)
        hidden = frappe.get_list(
            "GBOS Work Item",
            filters={"name": work.name},
            pluck="name",
        )
        self.assertEqual(hidden, [])

    def test_work_item_cursor_does_not_drop_equal_modified_rows(self) -> None:
        names = []
        for index in range(60):
            work = frappe.get_doc(
                {
                    "doctype": "GBOS Work Item",
                    "title": f"Equal timestamp work {index:02d}",
                    "team": self.team.name,
                    "business_status": "Open",
                    "review_status": "Pending",
                }
            ).insert(ignore_permissions=True)
            names.append(work.name)
        for name in names:
            frappe.db.set_value(
                "GBOS Work Item",
                name,
                "modified",
                "2026-08-06 12:00:00.000001",
                update_modified=False,
            )

        frappe.set_user(self.member)
        seen: list[str] = []
        cursor = None
        while True:
            page = list_work_items(
                filters={"team": self.team.name},
                cursor=cursor,
                page_size=10,
            )
            seen.extend(row["name"] for row in page["data"])
            cursor = page["meta"]["next_cursor"]
            if not cursor:
                break

        self.assertEqual(len(seen), 60)
        self.assertEqual(set(seen), set(names))

    def test_crm_records_use_the_same_team_scope(self) -> None:
        organization = frappe.get_doc(
            {
                "doctype": "CRM Organization",
                "organization_name": "Gate 1 scoped organization",
                "custom_esan_team": self.team.name,
                "custom_esan_origin": "Fixture",
            }
        ).insert(ignore_permissions=True)
        contact = frappe.get_doc(
            {
                "doctype": "Contact",
                "first_name": "Gate 1 scoped",
                "last_name": "contact",
                "custom_esan_team": self.team.name,
            }
        ).insert(ignore_permissions=True)

        frappe.set_user(self.member)
        visible = frappe.get_list(
            "CRM Organization",
            filters={"name": organization.name},
            pluck="name",
        )
        self.assertEqual(visible, [organization.name])

        frappe.set_user(self.outsider)
        hidden = frappe.get_list(
            "CRM Organization",
            filters={"name": organization.name},
            pluck="name",
        )
        self.assertEqual(hidden, [])

        frappe.set_user(self.product)
        self.assertEqual(
            frappe.get_list(
                "CRM Organization",
                filters={"name": organization.name},
                pluck="name",
            ),
            [organization.name],
        )
        with self.assertRaises(frappe.PermissionError):
            frappe.get_list(
                "Contact",
                filters={"name": contact.name},
                pluck="name",
            )

    def test_party_360_omits_every_linked_crm_record_from_another_team(self) -> None:
        other_team = frappe.get_doc(
            {
                "doctype": "GBOS Team",
                "team_name": "Gate 1 other Party 360 team",
            }
        ).insert(ignore_permissions=True)
        organization = frappe.get_doc(
            {
                "doctype": "CRM Organization",
                "organization_name": "Gate 1 hidden Party 360 organization",
                "custom_esan_team": other_team.name,
            }
        ).insert(ignore_permissions=True)
        contact = frappe.get_doc(
            {
                "doctype": "Contact",
                "first_name": "Gate 1 hidden Party 360",
                "last_name": "contact",
                "custom_esan_team": other_team.name,
            }
        ).insert(ignore_permissions=True)
        lead = frappe.get_doc(
            {
                "doctype": "CRM Lead",
                "first_name": "Gate 1 hidden Party 360",
                "last_name": "lead",
                "lead_name": "Gate 1 hidden Party 360 lead",
                "organization": organization.name,
                "status": "Qualified",
                "custom_esan_team": other_team.name,
            }
        ).insert(ignore_permissions=True)
        deal = frappe.get_doc(
            {
                "doctype": "CRM Deal",
                "organization": organization.name,
                "contact": contact.name,
                "lead": lead.name,
                "status": "Won",
                "expected_deal_value": 987654,
                "custom_esan_team": other_team.name,
            }
        ).insert(ignore_permissions=True)
        party = frappe.get_doc(
            {
                "doctype": "GBOS Party Profile",
                "party_name": "Gate 1 cross-team Party 360 profile",
                "team": self.team.name,
                "business_status": "Active",
                "review_status": "Pending",
            }
        ).insert(ignore_permissions=True)
        frappe.db.set_value(
            "GBOS Party Profile",
            party.name,
            {
                "crm_organization": organization.name,
                "contact": contact.name,
                "crm_lead": lead.name,
                "crm_deal": deal.name,
            },
            update_modified=False,
        )

        frappe.set_user(self.member)
        data = get_360(party.name)["data"]
        frappe.set_user("Administrator")
        frappe.db.set_value(
            "GBOS Party Profile",
            party.name,
            "crm_organization",
            "CRM-ORG-MISSING-PARTY-360",
            update_modified=False,
        )
        frappe.set_user(self.member)
        missing_data = get_360(party.name)["data"]

        self.assertIsNone(data["organization"])
        self.assertIsNone(data["contact"])
        self.assertIsNone(data["lead"])
        self.assertIsNone(data["deal"])
        self.assertEqual(data["organization"], missing_data["organization"])

    def test_party_profile_rejects_cross_team_crm_links_on_write(self) -> None:
        other_team = frappe.get_doc(
            {
                "doctype": "GBOS Team",
                "team_name": "Gate 1 invalid profile link team",
            }
        ).insert(ignore_permissions=True)
        organization = frappe.get_doc(
            {
                "doctype": "CRM Organization",
                "organization_name": "Gate 1 invalid profile link organization",
                "custom_esan_team": other_team.name,
            }
        ).insert(ignore_permissions=True)

        with self.assertRaises(frappe.ValidationError):
            frappe.get_doc(
                {
                    "doctype": "GBOS Party Profile",
                    "party_name": "Gate 1 invalid cross-team profile",
                    "team": self.team.name,
                    "crm_organization": organization.name,
                    "business_status": "Active",
                    "review_status": "Pending",
                }
            ).insert(ignore_permissions=True)

    def test_party_360_omits_link_when_record_read_permission_is_denied(self) -> None:
        organization = frappe.get_doc(
            {
                "doctype": "CRM Organization",
                "organization_name": "Gate 1 permission checked Party 360 organization",
                "custom_esan_team": self.team.name,
            }
        ).insert(ignore_permissions=True)
        party = frappe.get_doc(
            {
                "doctype": "GBOS Party Profile",
                "party_name": "Gate 1 permission checked Party 360 profile",
                "team": self.team.name,
                "crm_organization": organization.name,
                "business_status": "Active",
                "review_status": "Pending",
            }
        ).insert(ignore_permissions=True)

        frappe.set_user(self.member)

        def deny_linked_organization(
            doctype: str,
            *_args: object,
            **_kwargs: object,
        ) -> bool:
            return doctype != "CRM Organization"

        with patch(
            "esan_gbos.api.v1.party.frappe.has_permission",
            side_effect=deny_linked_organization,
        ) as has_permission:
            data = get_360(party.name)["data"]

        self.assertIsNone(data["organization"])
        has_permission.assert_any_call(
            "CRM Organization",
            ptype="read",
            doc=organization.name,
        )

    def test_sales_and_buyer_doctype_access_is_partitioned(self) -> None:
        party = frappe.get_doc(
            {
                "doctype": "GBOS Party Profile",
                "party_name": "Gate 1 scoped party",
                "team": self.team.name,
                "business_status": "Active",
                "review_status": "Pending",
            }
        ).insert(ignore_permissions=True)
        sample = frappe.get_doc(
            {
                "doctype": "GBOS Sample Project",
                "title": "Gate 1 scoped sample",
                "team": self.team.name,
                "business_status": "Draft",
                "review_status": "Pending",
            }
        ).insert(ignore_permissions=True)
        demand = frappe.get_doc(
            {
                "doctype": "GBOS Demand Signal",
                "title": "Gate 1 authorized demand summary",
                "team": self.team.name,
                "business_status": "Draft",
                "review_status": "Pending",
            }
        ).insert(ignore_permissions=True)
        sourcing = frappe.get_doc(
            {
                "doctype": "GBOS Sourcing Event",
                "title": "Gate 1 scoped sourcing",
                "team": self.team.name,
                "demand_signal": demand.name,
                "business_status": "Draft",
                "review_status": "Pending",
            }
        ).insert(ignore_permissions=True)

        self.assertTrue(has_gbos_permission(sample, self.member, "read"))
        self.assertFalse(has_gbos_permission(sourcing, self.member, "read"))
        self.assertFalse(has_gbos_permission(party, self.buyer, "read"))
        self.assertFalse(has_gbos_permission(sample, self.buyer, "read"))
        self.assertTrue(has_gbos_permission(demand, self.buyer, "read"))
        self.assertFalse(has_gbos_permission(demand, self.buyer, "write"))
        self.assertTrue(has_gbos_permission(sourcing, self.buyer, "write"))

        frappe.set_user(self.member)
        self.assertEqual(
            frappe.get_list(
                "GBOS Sample Project",
                filters={"name": sample.name},
                pluck="name",
            ),
            [sample.name],
        )
        with self.assertRaises(frappe.PermissionError):
            frappe.get_list(
                "GBOS Sourcing Event",
                filters={"name": sourcing.name},
                pluck="name",
            )

        frappe.set_user(self.buyer)
        self.assertEqual(
            frappe.get_list(
                "GBOS Sourcing Event",
                filters={"name": sourcing.name},
                pluck="name",
            ),
            [sourcing.name],
        )
        with self.assertRaises(frappe.PermissionError):
            frappe.get_list(
                "GBOS Sample Project",
                filters={"name": sample.name},
                pluck="name",
            )

    def test_buyer_cannot_make_final_supplier_selection(self) -> None:
        demand = frappe.get_doc(
            {
                "doctype": "GBOS Demand Signal",
                "title": "Gate 1 supplier selection demand",
                "team": self.team.name,
                "business_status": "Draft",
                "review_status": "Pending",
            }
        ).insert(ignore_permissions=True)
        event = frappe.get_doc(
            {
                "doctype": "GBOS Sourcing Event",
                "title": "Gate 1 governed supplier selection",
                "team": self.team.name,
                "demand_signal": demand.name,
                "business_status": "Draft",
                "review_status": "Pending",
                "candidates": [
                    {
                        "supplier_name": "Synthetic Selection Candidate",
                        "candidate_status": "Shortlisted",
                    }
                ],
            }
        ).insert(ignore_permissions=True)

        frappe.set_user(self.purchase_manager)
        for status in ("Invited", "Collecting", "Evaluating"):
            event.business_status = status
            event.save()

        frappe.set_user(self.buyer)
        event.selected_supplier = "Synthetic Selection Candidate"
        event.candidates[0].candidate_status = "Selected"
        event.business_status = "Selected"
        with self.assertRaises(frappe.PermissionError):
            event.save()

        frappe.set_user(self.purchase_manager)
        event.reload()
        event.selected_supplier = "Synthetic Selection Candidate"
        event.candidates[0].candidate_status = "Selected"
        event.business_status = "Selected"
        event.save()
        self.assertEqual(event.business_status, "Selected")
        self.assertEqual(event.selected_supplier, "Synthetic Selection Candidate")

    def test_sourcing_board_includes_terminal_lanes_and_consistent_total(self) -> None:
        demand = frappe.get_doc(
            {
                "doctype": "GBOS Demand Signal",
                "title": "Gate 1 sourcing board demand",
                "team": self.team.name,
                "business_status": "Draft",
                "review_status": "Pending",
            }
        ).insert(ignore_permissions=True)
        statuses = (
            "Draft",
            "Invited",
            "Collecting",
            "Evaluating",
            "Selected",
            "Closed",
            "Cancelled",
        )
        for status in statuses:
            values = {
                "doctype": "GBOS Sourcing Event",
                "title": f"Gate 1 board {status}",
                "team": self.team.name,
                "demand_signal": demand.name,
                "origin": "Fixture",
                "business_status": status,
                "review_status": "Pending",
            }
            if status in {"Selected", "Closed"}:
                values["selected_supplier"] = f"Synthetic {status} supplier"
                values["candidates"] = [
                    {
                        "supplier_name": f"Synthetic {status} supplier",
                        "candidate_status": "Selected",
                    }
                ]
            elif status == "Draft":
                values["candidates"] = [
                    {
                        "supplier_name": "Synthetic Draft supplier",
                        "external_supplier_id": "SYNTHETIC-SUPPLIER-DRAFT",
                        "quoted_price": 12.5,
                        "currency": "USD",
                        "lead_time_days": 21,
                        "candidate_status": "Shortlisted",
                        "notes": "Synthetic candidate evidence",
                    }
                ]
            event = frappe.get_doc(values)
            event.flags.gbos_fixture_seed = True
            event.insert(ignore_permissions=True)

        frappe.set_user(self.buyer)
        board = get_board(team=self.team.name)["data"]

        self.assertEqual(board["total"], len(statuses))
        self.assertEqual(set(board["lanes"]), set(statuses))
        self.assertEqual(
            sum(len(records) for records in board["lanes"].values()),
            board["total"],
        )
        draft_event = next(
            row for row in board["lanes"]["Draft"] if row["title"] == "Gate 1 board Draft"
        )
        self.assertEqual(
            draft_event["candidates"],
            [
                {
                    "name": draft_event["candidates"][0]["name"],
                    "supplier_name": "Synthetic Draft supplier",
                    "external_supplier_id": "SYNTHETIC-SUPPLIER-DRAFT",
                    "quoted_price": 12.5,
                    "currency": "USD",
                    "lead_time_days": 21,
                    "candidate_status": "Shortlisted",
                    "notes": "Synthetic candidate evidence",
                }
            ],
        )

    def test_business_member_cannot_change_team_membership(self) -> None:
        frappe.set_user(self.member)
        team = frappe.get_doc("GBOS Team", self.team.name)
        team.team_name = "Unauthorized team rename"

        with self.assertRaises(frappe.PermissionError):
            team.save()

    def test_reviewer_sees_only_assigned_cases_and_read_only_subjects(self) -> None:
        assigned_work = self._work_item()
        hidden_work = self._work_item()
        assigned_case = frappe.get_doc(
            {
                "doctype": "GBOS Review Case",
                "title": "Assigned Gate 1 review",
                "team": self.team.name,
                "assigned_reviewer": self.reviewer,
                "subject_doctype": "GBOS Work Item",
                "subject_name": assigned_work.name,
                "business_status": "Pending",
                "review_status": "Pending",
            }
        ).insert(ignore_permissions=True)
        hidden_case = frappe.get_doc(
            {
                "doctype": "GBOS Review Case",
                "title": "Other Gate 1 review",
                "team": self.team.name,
                "assigned_reviewer": self.other_reviewer,
                "subject_doctype": "GBOS Work Item",
                "subject_name": hidden_work.name,
                "business_status": "Pending",
                "review_status": "Pending",
            }
        ).insert(ignore_permissions=True)

        frappe.set_user(self.reviewer)
        visible_cases = frappe.get_list("GBOS Review Case", pluck="name")
        self.assertIn(assigned_case.name, visible_cases)
        self.assertNotIn(hidden_case.name, visible_cases)
        visible_work = list_work_items(page_size=50)
        visible_names = {row["name"] for row in visible_work["data"]}
        self.assertIn(assigned_work.name, visible_names)
        self.assertNotIn(hidden_work.name, visible_names)

        assigned_work = frappe.get_doc("GBOS Work Item", assigned_work.name)
        assigned_work.title = "Reviewer must not mutate the business subject"
        with self.assertRaises(frappe.PermissionError):
            assigned_work.save()

    def test_reviewer_cannot_reassign_or_decide_case_through_generic_save(self) -> None:
        work = self._work_item()
        review = frappe.get_doc(
            {
                "doctype": "GBOS Review Case",
                "title": "Governed decision",
                "team": self.team.name,
                "assigned_reviewer": self.reviewer,
                "subject_doctype": "GBOS Work Item",
                "subject_name": work.name,
                "business_status": "Pending",
                "review_status": "Pending",
            }
        ).insert(ignore_permissions=True)

        frappe.set_user(self.reviewer)
        review.assigned_reviewer = self.other_reviewer
        with self.assertRaises(frappe.PermissionError):
            review.save()

        review.reload()
        review.business_status = "Approved"
        review.decision_note = "Approved by the assigned synthetic reviewer"
        with self.assertRaises(frappe.PermissionError):
            review.save()

    def test_privacy_auditor_cannot_write_business_records(self) -> None:
        work = self._work_item()
        frappe.set_user(self.auditor)
        work = frappe.get_doc("GBOS Work Item", work.name)
        work.title = "Auditor must remain read only"

        with self.assertRaises(frappe.PermissionError):
            work.save()

    def test_guest_cannot_access_the_bff(self) -> None:
        frappe.set_user("Guest")

        result = list_work_items(page_size=1)

        self.assertEqual(result["error"]["code"], "authentication_required")
        self.assertEqual(frappe.local.response["http_status_code"], 401)

    def test_idempotency_replays_and_rejects_payload_conflict(self) -> None:
        calls = 0

        def execute() -> dict[str, str]:
            nonlocal calls
            calls += 1
            return {"doctype": "GBOS Team", "name": self.team.name}

        first = run_idempotent(
            "test.command",
            "gate-one-idempotency",
            {"value": 1},
            execute,
        )
        replay = run_idempotent(
            "test.command",
            "gate-one-idempotency",
            {"value": 1},
            execute,
        )

        self.assertEqual(first[0], replay[0])
        self.assertTrue(replay[1])
        self.assertEqual(calls, 1)
        with self.assertRaises(BFFError):
            run_idempotent(
                "test.command",
                "gate-one-idempotency",
                {"value": 2},
                execute,
            )
        with self.assertRaises(BFFError):
            run_idempotent(
                "another.command",
                "gate-one-idempotency",
                {"value": 1},
                execute,
            )

    def test_gbos_commands_do_not_create_erpnext_transactions(self) -> None:
        counts_before = {
            doctype: frappe.db.count(doctype)
            for doctype in (
                "Sales Order",
                "Purchase Order",
                "Stock Entry",
                "GL Entry",
            )
        }
        work = self._work_item()
        demand = frappe.get_doc(
            {
                "doctype": "GBOS Demand Signal",
                "title": "Gate 1 fixture demand",
                "team": self.team.name,
                "business_status": "Draft",
                "review_status": "Pending",
            }
        ).insert(ignore_permissions=True)
        demand.business_status = "Confirmed"
        demand.save(ignore_permissions=True)

        frappe.set_user(self.member)
        sample_result = create_project(
            team=self.team.name,
            title="Gate 1 fixture sample",
            expected_revision=0,
            idempotency_key="gate-one-create-sample",
        )
        frappe.set_user(self.buyer)
        sourcing_result = create_from_demand(
            demand=demand.name,
            expected_revision=demand.revision,
            idempotency_key="gate-one-source-demand",
        )
        frappe.set_user(self.member)
        work_result = transition(
            name=work.name,
            to_status="In Progress",
            expected_revision=work.revision,
            idempotency_key="gate-one-transition-work",
        )

        self.assertIn("data", sample_result)
        self.assertIn("data", sourcing_result)
        self.assertIn("data", work_result)
        counts_after = {doctype: frappe.db.count(doctype) for doctype in counts_before}
        self.assertEqual(counts_after, counts_before)
