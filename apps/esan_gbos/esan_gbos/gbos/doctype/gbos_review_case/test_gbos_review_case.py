from __future__ import annotations

from typing import Any

import frappe
from frappe.tests import IntegrationTestCase

from esan_gbos.api.v2.review_case import decide, get, list
from esan_gbos.domain.review_dto import canonical_payload_hash
from esan_gbos.gbos.doctype.gbos_review_case.gbos_review_case import (
    build_case_payload,
    build_subject_snapshot,
)

TEST_REVIEWER = "gbos-gate4-reviewer@example.invalid"
TEST_OTHER_REVIEWER = "gbos-gate4-other-reviewer@example.invalid"
TEST_ADMIN = "gbos-gate4-admin@example.invalid"

# This suite creates its complete graph explicitly.  In particular, allowing
# Frappe to synthesize the immutable decision link recursively reaches User and
# ERPNext Company fixtures, which correctly collide with the GBOS V1
# transaction guard.
IGNORE_TEST_RECORD_DEPENDENCIES = [
    "DocType",
    "GBOS Review Decision",
    "GBOS Team",
    "User",
]


class TestGBOSGateFourReviewCase(IntegrationTestCase):
    def setUp(self) -> None:
        frappe.set_user("Administrator")
        self.reviewer = self._user(TEST_REVIEWER, "Reviewer", "Sales User")
        self.other_reviewer = self._user(TEST_OTHER_REVIEWER, "Reviewer")
        self.admin = self._user(TEST_ADMIN, "GBOS Admin")
        self.team = frappe.get_doc(
            {
                "doctype": "GBOS Team",
                "team_name": "Gate 4 review test team",
                "members": [
                    {"user": self.reviewer, "team_role": "Member", "enabled": 1},
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

    def _case(self, *, assigned_reviewer: str | None = None) -> tuple[Any, Any, dict[str, Any]]:
        work = frappe.get_doc(
            {
                "doctype": "GBOS Work Item",
                "title": "Call synthetic customer",
                "team": self.team.name,
                "business_status": "Open",
                "review_status": "Pending",
            }
        ).insert(ignore_permissions=True)
        snapshot = build_subject_snapshot(work)
        evidence_refs = ["OBS-GATE4-01"]
        values: dict[str, Any] = {
            "doctype": "GBOS Review Case",
            "title": "Review synthetic customer follow-up",
            "team": self.team.name,
            "assigned_reviewer": assigned_reviewer or self.reviewer,
            "subject_doctype": work.doctype,
            "subject_name": work.name,
            "subject_revision": work.revision,
            "subject_payload_sha256": canonical_payload_hash(snapshot),
            "subject_snapshot": frappe.as_json(snapshot),
            "evidence_refs": frappe.as_json(evidence_refs),
            "policy_version": "review-policy/v1",
            "business_status": "Pending",
            "review_status": "Pending",
        }
        values["case_payload_sha256"] = canonical_payload_hash(build_case_payload(values))
        case = frappe.get_doc(values).insert(ignore_permissions=True)
        return case, work, snapshot

    def _decide(self, case: Any, **overrides: Any) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "name": case.name,
            "decision": "Approved",
            "decision_note": "Evidence supports the governed internal approval.",
            "expected_revision": case.revision,
            "expected_subject_revision": case.subject_revision,
            "idempotency_key": f"review-{case.name}",
            "subject_payload_sha256": case.subject_payload_sha256,
            "evidence_refs": frappe.parse_json(case.evidence_refs),
            "policy_version": case.policy_version,
            "expected_case_payload_hash": case.case_payload_sha256,
        }
        arguments.update(overrides)
        # Direct test invocation reuses frappe.local across calls, unlike
        # separate HTTP requests.  Clear the per-request cache so audit request
        # IDs retain their real wire-level uniqueness.
        frappe.local.gbos_request_id = None
        return decide(**arguments)

    def test_reviewer_generic_doctype_write_cannot_decide_case(self) -> None:
        case, _work, _snapshot = self._case()
        frappe.set_user(self.reviewer)
        case = frappe.get_doc("GBOS Review Case", case.name)
        case.business_status = "Approved"
        case.decision_note = "Direct writes must not be a command path."

        with self.assertRaises(frappe.PermissionError):
            case.save()

    def test_admin_without_explicit_reviewer_role_cannot_decide(self) -> None:
        case, _work, _snapshot = self._case(assigned_reviewer=self.admin)
        frappe.set_user(self.admin)

        result = self._decide(case)

        self.assertEqual(result["error"]["code"], "permission_denied")
        self.assertEqual(frappe.local.response["http_status_code"], 403)

    def test_admin_cannot_bypass_command_by_editing_review_status(self) -> None:
        case, _work, _snapshot = self._case()
        frappe.set_user(self.admin)
        case = frappe.get_doc("GBOS Review Case", case.name)
        case.review_status = "Approved"

        with self.assertRaises(frappe.PermissionError):
            case.save()

    def test_only_assigned_reviewer_can_list_and_get_frozen_case(self) -> None:
        assigned, _work, snapshot = self._case()
        hidden, _hidden_work, _hidden_snapshot = self._case(assigned_reviewer=self.other_reviewer)
        frappe.set_user(self.reviewer)

        rows = list(page_size=20)["data"]["cases"]
        detail = get(assigned.name)["data"]["case"]

        self.assertIn(assigned.name, {row["name"] for row in rows})
        self.assertNotIn(hidden.name, {row["name"] for row in rows})
        self.assertEqual(detail["subject"]["snapshot"], snapshot)

    def test_multi_role_reviewer_cannot_mutate_active_subject(self) -> None:
        _case, work, _snapshot = self._case()
        frappe.set_user(self.reviewer)
        work = frappe.get_doc("GBOS Work Item", work.name)
        work.title = "Attempted mutation while review is Pending"

        with self.assertRaises(frappe.PermissionError):
            work.save()

    def test_decide_changes_case_only_and_appends_immutable_decision(self) -> None:
        case, work, snapshot = self._case()
        subject_before = build_subject_snapshot(work)
        frappe.set_user(self.reviewer)

        result = self._decide(case)

        self.assertEqual(result["data"]["case"]["business_status"], "Approved")
        current_subject = frappe.get_doc(work.doctype, work.name)
        self.assertEqual(build_subject_snapshot(current_subject), subject_before)
        decision = frappe.get_doc("GBOS Review Decision", result["data"]["decision"]["name"])
        self.assertEqual(decision.reviewer, self.reviewer)
        self.assertEqual(decision.subject_snapshot, frappe.as_json(snapshot))
        decision.reason = "Audit records cannot be rewritten."
        with self.assertRaises(frappe.PermissionError):
            decision.save(ignore_permissions=True)

    def test_same_payload_replays_and_changed_payload_conflicts(self) -> None:
        case, _work, _snapshot = self._case()
        frappe.set_user(self.reviewer)

        first = self._decide(case)
        replay = self._decide(case)
        changed = self._decide(case, decision_note="Different reason under the same key.")

        self.assertFalse(first["meta"]["replayed"])
        self.assertTrue(replay["meta"]["replayed"])
        self.assertEqual(first["data"], replay["data"])
        self.assertEqual(changed["error"]["code"], "idempotency_conflict")

    def test_stale_case_or_subject_pin_returns_revision_conflict(self) -> None:
        case, _work, _snapshot = self._case()
        frappe.set_user(self.reviewer)

        stale_case = self._decide(case, expected_revision=case.revision + 1)

        self.assertEqual(stale_case["error"]["code"], "revision_conflict")
        self.assertEqual(stale_case["error"]["details"]["conflict"], "case_revision")

    def test_deleted_subject_returns_revision_conflict_not_not_found(self) -> None:
        case, work, _snapshot = self._case()
        # Simulate an out-of-band loss: normal Frappe deletion correctly
        # refuses to remove a document referenced by the Review Case.
        frappe.db.delete(work.doctype, {"name": work.name})
        frappe.set_user(self.reviewer)

        result = self._decide(case)

        self.assertEqual(result["error"]["code"], "revision_conflict")
        self.assertEqual(result["error"]["details"]["conflict"], "subject_missing")

    def test_closed_case_get_uses_snapshot_after_subject_changes(self) -> None:
        case, work, snapshot = self._case()
        frappe.set_user(self.reviewer)
        self._decide(case)
        frappe.set_user("Administrator")
        work = frappe.get_doc(work.doctype, work.name)
        work.title = "Changed after the review closed"
        work.save(ignore_permissions=True)
        frappe.set_user(self.reviewer)

        detail = get(case.name)["data"]["case"]

        self.assertEqual(detail["subject"]["snapshot"], snapshot)
        self.assertNotEqual(detail["subject"]["snapshot"]["title"], work.title)
