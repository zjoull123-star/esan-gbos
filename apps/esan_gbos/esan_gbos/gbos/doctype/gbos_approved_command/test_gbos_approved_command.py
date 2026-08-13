from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase


class TestGBOSApprovedCommand(FrappeTestCase):
    def test_doctype_is_not_generically_accessible(self) -> None:
        self.assertFalse(frappe.get_meta("GBOS Approved Command").permissions)
