from __future__ import annotations

import frappe
from frappe.model.document import Document

from esan_gbos.domain.naming import make_gbos_name


class GBOSReviewDecision(Document):
    def autoname(self) -> None:
        self.name = make_gbos_name("RDC")

    def validate(self) -> None:
        if self.get_doc_before_save():
            raise frappe.PermissionError

    def on_trash(self) -> None:
        raise frappe.PermissionError
