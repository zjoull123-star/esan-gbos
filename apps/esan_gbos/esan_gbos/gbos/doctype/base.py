from __future__ import annotations

import frappe
from frappe.model.document import Document

from esan_gbos.domain.naming import make_gbos_name
from esan_gbos.domain.review_dto import REVIEW_SUBJECT_DOCTYPES
from esan_gbos.domain.revision import RevisionConflict, next_revision
from esan_gbos.domain.state_machine import (
    InvalidTransition,
    validate_initial_status,
    validate_transition,
)

PREFIXES = {
    "GBOS Team": "TEM",
    "GBOS Party Profile": "PTY",
    "GBOS External Identity": "EID",
    "GBOS External Crosswalk": "XWK",
    "GBOS Product Brief": "PRB",
    "GBOS Sample Project": "SAM",
    "GBOS Sample Iteration": "ITR",
    "GBOS Sample Shipment": "SHP",
    "GBOS Sample Feedback": "FDB",
    "GBOS Demand Signal": "DEM",
    "GBOS Sourcing Event": "SRC",
    "GBOS Work Item": "WRK",
    "GBOS Review Case": "REV",
}

WORKFLOWS = {
    "GBOS Sample Project": "sample",
    "GBOS Demand Signal": "demand",
    "GBOS Sourcing Event": "sourcing",
    "GBOS Work Item": "work",
    "GBOS Review Case": "review",
}


# Frappe is installed in the application image, not the root CI type-check environment.
class GBOSDocument(Document):  # type: ignore[misc]
    def autoname(self) -> None:
        self.name = make_gbos_name(PREFIXES[self.doctype])

    def validate(self) -> None:
        self._validate_origin_boundary()
        self._protect_assigned_review_subject()
        self._validate_transition()
        self._advance_revision()

    def _validate_origin_boundary(self) -> None:
        if self.is_new() and self.origin == "AI" and self.review_status != "AI Draft":
            frappe.throw(
                "AI-origin records must be created as AI Draft",
                title="AI Draft boundary",
            )

    def _protect_assigned_review_subject(self) -> None:
        """Prevent an assigned Reviewer from changing the pinned subject.

        A user may hold both a business writer role and Reviewer.  Record
        permissions alone must not let that combination bypass the governed
        decision command while the assigned review remains pending.
        """
        if self.is_new() or self.doctype not in REVIEW_SUBJECT_DOCTYPES:
            return
        actor = frappe.session.user
        if "Reviewer" not in frappe.get_roles(actor):
            return
        if frappe.db.exists(
            "GBOS Review Case",
            {
                "assigned_reviewer": actor,
                "subject_doctype": self.doctype,
                "subject_name": self.name,
                "business_status": "Pending",
                "review_status": "Pending",
            },
        ):
            raise frappe.PermissionError

    def _validate_transition(self) -> None:
        workflow = WORKFLOWS.get(self.doctype)
        before = self.get_doc_before_save()
        if not workflow:
            return
        try:
            if not before:
                if self.flags.gbos_fixture_seed and self.origin == "Fixture":
                    return
                validate_initial_status(workflow, self.business_status)
                return
            validate_transition(
                workflow,
                before.business_status,
                self.business_status,
            )
        except InvalidTransition as error:
            frappe.throw(str(error), title="Invalid GBOS workflow transition")

    def _advance_revision(self) -> None:
        if self.is_new():
            self.revision = 1
            return
        current = int(
            frappe.db.get_value(self.doctype, self.name, "revision", for_update=True) or 0
        )
        try:
            self.revision = next_revision(
                expected=int(self.revision or 0),
                current=current,
            )
        except RevisionConflict as error:
            frappe.throw(str(error), title="Revision conflict")
