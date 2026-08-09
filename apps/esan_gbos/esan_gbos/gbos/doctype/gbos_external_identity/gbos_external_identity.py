from __future__ import annotations

import re
from typing import Any

import frappe

from esan_gbos.gbos.doctype.base import GBOSDocument

IDENTITY_PROVIDERS = frozenset({"email", "wecom", "whatsapp", "phone", "manual_import"})
_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,127}")
_RAW_EMAIL = re.compile(r"[^@\s]+@[^@\s]+")
_RAW_PHONE = re.compile(r"\+?[0-9][0-9 ()-]{7,}[0-9]")
_TARGET_FIELDS = ("team", "identity_type", "user", "party_profile")
_DB_SET_PROTECTED_FIELDS = frozenset(
    {
        *_TARGET_FIELDS,
        "identity_provider",
        "external_subject",
        "origin",
        "business_status",
        "review_status",
        "revision",
    }
)


def validate_external_subject(provider: object, external_subject: object) -> None:
    """Validate the opaque provider-scoped reference without echoing it on refusal."""
    if not isinstance(provider, str) or provider not in IDENTITY_PROVIDERS:
        raise ValueError("identity provider is not allowed")
    if not isinstance(external_subject, str) or len(external_subject) > 160:
        raise ValueError("external subject reference is invalid")
    prefix = f"extid:v1:{provider}:"
    if not external_subject.startswith(prefix):
        raise ValueError("external subject reference is invalid")
    opaque = external_subject[len(prefix) :]
    if (
        _OPAQUE.fullmatch(opaque) is None
        or _RAW_EMAIL.fullmatch(opaque) is not None
        or _RAW_PHONE.fullmatch(opaque) is not None
    ):
        raise ValueError("external subject reference is invalid")


def is_authoritative_mapping(mapping: object) -> bool:
    """Return whether a mapping can be used for resolution or access decisions."""
    return bool(
        _value(mapping, "review_status") == "Approved"
        and _value(mapping, "business_status") == "Active"
    )


def review_state_for_decision(decision: object) -> tuple[str, str]:
    """Map a governed Review Case outcome to the only permitted identity state."""
    states = {
        "Approved": ("Approved", "Active"),
        "Rejected": ("Rejected", "Active"),
        "Superseded": ("Superseded", "Archived"),
    }
    try:
        return states[str(decision)]
    except KeyError as error:
        raise ValueError("review decision is not allowed") from error


def _value(source: object, fieldname: str, default: Any = None) -> Any:
    if isinstance(source, dict):
        return source.get(fieldname, default)
    return getattr(source, fieldname, default)


class GBOSExternalIdentity(GBOSDocument):
    def on_trash(self) -> None:
        raise frappe.PermissionError

    def db_set(
        self,
        fieldname: str | dict[str, Any],
        value: Any = None,
        commit: bool = False,
        update_modified: bool = True,
        notify: bool = False,
    ) -> None:
        fields = set(fieldname) if isinstance(fieldname, dict) else {fieldname}
        if fields & _DB_SET_PROTECTED_FIELDS:
            raise frappe.PermissionError
        super().db_set(
            fieldname,
            value,
            commit=commit,
            update_modified=update_modified,
            notify=notify,
        )

    def validate(self) -> None:
        self._validate_subject_reference()
        self._validate_target()
        self._validate_duplicate()
        self._protect_governed_state()
        super().validate()

    def _validate_subject_reference(self) -> None:
        try:
            validate_external_subject(self.identity_provider, self.external_subject)
        except ValueError as error:
            frappe.throw(str(error), title="Invalid external identity")

    def _validate_target(self) -> None:
        identity_type = self.identity_type
        user = self.get("user")
        party_profile = self.get("party_profile")
        if identity_type == "User":
            valid_target = bool(user) and not party_profile
        elif identity_type == "Party":
            valid_target = bool(party_profile) and not user
        elif identity_type == "Channel":
            valid_target = not user and not party_profile
        else:
            valid_target = False
        if not valid_target:
            frappe.throw("identity target does not match its closed type", title="Invalid target")

        if identity_type == "User" and (
            int(frappe.db.get_value("User", user, "enabled") or 0) != 1
            or not frappe.db.exists(
                "GBOS Team Member",
                {"parent": self.team, "user": user, "enabled": 1},
            )
        ):
            frappe.throw(
                "identity target must be enabled and belong to the same team",
                title="Invalid target",
            )
        if identity_type == "Party" and (
            frappe.db.get_value("GBOS Party Profile", party_profile, "team") != self.team
        ):
            frappe.throw(
                "identity target must belong to the same team",
                title="Invalid target",
            )

    def _validate_duplicate(self) -> None:
        filters: dict[str, Any] = {
            "identity_provider": self.identity_provider,
            "external_subject": self.external_subject,
        }
        if self.get("name"):
            filters["name"] = ["!=", self.name]
        if frappe.db.exists("GBOS External Identity", filters):
            frappe.throw(
                "an external identity mapping already exists",
                title="Duplicate external identity",
            )

    def _protect_governed_state(self) -> None:
        if self.is_new():
            if self.origin == "AI" and self.review_status != "AI Draft":
                frappe.throw(
                    "AI-origin records must be created as AI Draft",
                    title="AI Draft boundary",
                )
            if self.origin != "AI" and self.review_status != "Pending":
                raise frappe.PermissionError
            return

        before = self.get_doc_before_save()
        if before is None:
            raise frappe.PermissionError
        review_changed = self.review_status != _value(before, "review_status")
        business_changed = self.business_status != _value(before, "business_status")
        review_command = bool(
            getattr(self.flags, "gbos_identity_review_decision", False)
            or getattr(self.flags, "gbos_ai_draft_command", False)
        )
        status_command = bool(
            review_command or getattr(self.flags, "gbos_identity_status_command", False)
        )
        if review_changed and not review_command:
            raise frappe.PermissionError
        if business_changed and not status_command:
            raise frappe.PermissionError

        if getattr(self.flags, "gbos_ai_draft_command", False) and not (
            _value(before, "origin") == "AI"
            and _value(before, "review_status") == "AI Draft"
            and self.review_status == "Pending"
            and not business_changed
        ):
            raise frappe.PermissionError
        if getattr(
            self.flags, "gbos_identity_review_decision", False
        ) and self.review_status not in {"Approved", "Rejected", "Superseded"}:
            raise frappe.PermissionError

        if is_authoritative_mapping(self) and self.review_status != "Approved":
            raise frappe.PermissionError

        if is_authoritative_mapping(before):
            for fieldname in _TARGET_FIELDS + ("identity_provider", "external_subject"):
                if self.get(fieldname) != _value(before, fieldname):
                    raise frappe.PermissionError


__all__ = [
    "GBOSExternalIdentity",
    "IDENTITY_PROVIDERS",
    "is_authoritative_mapping",
    "review_state_for_decision",
    "validate_external_subject",
]
