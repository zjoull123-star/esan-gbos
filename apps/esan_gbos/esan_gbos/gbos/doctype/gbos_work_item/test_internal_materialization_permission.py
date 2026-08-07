from __future__ import annotations

from typing import Any

import frappe
from frappe.tests import IntegrationTestCase

from esan_gbos.domain.permissions import INTERNAL_MATERIALIZER_ROLE
from esan_gbos.install import (
    ensure_internal_materialization_audit_permissions,
    ensure_permissions,
    ensure_roles,
)
from esan_gbos.permissions import internal_materialization_permission_scope

SERVICE_USER = "gbos-materializer-test@example.invalid"

IGNORE_TEST_RECORD_DEPENDENCIES = [
    "DocType",
    "GBOS Team",
    "GBOS Work Item",
    "Integration Request",
    "User",
]


class TestInternalMaterializationPermission(IntegrationTestCase):
    def setUp(self) -> None:
        frappe.set_user("Administrator")
        ensure_roles()
        ensure_permissions()
        ensure_internal_materialization_audit_permissions()
        frappe.clear_cache()
        if not frappe.db.exists("User", SERVICE_USER):
            frappe.get_doc(
                {
                    "doctype": "User",
                    "email": SERVICE_USER,
                    "first_name": "GBOS Materializer",
                    "send_welcome_email": 0,
                }
            ).insert(ignore_permissions=True)
        user = frappe.get_doc("User", SERVICE_USER)
        if INTERNAL_MATERIALIZER_ROLE not in frappe.get_roles(SERVICE_USER):
            user.add_roles(INTERNAL_MATERIALIZER_ROLE)
        self.team = frappe.get_doc(
            {
                "doctype": "GBOS Team",
                "team_name": "Internal materialization permission test",
            }
        ).insert(ignore_permissions=True)
        self.subject = frappe.get_doc(
            {
                "doctype": "GBOS Work Item",
                "title": "Controlled subject",
                "team": self.team.name,
                "origin": "Manual",
                "business_status": "Open",
                "review_status": "Pending",
            }
        ).insert(ignore_permissions=True)

    def tearDown(self) -> None:
        frappe.set_user("Administrator")

    def _draft(self, suffix: str) -> Any:
        return frappe.get_doc(
            {
                "doctype": "GBOS Work Item",
                "title": f"Controlled AI Draft {suffix}",
                "team": self.team.name,
                "reference_doctype": self.subject.doctype,
                "reference_name": self.subject.name,
                "origin": "AI",
                "origin_reference": f"proposal-{suffix}",
                "business_status": "Open",
                "review_status": "AI Draft",
            }
        )

    def test_direct_document_insert_stays_denied_for_service_role(self) -> None:
        frappe.set_user(SERVICE_USER)

        with self.assertRaises(frappe.PermissionError):
            self._draft("direct").insert()

    def test_request_scope_grants_only_controlled_core_permissions(self) -> None:
        frappe.set_user(SERVICE_USER)
        audit = frappe.get_doc(
            {
                "doctype": "Integration Request",
                "request_id": "materialization-permission-test",
                "integration_request_service": "esan_gbos.internal.materialization",
                "status": "Authorized",
            }
        )

        with self.assertRaises(frappe.PermissionError):
            self.subject.check_permission("read")
        with self.assertRaises(frappe.PermissionError):
            audit.check_permission("create")

        with internal_materialization_permission_scope("resolve"):
            self.subject.check_permission("read")
            self.team.check_permission("read")
            with self.assertRaises(frappe.PermissionError):
                self._draft("resolve").check_permission("create")

        with internal_materialization_permission_scope("apply"):
            self.subject.check_permission("read")
            audit.check_permission("create")
            draft = self._draft("apply")
            draft.check_permission("create")
            with self.assertRaises(frappe.PermissionError):
                draft.check_permission("delete")

        with self.assertRaises(frappe.PermissionError):
            self.subject.check_permission("read")
        with self.assertRaises(frappe.PermissionError):
            audit.check_permission("write")

    def test_scope_is_removed_when_endpoint_work_raises(self) -> None:
        frappe.set_user(SERVICE_USER)

        with (
            self.assertRaisesRegex(RuntimeError, "forced"),
            internal_materialization_permission_scope("apply"),
        ):
            self.subject.check_permission("read")
            raise RuntimeError("forced")

        with self.assertRaises(frappe.PermissionError):
            self.subject.check_permission("read")
