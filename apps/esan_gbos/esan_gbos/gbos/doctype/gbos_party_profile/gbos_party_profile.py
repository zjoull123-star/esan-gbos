from __future__ import annotations

import frappe

from esan_gbos.domain.external_identity_projection import owner_eligibility_revision
from esan_gbos.gbos.doctype.base import GBOSDocument

CRM_TEAM_LINKS = {
    "crm_organization": "CRM Organization",
    "contact": "Contact",
    "crm_lead": "CRM Lead",
    "crm_deal": "CRM Deal",
}


class GBOSPartyProfile(GBOSDocument):
    def validate(self) -> None:
        super().validate()
        self._validate_owner()
        for fieldname, doctype in CRM_TEAM_LINKS.items():
            name = self.get(fieldname)
            if not name:
                continue
            linked_team = frappe.db.get_value(
                doctype,
                name,
                "custom_esan_team",
            )
            if not linked_team or linked_team != self.team:
                frappe.throw(
                    f"{doctype} link must belong to the Party Profile team",
                    title="Invalid cross-team link",
                )

    def _validate_owner(self) -> None:
        owner = self.get("owner_user")
        if not owner:
            return
        user = frappe.db.get_value(
            "User",
            owner,
            ["enabled", "user_type"],
            as_dict=True,
        )
        if (
            not user
            or int(user.get("enabled") or 0) != 1
            or user.get("user_type") != "System User"
            or not frappe.db.exists(
                "GBOS Team Member",
                {"parent": self.team, "user": owner, "enabled": 1},
            )
        ):
            frappe.throw(
                "Party owner must be an enabled System User in the exact Party team",
                title="Invalid Party owner",
            )


__all__ = ["GBOSPartyProfile", "owner_eligibility_revision"]
