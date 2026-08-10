from __future__ import annotations

from typing import Any
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from esan_gbos.api.v4.ai_draft import get as get_draft
from esan_gbos.api.v4.ai_draft import submit_for_review
from esan_gbos.api.v4.communication import list as list_communications
from esan_gbos.api.v4.integration import pause

TEST_REVIEWER = "gbos-v4-reviewer@example.invalid"
TEST_OTHER_REVIEWER = "gbos-v4-other-reviewer@example.invalid"
TEST_SALES = "gbos-v4-sales@example.invalid"
TEST_ADMIN = "gbos-v4-admin@example.invalid"

IGNORE_TEST_RECORD_DEPENDENCIES = [
    "DocType",
    "GBOS Informal Evidence Ref",
    "GBOS Informal Observation",
    "GBOS Team",
    "User",
]


class TestGBOSInformalObservation(IntegrationTestCase):
    def setUp(self) -> None:
        frappe.set_user("Administrator")
        self.reviewer = self._user(TEST_REVIEWER, "Reviewer")
        self.other_reviewer = self._user(TEST_OTHER_REVIEWER, "Reviewer")
        self.sales = self._user(TEST_SALES, "Sales User")
        self.admin = self._user(TEST_ADMIN, "GBOS Admin")
        self.team = self._team("V4 assigned team", self.reviewer, self.sales)
        self.other_team = self._team("V4 other team", self.other_reviewer)

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

    @staticmethod
    def _team(label: str, *members: str) -> Any:
        return frappe.get_doc(
            {
                "doctype": "GBOS Team",
                "team_name": label,
                "members": [
                    {"user": user, "team_role": "Member", "enabled": 1} for user in members
                ],
            }
        ).insert(ignore_permissions=True)

    def _observation(
        self,
        *,
        team: str | None = None,
        is_official_metric: int = 0,
        model_name: str = "deepseek-v4-flash",
    ) -> Any:
        return frappe.get_doc(
            {
                "doctype": "GBOS Informal Observation",
                "subject": "客户机会观察",
                "summary_zh": "这是只供人工审核的非正式中文摘要。",
                "team": team or self.team.name,
                "evidence_refs": [
                    {
                        "evidence_ref": "EVD-V4-001",
                        "locator_ref": "evidence://EVD-V4-001",
                    }
                ],
                "model_name": model_name,
                "model_version": "2026-08-01",
                "is_official_metric": is_official_metric,
                "origin": "AI",
                "origin_reference": "proposal-v4-001",
                "review_status": "AI Draft",
            }
        ).insert(ignore_permissions=True)

    @staticmethod
    def _agent_detail(
        service: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if service != "Agent":
            raise AssertionError("unexpected downstream")
        return {
            "draft": {
                "draft_id": "proposal-v4-001",
                "kind": "CEO Informal Observation",
                "status": "AI Draft",
                "origin": "AI",
                "subject": "downstream metadata only",
                "evidence": [
                    {"ref": "EVD-V4-001", "locator": "evidence://EVD-V4-001"},
                ],
                "model": {
                    "name": "deepseek-v4-flash",
                    "version": "2026-08-01",
                },
                "revision": 1,
            }
        }

    def test_informal_observation_can_never_be_an_official_metric(self) -> None:
        with self.assertRaises(frappe.ValidationError):
            self._observation(is_official_metric=1)

    def test_informal_observation_rejects_an_unapproved_model_identity(self) -> None:
        with self.assertRaises(frappe.ValidationError):
            self._observation(model_name="unapproved-model")

    def test_direct_doctype_save_cannot_submit_ai_draft(self) -> None:
        observation = self._observation()
        frappe.set_user(self.admin)
        observation = frappe.get_doc(observation.doctype, observation.name)
        observation.review_status = "Pending"

        with self.assertRaises(frappe.PermissionError):
            observation.save(ignore_permissions=True)

    def test_generic_work_item_save_cannot_bypass_draft_submission(self) -> None:
        work = frappe.get_doc(
            {
                "doctype": "GBOS Work Item",
                "title": "AI-created internal follow-up",
                "team": self.team.name,
                "assigned_to": self.reviewer,
                "origin": "AI",
                "origin_reference": "proposal-work-v4-001",
                "business_status": "Open",
                "review_status": "AI Draft",
            }
        ).insert(ignore_permissions=True)
        frappe.set_user(self.admin)
        work = frappe.get_doc(work.doctype, work.name)
        work.review_status = "Pending"

        with self.assertRaises(frappe.PermissionError):
            work.save(ignore_permissions=True)

    def test_reviewer_can_read_only_an_explicitly_assigned_informal_observation(self) -> None:
        assigned = self._observation()
        hidden = self._observation(team=self.other_team.name)
        frappe.get_doc(
            {
                "doctype": "GBOS Work Item",
                "title": "Review the informal observation",
                "team": self.team.name,
                "assigned_to": self.reviewer,
                "reference_doctype": assigned.doctype,
                "reference_name": assigned.name,
                "origin": "Manual",
                "business_status": "Open",
                "review_status": "Pending",
            }
        ).insert(ignore_permissions=True)
        frappe.set_user(self.reviewer)

        self.assertTrue(frappe.has_permission(assigned.doctype, "read", assigned))
        self.assertFalse(frappe.has_permission(hidden.doctype, "read", hidden))

    def test_submit_changes_only_review_state_and_replays_idempotently(self) -> None:
        observation = self._observation()
        before = {
            field: observation.get(field)
            for field in (
                "subject",
                "summary_zh",
                "team",
                "model_name",
                "model_version",
                "is_official_metric",
                "origin",
                "origin_reference",
            )
        }
        before_evidence = [(row.evidence_ref, row.locator_ref) for row in observation.evidence_refs]
        frappe.set_user(self.admin)
        with patch(
            "esan_gbos.api.v4.ai_draft.call_local",
            side_effect=self._agent_detail,
        ):
            frappe.local.gbos_request_id = None
            first = submit_for_review(
                observation.name,
                observation.revision,
                "submit-observation-v4-001",
            )
            frappe.local.gbos_request_id = None
            replay = submit_for_review(
                observation.name,
                observation.revision,
                "submit-observation-v4-001",
            )

        current = frappe.get_doc(observation.doctype, observation.name)
        self.assertEqual(current.review_status, "Pending")
        self.assertEqual(first["meta"]["schema_version"], "4.0")
        self.assertFalse(first["meta"]["replayed"])
        self.assertTrue(replay["meta"]["replayed"])
        self.assertEqual(before, {field: current.get(field) for field in before})
        self.assertEqual(
            before_evidence,
            [(row.evidence_ref, row.locator_ref) for row in current.evidence_refs],
        )

    def test_unassigned_reviewer_cannot_get_an_informal_draft(self) -> None:
        observation = self._observation()
        frappe.set_user(self.reviewer)
        with patch(
            "esan_gbos.api.v4.ai_draft.call_local",
            side_effect=self._agent_detail,
        ):
            result = get_draft(observation.name)

        self.assertEqual(result["error"]["code"], "permission_denied")

    def test_sales_communication_call_passes_only_the_users_team_scope(self) -> None:
        seen: list[dict[str, Any]] = []

        def observer(service: str, **kwargs: Any) -> dict[str, Any]:
            self.assertEqual(service, "Observer")
            seen.append(kwargs["payload"])
            return {"communications": [], "next_cursor": None}

        frappe.set_user(self.sales)
        with patch("esan_gbos.api.v4.communication.call_local", side_effect=observer):
            result = list_communications(page_size=20)

        expected_teams = sorted(
            str(row["parent"])
            for row in frappe.get_all(
                "GBOS Team Member",
                filters={"user": self.sales, "enabled": 1},
                fields=["parent"],
            )
        )
        self.assertEqual(result["meta"]["schema_version"], "4.0")
        self.assertEqual(seen[0]["allowed_team_refs"], expected_teams)
        self.assertIn(self.team.name, seen[0]["allowed_team_refs"])
        self.assertNotIn(self.other_team.name, seen[0]["allowed_team_refs"])
        self.assertFalse(seen[0]["include_raw"])

    def test_connector_command_replays_and_changed_payload_conflicts(self) -> None:
        connector = {
            "instance_id": "wecom:sales",
            "channel": "wecom",
            "status": "paused",
            "checkpoint_version": 2,
            "backlog": 0,
            "last_success_at": None,
            "safe_error_code": None,
            "freshness": "fresh",
            "revision": 3,
        }
        frappe.set_user(self.admin)
        with patch(
            "esan_gbos.api.v4.integration.call_local",
            return_value={"connector": connector},
        ):
            frappe.local.gbos_request_id = None
            first = pause("wecom:sales", 2, "pause-wecom-v4-001")
            frappe.local.gbos_request_id = None
            replay = pause("wecom:sales", 2, "pause-wecom-v4-001")
            frappe.local.gbos_request_id = None
            conflict = pause("wecom:sales", 3, "pause-wecom-v4-001")

        self.assertFalse(first["meta"]["replayed"])
        self.assertTrue(replay["meta"]["replayed"])
        self.assertEqual(conflict["error"]["code"], "idempotency_conflict")
