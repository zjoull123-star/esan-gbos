from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from esan_gbos.domain.external_identity_projection import owner_eligibility_revision


class TestGBOSPartyProfileOwner(FrappeTestCase):
    def test_owner_field_is_nullable_link_to_user(self) -> None:
        field = frappe.get_meta("GBOS Party Profile").get_field("owner_user")

        self.assertIsNotNone(field)
        self.assertEqual(field.fieldtype, "Link")
        self.assertEqual(field.options, "User")
        self.assertEqual(field.reqd, 0)

    def test_gateway_authority_role_has_no_standard_or_custom_docperm(self) -> None:
        role = "Email Gateway Authority Consumer"

        self.assertFalse(frappe.get_all("Custom DocPerm", filters={"role": role}, pluck="name"))
        for doctype in (
            "GBOS Team",
            "GBOS Party Profile",
            "GBOS External Identity",
            "GBOS Review Case",
        ):
            self.assertFalse(
                [
                    permission
                    for permission in frappe.get_meta(doctype).permissions
                    if permission.role == role
                ]
            )

    def test_owner_revision_is_stable_for_same_live_authority_state(self) -> None:
        party = frappe._dict(
            name="PTY-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            revision=2,
            team="TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            owner_user="owner@example.invalid",
        )
        state = {
            "owner_enabled": 1,
            "owner_user_type": "System User",
            "membership_ref": "TM-0001",
            "membership_parent": party.team,
            "membership_user": party.owner_user,
            "membership_enabled": 1,
            "membership_modified": "2026-08-13T00:00:00Z",
            "team_revision": 3,
        }

        first = owner_eligibility_revision(party, state)
        second = owner_eligibility_revision(party, dict(reversed(list(state.items()))))

        self.assertRegex(first, r"^sha256:[a-f0-9]{64}$")
        self.assertEqual(first, second)
