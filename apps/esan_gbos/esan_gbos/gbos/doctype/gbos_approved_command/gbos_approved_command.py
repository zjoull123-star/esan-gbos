from __future__ import annotations

import frappe
from frappe.model.document import Document

from esan_gbos.domain.approved_command import (
    ApprovedCommandValidationError,
    validate_email_send_approved_command,
)
from esan_gbos.domain.naming import make_gbos_name


class GBOSApprovedCommand(Document):
    def autoname(self) -> None:
        self.name = make_gbos_name("CMD")

    def validate(self) -> None:
        if self.get_doc_before_save():
            raise frappe.PermissionError
        try:
            command = validate_email_send_approved_command(frappe.parse_json(self.command_payload))
        except ApprovedCommandValidationError as error:
            frappe.throw(str(error), title="Invalid approved command")
        if command["command_id"] != self.name or command["payload_sha256"] != self.payload_sha256:
            frappe.throw("Approved command identity does not match payload")

    def on_trash(self) -> None:
        raise frappe.PermissionError
