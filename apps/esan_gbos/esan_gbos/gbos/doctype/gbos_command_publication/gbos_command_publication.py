from __future__ import annotations

import frappe
from frappe.model.document import Document

from esan_gbos.domain.approved_command import (
    ApprovedCommandValidationError,
    validate_email_send_approved_command,
)
from esan_gbos.domain.naming import make_gbos_name

_IMMUTABLE = ("approved_command", "command_payload", "payload_digest", "max_attempts")


class GBOSCommandPublication(Document):
    def autoname(self) -> None:
        self.name = make_gbos_name("PUB")

    def validate(self) -> None:
        before = self.get_doc_before_save()
        if before and any(self.get(field) != before.get(field) for field in _IMMUTABLE):
            raise frappe.PermissionError
        if before and not self.flags.gbos_publication_worker:
            raise frappe.PermissionError
        try:
            command = validate_email_send_approved_command(frappe.parse_json(self.command_payload))
        except ApprovedCommandValidationError as error:
            frappe.throw(str(error), title="Invalid command publication")
        if (
            command["command_id"] != self.approved_command
            or "sha256:" + command["payload_sha256"] != self.payload_digest
        ):
            frappe.throw("Command publication identity does not match payload")

    def on_trash(self) -> None:
        raise frappe.PermissionError
