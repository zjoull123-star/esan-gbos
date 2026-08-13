from __future__ import annotations

import json
from typing import Any

import frappe

from esan_gbos.domain.email_review_policy import (
    EmailSendReviewPolicyError,
    validate_email_send_approval_snapshot,
)
from esan_gbos.domain.naming import make_gbos_name
from esan_gbos.domain.review_dto import canonical_payload_hash
from esan_gbos.gbos.doctype.base import GBOSDocument

_IMMUTABLE_FIELDS = (
    "site_id",
    "processing_purpose",
    "team",
    "assignee_user_ref",
    "approval_expires_at",
    "mailbox_ref",
    "mailbox_config_revision",
    "inbox_item_ref",
    "inbox_item_revision",
    "conversation_ref",
    "conversation_revision",
    "reply_draft_ref",
    "reply_draft_revision",
    "reply_draft_digest",
    "participants",
    "party_ref",
    "party_revision",
    "team_revision",
    "owner_user_ref",
    "owner_eligibility_revision",
    "final_mime_evidence_ref",
    "final_mime_digest",
    "evidence_refs",
    "stable_client_request_id",
    "payload_sha256",
    "origin",
    "origin_reference",
)


def approval_snapshot(doc: Any) -> dict[str, Any]:
    def value(field: str) -> Any:
        result = doc.get(field) if hasattr(doc, "get") else getattr(doc, field)
        if field in {"participants", "evidence_refs"} and isinstance(result, str):
            return frappe.parse_json(result)
        return result

    return {
        "schema_version": "1.0",
        "site_id": value("site_id"),
        "processing_purpose": value("processing_purpose"),
        "team_ref": value("team"),
        "assignee_user_ref": value("assignee_user_ref"),
        "approval_expires_at": value("approval_expires_at"),
        "mailbox_ref": value("mailbox_ref"),
        "mailbox_config_revision": value("mailbox_config_revision"),
        "inbox_item_ref": value("inbox_item_ref"),
        "inbox_item_revision": value("inbox_item_revision"),
        "conversation_ref": value("conversation_ref"),
        "conversation_revision": value("conversation_revision"),
        "reply_draft_ref": value("reply_draft_ref"),
        "reply_draft_revision": value("reply_draft_revision"),
        "reply_draft_digest": value("reply_draft_digest"),
        "participants": value("participants"),
        "party_ref": value("party_ref"),
        "party_revision": value("party_revision"),
        "team_revision": value("team_revision"),
        "owner_user_ref": value("owner_user_ref"),
        "owner_eligibility_revision": value("owner_eligibility_revision"),
        "final_mime_evidence_ref": value("final_mime_evidence_ref"),
        "final_mime_digest": value("final_mime_digest"),
        "evidence_refs": value("evidence_refs"),
        "stable_client_request_id": value("stable_client_request_id"),
    }


class GBOSEmailSendApproval(GBOSDocument):
    def autoname(self) -> None:
        self.name = make_gbos_name("ESA")

    def validate(self) -> None:
        before = self.get_doc_before_save()
        if before:
            changed = any(self.get(field) != before.get(field) for field in _IMMUTABLE_FIELDS)
            status_changed = (
                self.business_status != before.business_status
                or self.review_status != before.review_status
            )
            if changed or (status_changed and not self.flags.gbos_email_send_decision):
                raise frappe.PermissionError
        try:
            snapshot = validate_email_send_approval_snapshot(approval_snapshot(self))
        except EmailSendReviewPolicyError as error:
            frappe.throw(str(error), title="Invalid email send approval")
        expected = canonical_payload_hash(snapshot)
        if self.is_new() and not self.payload_sha256:
            self.payload_sha256 = expected
        if self.payload_sha256 != expected:
            frappe.throw(
                "payload_sha256 does not match approval", title="Invalid email send approval"
            )
        self.participants = json.dumps(
            snapshot["participants"], separators=(",", ":"), sort_keys=True
        )
        self.evidence_refs = json.dumps(
            snapshot["evidence_refs"], separators=(",", ":"), sort_keys=True
        )
        super().validate()

    def on_trash(self) -> None:
        raise frappe.PermissionError


__all__ = ["GBOSEmailSendApproval", "approval_snapshot"]
