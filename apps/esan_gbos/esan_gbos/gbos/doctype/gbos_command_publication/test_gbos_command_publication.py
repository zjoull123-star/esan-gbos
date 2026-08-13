from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase


class TestGBOSCommandPublication(FrappeTestCase):
    def test_doctype_is_not_generically_accessible(self) -> None:
        self.assertFalse(frappe.get_meta("GBOS Command Publication").permissions)
        self.assertTrue(frappe.db.exists("Role", "Email Command Publication Consumer"))
        self.assertEqual(
            frappe.db.get_value("Role", "Email Command Publication Consumer", "desk_access"),
            0,
        )
