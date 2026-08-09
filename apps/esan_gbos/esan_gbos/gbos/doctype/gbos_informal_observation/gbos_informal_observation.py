from __future__ import annotations

import frappe

from esan_gbos.domain.naming import make_gbos_name
from esan_gbos.gbos.doctype.base import GBOSDocument

_IMMUTABLE_FIELDS = (
    "subject",
    "summary_zh",
    "team",
    "evidence_refs",
    "model_name",
    "model_version",
    "is_official_metric",
    "origin",
    "origin_reference",
)


class GBOSInformalObservation(GBOSDocument):
    def autoname(self) -> None:
        self.name = make_gbos_name("OBS")

    def validate(self) -> None:
        if self.origin != "AI":
            frappe.throw("Informal observations must have AI origin")
        if self.model_name != "deepseek-v4-flash":
            frappe.throw("Informal observations require the approved model identity")
        if int(self.is_official_metric or 0) != 0:
            frappe.throw("Informal observations can never be official metrics")
        before = self.get_doc_before_save()
        if before:
            for fieldname in _IMMUTABLE_FIELDS:
                if self.get(fieldname) != before.get(fieldname):
                    raise frappe.PermissionError
            if self.review_status != before.review_status and not self.flags.gbos_ai_draft_command:
                raise frappe.PermissionError
        super().validate()

    def on_trash(self) -> None:
        raise frappe.PermissionError
