from __future__ import annotations

from typing import Any

import frappe
from frappe.tests import IntegrationTestCase

from esan_gbos.api.v2.review_case import decide
from esan_gbos.domain.review_dto import canonical_payload_hash
from esan_gbos.gbos.doctype.gbos_review_case.gbos_review_case import (
    build_case_payload,
    build_subject_snapshot,
)

TEST_MEMBER = "gbos-identity-member@example.invalid"
TEST_REVIEWER = "gbos-identity-reviewer@example.invalid"
TEST_INTEGRATION_ADMIN = "gbos-identity-admin@example.invalid"

IGNORE_TEST_RECORD_DEPENDENCIES = [
    "DocType",
    "GBOS Review Decision",
    "GBOS Team",
    "User",
]


class TestGBOSExternalIdentityAuthority(IntegrationTestCase):
    def setUp(self) -> None:
        frappe.set_user("Administrator")
        self.member = self._user(TEST_MEMBER, "Sales User")
        self.reviewer = self._user(TEST_REVIEWER, "Reviewer")
        self.integration_admin = self._user(TEST_INTEGRATION_ADMIN, "Integration Admin")
        self.team = frappe.get_doc(
            {
                "doctype": "GBOS Team",
                "team_name": f"Identity authority {frappe.generate_hash(length=8)}",
                "members": [
                    {"user": self.member, "team_role": "Member", "enabled": 1},
                    {"user": self.reviewer, "team_role": "Reviewer", "enabled": 1},
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

    def _identity(self, **overrides: Any) -> Any:
        values: dict[str, Any] = {
            "doctype": "GBOS External Identity",
            "team": self.team.name,
            "identity_provider": "email",
            "external_subject": f"extid:v1:email:{frappe.generate_hash(length=24)}",
            "identity_type": "User",
            "user": self.member,
            "origin": "Manual",
            "business_status": "Active",
            "review_status": "Pending",
        }
        values.update(overrides)
        return frappe.get_doc(values).insert(ignore_permissions=True)

    def _case(self, identity: Any) -> Any:
        snapshot = build_subject_snapshot(identity)
        values: dict[str, Any] = {
            "doctype": "GBOS Review Case",
            "title": "Review opaque external identity mapping",
            "team": self.team.name,
            "assigned_reviewer": self.reviewer,
            "subject_doctype": identity.doctype,
            "subject_name": identity.name,
            "subject_revision": identity.revision,
            "subject_payload_sha256": canonical_payload_hash(snapshot),
            "subject_snapshot": frappe.as_json(snapshot),
            "evidence_refs": frappe.as_json(["OBS-IDENTITY-01"]),
            "policy_version": "identity-resolution/v1",
            "business_status": "Pending",
            "review_status": "Pending",
        }
        values["case_payload_sha256"] = canonical_payload_hash(build_case_payload(values))
        return frappe.get_doc(values).insert(ignore_permissions=True)

    def _decide(self, case: Any, decision: str) -> dict[str, Any]:
        frappe.local.gbos_request_id = None
        return decide(
            name=case.name,
            decision=decision,
            decision_note="The pinned evidence supports this governed outcome.",
            expected_revision=case.revision,
            expected_subject_revision=case.subject_revision,
            idempotency_key=f"identity-review-{case.name}-{decision}",
            subject_payload_sha256=case.subject_payload_sha256,
            evidence_refs=frappe.parse_json(case.evidence_refs),
            policy_version=case.policy_version,
            expected_case_payload_hash=case.case_payload_sha256,
        )

    def test_native_insert_rejects_raw_subject_wrong_target_and_duplicate(self) -> None:
        with self.assertRaises(frappe.ValidationError):
            self._identity(external_subject="raw-person@example.invalid")
        with self.assertRaises(frappe.ValidationError):
            self._identity(identity_type="Channel", user=self.member)

        identity = self._identity()
        with self.assertRaises(frappe.ValidationError):
            self._identity(
                identity_provider=identity.identity_provider,
                external_subject=identity.external_subject,
            )

    def test_ai_cannot_create_approved_mapping_or_use_generic_set_value(self) -> None:
        with self.assertRaises(frappe.ValidationError):
            self._identity(origin="AI", review_status="Approved")

        identity = self._identity()
        frappe.set_user(self.integration_admin)
        with self.assertRaises(frappe.PermissionError):
            frappe.get_doc(identity.doctype, identity.name).db_set(
                "review_status",
                "Approved",
            )

    def test_review_approval_activates_and_rejection_remains_non_authoritative(self) -> None:
        approved = self._identity()
        rejected = self._identity()
        approved_case = self._case(approved)
        rejected_case = self._case(rejected)
        frappe.set_user(self.reviewer)

        approved_result = self._decide(approved_case, "Approved")
        rejected_result = self._decide(rejected_case, "Rejected")

        self.assertEqual(approved_result["data"]["case"]["business_status"], "Approved")
        self.assertEqual(rejected_result["data"]["case"]["business_status"], "Rejected")
        approved = frappe.get_doc(approved.doctype, approved.name)
        rejected = frappe.get_doc(rejected.doctype, rejected.name)
        self.assertEqual((approved.review_status, approved.business_status), ("Approved", "Active"))
        self.assertEqual((rejected.review_status, rejected.business_status), ("Rejected", "Active"))

    def test_stale_mapping_revision_stops_review_decision(self) -> None:
        identity = self._identity()
        case = self._case(identity)
        frappe.set_user("Administrator")
        identity = frappe.get_doc(identity.doctype, identity.name)
        identity.origin_reference = "new-proposal-reference"
        identity.save(ignore_permissions=True)
        frappe.set_user(self.reviewer)

        result = self._decide(case, "Approved")

        self.assertEqual(result["error"]["code"], "revision_conflict")
        self.assertEqual(result["error"]["details"]["conflict"], "subject_revision")

    def test_governed_supersession_archives_mapping(self) -> None:
        identity = self._identity()
        case = self._case(identity)
        case.flags.gbos_review_command = True
        case.business_status = "Superseded"
        case.review_status = "Superseded"

        case.save(ignore_permissions=True)

        identity = frappe.get_doc(identity.doctype, identity.name)
        self.assertEqual(identity.review_status, "Superseded")
        self.assertEqual(identity.business_status, "Archived")
