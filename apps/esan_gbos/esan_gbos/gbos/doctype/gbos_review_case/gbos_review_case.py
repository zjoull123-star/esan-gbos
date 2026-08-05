import frappe
from frappe.utils import now_datetime

from esan_gbos.gbos.doctype.base import GBOSDocument

_REVIEWER_IMMUTABLE_FIELDS = (
    "title",
    "team",
    "assigned_reviewer",
    "subject_doctype",
    "subject_name",
    "origin",
    "origin_reference",
)


class GBOSReviewCase(GBOSDocument):
    def validate(self) -> None:
        self._protect_case_scope()
        self._sync_decision_state()
        super().validate()

    def _protect_case_scope(self) -> None:
        before = self.get_doc_before_save()
        roles = set(frappe.get_roles())
        if not before or "Reviewer" not in roles or "GBOS Admin" in roles:
            return
        if before.assigned_reviewer != frappe.session.user:
            raise frappe.PermissionError
        for fieldname in _REVIEWER_IMMUTABLE_FIELDS:
            if self.get(fieldname) != before.get(fieldname):
                raise frappe.PermissionError

    def _sync_decision_state(self) -> None:
        before = self.get_doc_before_save()
        if not before or self.business_status == before.business_status:
            return
        if self.business_status in {"Approved", "Rejected", "Superseded"}:
            self.review_status = self.business_status
            self.decided_at = now_datetime()
