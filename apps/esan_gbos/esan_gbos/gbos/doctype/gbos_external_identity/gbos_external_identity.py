from __future__ import annotations

import hashlib
import re
from typing import Any

import frappe

from esan_gbos.gbos.doctype.base import GBOSDocument

IDENTITY_PROVIDERS = frozenset({"email", "wecom", "whatsapp", "phone", "manual_import"})
_OPAQUE = re.compile(r"[A-Za-z0-9_-]{43}")
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
_AUTHORITY_MAPPING_FIELDS = [
    "name",
    "revision",
    "team",
    "identity_provider",
    "external_subject",
]


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
    if _OPAQUE.fullmatch(opaque) is None:
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


def _active_authority_mappings(filters: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page_size = 500
    while True:
        page = frappe.get_all(
            "GBOS External Identity",
            filters={
                **filters,
                "review_status": "Approved",
                "business_status": "Active",
            },
            fields=_AUTHORITY_MAPPING_FIELDS,
            limit_start=len(rows),
            limit_page_length=page_size,
        )
        rows.extend(dict(row) for row in page)
        if len(page) < page_size:
            return rows


def _deny_mapping_authority(
    mapping: object,
    *,
    reason: str,
    mapping_revision: int | None = None,
) -> None:
    revision = (
        int(_value(mapping, "revision") or 0) if mapping_revision is None else mapping_revision
    )
    mapping_ref = str(_value(mapping, "name") or "")
    digest = hashlib.sha256(
        f"identity-authority-denial-v1\0{mapping_ref}\0{revision}\0{reason}".encode()
    ).hexdigest()
    idempotency_key = f"identity-authority-deny-{digest}"
    payload = {
        "identity_provider": _value(mapping, "identity_provider"),
        "external_subject_ref": _value(mapping, "external_subject"),
        "mapping_ref": mapping_ref,
        "team_ref": _value(mapping, "team"),
        "deny_through_revision": revision,
        "reason": reason,
        "idempotency_key": idempotency_key,
    }
    from esan_gbos.api.v4.gateway import call_local

    response = call_local(
        "Observer",
        method="POST",
        path="/internal/v1/identity-authority/deny",
        purpose="identity_authority",
        payload=payload,
        idempotency_key=idempotency_key,
    )
    if response != {
        "denial": {
            "mapping_ref": mapping_ref,
            "deny_through_revision": revision,
            "status": "denied",
        }
    }:
        frappe.throw(
            "Observer identity authority denial could not be verified",
            title="Identity authority unavailable",
        )


def deny_ineligible_user_mappings(doc: object, method: str | None = None) -> None:
    if method != "on_trash" and int(_value(doc, "enabled") or 0) == 1:
        return
    for mapping in _active_authority_mappings(
        {"identity_type": "User", "user": str(_value(doc, "name") or "")}
    ):
        _deny_mapping_authority(mapping, reason="target_ineligible")


def deny_ineligible_team_member_mappings(doc: object, method: str | None = None) -> None:
    memberships: set[tuple[str, str]] = set()
    parent = str(_value(doc, "parent") or "")
    user = str(_value(doc, "user") or "")
    if method == "on_trash" or int(_value(doc, "enabled") or 0) != 1:
        memberships.add((parent, user))
    before_getter = getattr(doc, "get_doc_before_save", None)
    before = before_getter() if callable(before_getter) else None
    if before is not None:
        previous = (str(_value(before, "parent") or ""), str(_value(before, "user") or ""))
        if int(_value(before, "enabled") or 0) == 1 and (
            previous != (parent, user) or int(_value(doc, "enabled") or 0) != 1
        ):
            memberships.add(previous)
    for team_ref, user_ref in sorted(memberships):
        for mapping in _active_authority_mappings(
            {"identity_type": "User", "user": user_ref, "team": team_ref}
        ):
            _deny_mapping_authority(mapping, reason="target_ineligible")


def _enabled_member_users(doc: object) -> set[str]:
    members = _value(doc, "members", ()) or ()
    return {
        str(_value(member, "user") or "")
        for member in members
        if int(_value(member, "enabled") or 0) == 1 and _value(member, "user")
    }


def deny_removed_team_member_mappings(doc: object, method: str | None = None) -> None:
    before_getter = getattr(doc, "get_doc_before_save", None)
    before = before_getter() if callable(before_getter) else None
    previous_members = _enabled_member_users(before or doc)
    current_members = set() if method == "on_trash" else _enabled_member_users(doc)
    team_ref = str(_value(doc, "name") or "")
    for user_ref in sorted(previous_members - current_members):
        for mapping in _active_authority_mappings(
            {"identity_type": "User", "user": user_ref, "team": team_ref}
        ):
            _deny_mapping_authority(mapping, reason="target_ineligible")


def deny_ineligible_party_mappings(doc: object, method: str | None = None) -> None:
    party_ref = str(_value(doc, "name") or "")
    current_team = str(_value(doc, "team") or "")
    for mapping in _active_authority_mappings(
        {"identity_type": "Party", "party_profile": party_ref}
    ):
        if method == "on_trash" or str(mapping.get("team") or "") != current_team:
            _deny_mapping_authority(mapping, reason="target_ineligible")


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
        self._deny_observer_self_access_before_authority_loss()

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

        requires_live_target = self.business_status not in {"Revoked", "Archived"} and (
            self.review_status not in {"Rejected", "Superseded"}
        )
        if (
            requires_live_target
            and identity_type == "User"
            and (
                int(frappe.db.get_value("User", user, "enabled") or 0) != 1
                or not frappe.db.exists(
                    "GBOS Team Member",
                    {"parent": self.team, "user": user, "enabled": 1},
                )
            )
        ):
            frappe.throw(
                "identity target must be enabled and belong to the same team",
                title="Invalid target",
            )
        if (
            requires_live_target
            and identity_type == "Party"
            and (frappe.db.get_value("GBOS Party Profile", party_profile, "team") != self.team)
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
        reopen_command = bool(getattr(self.flags, "gbos_ai_reopen_command", False))
        review_command = bool(
            getattr(self.flags, "gbos_identity_review_decision", False)
            or getattr(self.flags, "gbos_ai_draft_command", False)
            or reopen_command
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
        if reopen_command and not (
            _value(before, "origin") == "AI"
            and self.origin == "AI"
            and _value(before, "review_status") == "Rejected"
            and _value(before, "business_status") == "Active"
            and self.review_status == "AI Draft"
            and self.business_status == "Active"
            and self.team == _value(before, "team")
            and self.identity_provider == _value(before, "identity_provider")
            and self.external_subject == _value(before, "external_subject")
        ):
            raise frappe.PermissionError
        if getattr(
            self.flags, "gbos_identity_review_decision", False
        ) and self.review_status not in {"Approved", "Rejected", "Superseded"}:
            raise frappe.PermissionError

        if (
            _value(before, "review_status") == "Rejected"
            and _value(before, "business_status") == "Active"
            and not reopen_command
        ):
            for fieldname in (
                "team",
                "identity_provider",
                "external_subject",
                "identity_type",
                "user",
                "party_profile",
                "origin_reference",
                "last_request_id",
            ):
                if self.get(fieldname) != _value(before, fieldname):
                    raise frappe.PermissionError

        if is_authoritative_mapping(self) and self.review_status != "Approved":
            raise frappe.PermissionError

        if is_authoritative_mapping(before):
            for fieldname in _TARGET_FIELDS + ("identity_provider", "external_subject"):
                if self.get(fieldname) != _value(before, fieldname):
                    raise frappe.PermissionError

    def _deny_observer_self_access_before_authority_loss(self) -> None:
        before = self.get_doc_before_save()
        if before is None:
            return
        reason: str | None = None
        if self.business_status == "Revoked" and _value(before, "business_status") != "Revoked":
            reason = "revoked"
        elif self.review_status == "Superseded" and _value(before, "review_status") != "Superseded":
            reason = "superseded"
        if reason is None:
            return

        _deny_mapping_authority(
            self,
            reason=reason,
            mapping_revision=int(_value(before, "revision") or 0),
        )


__all__ = [
    "GBOSExternalIdentity",
    "IDENTITY_PROVIDERS",
    "deny_ineligible_party_mappings",
    "deny_ineligible_team_member_mappings",
    "deny_ineligible_user_mappings",
    "deny_removed_team_member_mappings",
    "is_authoritative_mapping",
    "review_state_for_decision",
    "validate_external_subject",
]
