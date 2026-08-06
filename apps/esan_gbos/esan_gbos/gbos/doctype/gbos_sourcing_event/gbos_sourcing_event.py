import frappe

from esan_gbos.gbos.doctype.base import GBOSDocument


class GBOSSourcingEvent(GBOSDocument):
    def validate(self) -> None:
        self._validate_supplier_selection()
        super().validate()

    def _validate_supplier_selection(self) -> None:
        selected_rows = [
            row for row in self.get("candidates") or [] if row.get("candidate_status") == "Selected"
        ]
        is_selected_state = self.business_status in {"Selected", "Closed"}

        if is_selected_state:
            if not self.selected_supplier:
                frappe.throw(
                    "Selected sourcing events require a selected supplier",
                    title="Supplier selection",
                )
            if (
                len(selected_rows) != 1
                or selected_rows[0].get("supplier_name") != self.selected_supplier
            ):
                frappe.throw(
                    "Exactly one candidate must match the selected supplier",
                    title="Supplier selection",
                )
        elif self.selected_supplier or selected_rows:
            frappe.throw(
                "Supplier selection is only valid in Selected or Closed status",
                title="Supplier selection",
            )

        if not is_selected_state or (self.flags.gbos_fixture_seed and self.origin == "Fixture"):
            return
        if not set(frappe.get_roles()) & {"GBOS Admin", "Purchase Manager"}:
            frappe.throw(
                "Final supplier selection requires Purchase Manager confirmation",
                exc=frappe.PermissionError,
                title="Supplier selection",
            )
