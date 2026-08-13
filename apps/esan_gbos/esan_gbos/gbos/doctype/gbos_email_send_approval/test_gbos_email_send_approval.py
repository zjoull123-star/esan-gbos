from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from esan_gbos.api.v5.email_send import _approve_locked, approve, submit_for_review
from esan_gbos.domain.email_review_policy import (
    protect_live_email_send_snapshot,
    protected_user_ref,
)
from esan_gbos.domain.naming import make_gbos_name
from esan_gbos.gbos.doctype.gbos_command_publication.gbos_command_publication import (
    GBOSCommandPublication,
)
from esan_gbos.gbos.doctype.gbos_email_send_approval.gbos_email_send_approval import (
    approval_snapshot,
)

TEST_OWNER = "gbos-email-send-owner@example.invalid"

IGNORE_TEST_RECORD_DEPENDENCIES = [
    "DocType",
    "GBOS Review Decision",
    "GBOS Approved Command",
    "GBOS Command Publication",
    "GBOS Team",
    "User",
]


class TestGBOSEmailSendApproval(IntegrationTestCase):
    def setUp(self) -> None:
        frappe.set_user("Administrator")
        if not frappe.db.exists("User", TEST_OWNER):
            frappe.get_doc(
                {
                    "doctype": "User",
                    "email": TEST_OWNER,
                    "first_name": "GBOS email send owner",
                    "send_welcome_email": 0,
                }
            ).insert(ignore_permissions=True)
        user = frappe.get_doc("User", TEST_OWNER)
        if "Sales User" not in frappe.get_roles(TEST_OWNER):
            user.add_roles("Sales User")
        self.team = frappe.get_doc(
            {
                "doctype": "GBOS Team",
                "team_name": "Email send native test team",
                "members": [{"user": TEST_OWNER, "team_role": "Member", "enabled": 1}],
            }
        ).insert(ignore_permissions=True)
        frappe.set_user(TEST_OWNER)

    def tearDown(self) -> None:
        frappe.set_user("Administrator")

    def test_doctype_has_no_standard_permissions_and_forbids_sensitive_fields(self) -> None:
        meta = frappe.get_meta("GBOS Email Send Approval")
        self.assertFalse(meta.permissions)
        fields = {field.fieldname for field in meta.fields}
        for forbidden in (
            "subject",
            "body",
            "mime",
            "sender_address",
            "recipient_address",
            "provider_payload",
            "send_state",
        ):
            self.assertNotIn(forbidden, fields)
        self.assertTrue(json.loads(meta.as_json()))

    def _live_snapshot(self) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "site_id": frappe.local.site,
            "processing_purpose": "customer_service",
            "team_ref": self.team.name,
            "assignee_user_name": TEST_OWNER,
            "approval_expires_at": (datetime.now(UTC) + timedelta(hours=1))
            .isoformat()
            .replace("+00:00", "Z"),
            "mailbox_ref": make_gbos_name("MBX"),
            "mailbox_config_revision": 1,
            "inbox_item_ref": make_gbos_name("INB"),
            "inbox_item_revision": 1,
            "conversation_ref": make_gbos_name("CNV"),
            "conversation_revision": 1,
            "reply_draft_ref": make_gbos_name("DRF"),
            "reply_draft_revision": 1,
            "reply_draft_digest": "sha256:" + "1" * 64,
            "participants": [
                {
                    "address_role": "sender",
                    "opaque_address_ref": "extid:v1:email:" + "A" * 43,
                },
                {
                    "address_role": "to",
                    "opaque_address_ref": "extid:v1:email:" + "B" * 43,
                    "identity_mapping_ref": make_gbos_name("EID"),
                    "identity_mapping_revision": 1,
                },
            ],
            "party_ref": make_gbos_name("PTY"),
            "party_revision": 1,
            "team_revision": int(self.team.revision),
            "owner_user_name": TEST_OWNER,
            "owner_eligibility_revision": "sha256:" + "2" * 64,
            "final_mime_evidence_ref": make_gbos_name("EVR"),
            "final_mime_digest": "sha256:" + "3" * 64,
            "evidence_refs": [make_gbos_name("EVR")],
            "stable_client_request_id": make_gbos_name("CLI"),
        }

    def _submit(self, live: dict[str, object]) -> tuple[dict[str, object], object]:
        frappe.local.gbos_request_id = None
        protected = protect_live_email_send_snapshot(
            live,
            site_id=frappe.local.site,
            authenticated_user_name=TEST_OWNER,
        )
        with patch(
            "esan_gbos.api.v5.email_send._derive_submission_snapshot",
            return_value=protected,
        ):
            response = submit_for_review(
                str(live["inbox_item_ref"]),
                str(live["reply_draft_ref"]),
                int(live["inbox_item_revision"]),
                int(live["reply_draft_revision"]),
                "email-send-submit-" + hashlib.sha256(repr(live).encode()).hexdigest(),
            )
        self.assertNotIn("error", response)
        case = frappe.get_doc("GBOS Review Case", response["data"]["review_case_ref"])
        return response, case

    def test_specialized_approval_is_atomic_replay_stable_and_contains_no_raw_user(self) -> None:
        live = self._live_snapshot()
        submitted, case = self._submit(live)
        approval = frappe.get_doc(
            "GBOS Email Send Approval", submitted["data"]["email_send_approval_ref"]
        )
        idempotency_key = "idem:v2:" + hashlib.sha256(case.name.encode()).hexdigest()
        frappe.local.gbos_request_id = None
        protected_live = protect_live_email_send_snapshot(
            live,
            site_id=frappe.local.site,
            authenticated_user_name=TEST_OWNER,
        )
        with patch(
            "esan_gbos.api.v5.email_send._derive_approval_live_snapshot",
            return_value=protected_live,
        ):
            first = approve(
                case.name,
                case.revision,
                "Current owner approves the frozen reply.",
                idempotency_key,
            )
            frappe.local.gbos_request_id = None
            replay = approve(
                case.name,
                case.revision,
                "Current owner approves the frozen reply.",
                idempotency_key,
            )

        self.assertNotIn("error", first)
        self.assertEqual(replay["data"], first["data"])
        self.assertTrue(replay["meta"]["replayed"])
        self.assertEqual(
            frappe.db.count("GBOS Approved Command", {"review_case": case.name}),
            1,
        )
        self.assertEqual(
            frappe.db.count(
                "GBOS Command Publication",
                {"approved_command": first["data"]["approved_command_ref"]},
            ),
            1,
        )
        command = frappe.get_doc("GBOS Approved Command", first["data"]["approved_command_ref"])
        publication = frappe.get_doc(
            "GBOS Command Publication", first["data"]["command_publication_ref"]
        )
        protected = protected_user_ref(frappe.local.site, TEST_OWNER)
        self.assertEqual(approval.assignee_user_ref, protected)
        self.assertEqual(command.actor_user_ref, protected)
        for stored in (
            approval_snapshot(approval),
            frappe.parse_json(command.command_payload),
            frappe.parse_json(publication.command_payload),
        ):
            self.assertNotIn(TEST_OWNER, repr(stored))

        approval.mailbox_ref = make_gbos_name("MBX")
        with self.assertRaises(frappe.PermissionError):
            approval.save(ignore_permissions=True)
        command.payload_sha256 = "0" * 64
        with self.assertRaises(frappe.PermissionError):
            command.save(ignore_permissions=True)
        publication.command_payload = "{}"
        with self.assertRaises(frappe.PermissionError):
            publication.save(ignore_permissions=True)

    def test_publication_insert_failure_rolls_back_decision_and_command(self) -> None:
        live = self._live_snapshot()
        _submitted, case = self._submit(live)
        protected = protect_live_email_send_snapshot(
            live,
            site_id=frappe.local.site,
            authenticated_user_name=TEST_OWNER,
        )
        idempotency_key = (
            "idem:v2:" + hashlib.sha256((case.name + "-rollback").encode()).hexdigest()
        )
        savepoint = "email_send_publication_failure"
        frappe.db.savepoint(savepoint)
        with (
            patch.object(
                GBOSCommandPublication,
                "validate",
                side_effect=frappe.ValidationError("injected publication failure"),
            ),
            self.assertRaises(frappe.ValidationError),
            patch(
                "esan_gbos.api.v5.email_send._derive_approval_live_snapshot",
                return_value=protected,
            ),
        ):
            _approve_locked(
                payload={
                    "review_case_name": case.name,
                    "expected_revision": int(case.revision),
                    "decision_note": "Approval must roll back atomically.",
                    "idempotency_key": idempotency_key,
                },
                actor=TEST_OWNER,
                issued_at=datetime.now(UTC),
            )
        frappe.db.rollback(save_point=savepoint)

        pending = frappe.get_doc("GBOS Review Case", case.name)
        self.assertEqual(pending.business_status, "Pending")
        self.assertEqual(
            frappe.db.count("GBOS Approved Command", {"review_case": case.name}),
            0,
        )
