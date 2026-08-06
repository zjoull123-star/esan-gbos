import frappe

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
