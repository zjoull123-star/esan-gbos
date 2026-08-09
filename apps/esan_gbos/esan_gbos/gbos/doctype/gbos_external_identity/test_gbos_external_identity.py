from __future__ import annotations

from typing import Any

import frappe
from frappe.auth import validate_auth
from frappe.tests import IntegrationTestCase
from werkzeug.wrappers import Request

from esan_gbos.api.internal import identity_resolution as identity_resolution_api
from esan_gbos.api.v2.review_case import decide
from esan_gbos.domain.permissions import IDENTITY_RESOLVER_ROLE
from esan_gbos.domain.review_dto import canonical_payload_hash
from esan_gbos.gbos.doctype.gbos_review_case.gbos_review_case import (
    build_case_payload,
    build_subject_snapshot,
)
from esan_gbos.identity_resolver_access import identity_resolution_scope_active
from esan_gbos.install import ensure_permissions, ensure_roles

TEST_MEMBER = "gbos-identity-member@example.invalid"
TEST_REVIEWER = "gbos-identity-reviewer@example.invalid"
TEST_INTEGRATION_ADMIN = "gbos-identity-admin@example.invalid"
RESOLVER_USER = "gbos-identity-resolver@localhost.invalid"
OTHER_RESOLVER_USER = "gbos-other-identity-resolver@example.invalid"
RESOLVER_MEMBER = "gbos-identity-resolver-member@example.invalid"
RESOLVER_AUTH_REF = "observer-identity-resolver-v1"
RESOLVER_API_KEY = "ResolverKey_0123456789ABCDEF"
RESOLVER_API_SECRET = "ResolverSecret_0123456789ABCDEF"
OTHER_RESOLVER_API_KEY = "OtherResolverKey_0123456789ABCDEF"
OTHER_RESOLVER_API_SECRET = "OtherResolverSecret_0123456789ABCDEF"
RESOLVER_PURPOSE = "identity_resolution"
RESOLVER_CONFIG_KEY = "gbos_identity_resolver_identities"
RESOLVER_SCOPE_ATTRIBUTE = "_gbos_identity_resolution_scope"
_MISSING = object()

IGNORE_TEST_RECORD_DEPENDENCIES = [
    "DocType",
    "GBOS Review Decision",
    "GBOS Team",
    "User",
]


class TestGBOSExternalIdentityAuthority(IntegrationTestCase):
    def setUp(self) -> None:
        frappe.set_user("Administrator")
        self.member = self._user(TEST_MEMBER, "Sales User")
        self.reviewer = self._user(TEST_REVIEWER, "Reviewer")
        self.integration_admin = self._user(TEST_INTEGRATION_ADMIN, "Integration Admin")
        self.team = frappe.get_doc(
            {
                "doctype": "GBOS Team",
                "team_name": f"Identity authority {frappe.generate_hash(length=8)}",
                "members": [
                    {"user": self.member, "team_role": "Member", "enabled": 1},
                    {"user": self.reviewer, "team_role": "Reviewer", "enabled": 1},
                ],
            }
        ).insert(ignore_permissions=True)

    def tearDown(self) -> None:
        frappe.set_user("Administrator")

    def _user(self, email: str, *roles: str) -> str:
        if not frappe.db.exists("User", email):
            frappe.get_doc(
                {
                    "doctype": "User",
                    "email": email,
                    "first_name": email.split("@", 1)[0],
                    "send_welcome_email": 0,
                }
            ).insert(ignore_permissions=True)
        user = frappe.get_doc("User", email)
        for role in roles:
            if role not in frappe.get_roles(email):
                user.add_roles(role)
        return email

    def _identity(self, **overrides: Any) -> Any:
        values: dict[str, Any] = {
            "doctype": "GBOS External Identity",
            "team": self.team.name,
            "identity_provider": "email",
            "external_subject": f"extid:v1:email:{frappe.generate_hash(length=24)}",
            "identity_type": "User",
            "user": self.member,
            "origin": "Manual",
            "business_status": "Active",
            "review_status": "Pending",
        }
        values.update(overrides)
        return frappe.get_doc(values).insert(ignore_permissions=True)

    def _case(self, identity: Any) -> Any:
        snapshot = build_subject_snapshot(identity)
        values: dict[str, Any] = {
            "doctype": "GBOS Review Case",
            "title": "Review opaque external identity mapping",
            "team": self.team.name,
            "assigned_reviewer": self.reviewer,
            "subject_doctype": identity.doctype,
            "subject_name": identity.name,
            "subject_revision": identity.revision,
            "subject_payload_sha256": canonical_payload_hash(snapshot),
            "subject_snapshot": frappe.as_json(snapshot),
            "evidence_refs": frappe.as_json(["OBS-IDENTITY-01"]),
            "policy_version": "identity-resolution/v1",
            "business_status": "Pending",
            "review_status": "Pending",
        }
        values["case_payload_sha256"] = canonical_payload_hash(build_case_payload(values))
        return frappe.get_doc(values).insert(ignore_permissions=True)

    def _decide(self, case: Any, decision: str) -> dict[str, Any]:
        frappe.local.gbos_request_id = None
        return decide(
            name=case.name,
            decision=decision,
            decision_note="The pinned evidence supports this governed outcome.",
            expected_revision=case.revision,
            expected_subject_revision=case.subject_revision,
            idempotency_key=f"identity-review-{case.name}-{decision}",
            subject_payload_sha256=case.subject_payload_sha256,
            evidence_refs=frappe.parse_json(case.evidence_refs),
            policy_version=case.policy_version,
            expected_case_payload_hash=case.case_payload_sha256,
        )

    def test_native_insert_rejects_raw_subject_wrong_target_and_duplicate(self) -> None:
        with self.assertRaises(frappe.ValidationError):
            self._identity(external_subject="raw-person@example.invalid")
        with self.assertRaises(frappe.ValidationError):
            self._identity(identity_type="Channel", user=self.member)

        identity = self._identity()
        with self.assertRaises(frappe.ValidationError):
            self._identity(
                identity_provider=identity.identity_provider,
                external_subject=identity.external_subject,
            )

    def test_ai_cannot_create_approved_mapping_or_use_generic_set_value(self) -> None:
        with self.assertRaises(frappe.ValidationError):
            self._identity(origin="AI", review_status="Approved")

        identity = self._identity()
        frappe.set_user(self.integration_admin)
        with self.assertRaises(frappe.PermissionError):
            frappe.get_doc(identity.doctype, identity.name).db_set(
                "review_status",
                "Approved",
            )

    def test_review_approval_activates_and_rejection_remains_non_authoritative(self) -> None:
        approved = self._identity()
        rejected = self._identity()
        approved_case = self._case(approved)
        rejected_case = self._case(rejected)
        frappe.set_user(self.reviewer)

        approved_result = self._decide(approved_case, "Approved")
        rejected_result = self._decide(rejected_case, "Rejected")

        self.assertEqual(approved_result["data"]["case"]["business_status"], "Approved")
        self.assertEqual(rejected_result["data"]["case"]["business_status"], "Rejected")
        approved = frappe.get_doc(approved.doctype, approved.name)
        rejected = frappe.get_doc(rejected.doctype, rejected.name)
        self.assertEqual((approved.review_status, approved.business_status), ("Approved", "Active"))
        self.assertEqual((rejected.review_status, rejected.business_status), ("Rejected", "Active"))

    def test_stale_mapping_revision_stops_review_decision(self) -> None:
        identity = self._identity()
        case = self._case(identity)
        frappe.set_user("Administrator")
        identity = frappe.get_doc(identity.doctype, identity.name)
        identity.origin_reference = "new-proposal-reference"
        identity.save(ignore_permissions=True)
        frappe.set_user(self.reviewer)

        result = self._decide(case, "Approved")

        self.assertEqual(result["error"]["code"], "revision_conflict")
        self.assertEqual(result["error"]["details"]["conflict"], "subject_revision")

    def test_governed_supersession_archives_mapping(self) -> None:
        identity = self._identity()
        case = self._case(identity)
        case.flags.gbos_review_command = True
        case.business_status = "Superseded"
        case.review_status = "Superseded"

        case.save(ignore_permissions=True)

        identity = frappe.get_doc(identity.doctype, identity.name)
        self.assertEqual(identity.review_status, "Superseded")
        self.assertEqual(identity.business_status, "Archived")

    def test_physical_delete_is_denied_for_every_mapping_lifecycle(self) -> None:
        identity = self._identity()
        subject_ref = identity.external_subject
        target_ref = identity.user

        for review_status in ("AI Draft", "Pending", "Approved", "Rejected", "Superseded"):
            for business_status in ("Active", "Revoked", "Archived"):
                frappe.db.set_value(
                    identity.doctype,
                    identity.name,
                    {
                        "review_status": review_status,
                        "business_status": business_status,
                    },
                    update_modified=False,
                )
                with self.assertRaises(frappe.PermissionError) as error:
                    frappe.delete_doc(
                        identity.doctype,
                        identity.name,
                        ignore_permissions=True,
                    )
                self.assertNotIn(subject_ref, repr(error.exception))
                self.assertNotIn(target_ref, repr(error.exception))
                self.assertTrue(frappe.db.exists(identity.doctype, identity.name))


class TestObserverIdentityResolverNativeBoundary(IntegrationTestCase):
    def setUp(self) -> None:
        frappe.set_user("Administrator")
        ensure_roles()
        ensure_permissions()
        self._previous_identity_config = frappe.conf.get(RESOLVER_CONFIG_KEY, _MISSING)
        self._previous_request = getattr(frappe.local, "request", _MISSING)
        self._previous_response = getattr(frappe.local, "response", _MISSING)
        self.site_id = str(frappe.local.site)
        frappe.conf[RESOLVER_CONFIG_KEY] = {
            RESOLVER_AUTH_REF: {
                "user": RESOLVER_USER,
                "site_id": self.site_id,
                "processing_purposes": [RESOLVER_PURPOSE],
            }
        }
        self._user(RESOLVER_MEMBER)
        self._service_user()
        self.team = frappe.get_doc(
            {
                "doctype": "GBOS Team",
                "team_name": f"Identity resolver native {frappe.generate_hash(length=8)}",
                "members": [
                    {
                        "user": RESOLVER_MEMBER,
                        "team_role": "Member",
                        "enabled": 1,
                    }
                ],
            }
        ).insert(ignore_permissions=True)
        frappe.clear_cache()

    def tearDown(self) -> None:
        frappe.set_user("Administrator")
        if hasattr(frappe.local, RESOLVER_SCOPE_ATTRIBUTE):
            delattr(frappe.local, RESOLVER_SCOPE_ATTRIBUTE)
        self._restore_local("request", self._previous_request)
        self._restore_local("response", self._previous_response)
        if self._previous_identity_config is _MISSING:
            frappe.conf.pop(RESOLVER_CONFIG_KEY, None)
        else:
            frappe.conf[RESOLVER_CONFIG_KEY] = self._previous_identity_config
        frappe.clear_cache()

    def _restore_local(self, attribute: str, value: object) -> None:
        if value is _MISSING:
            if hasattr(frappe.local, attribute):
                delattr(frappe.local, attribute)
            return
        setattr(frappe.local, attribute, value)

    def _user(self, email: str) -> Any:
        if not frappe.db.exists("User", email):
            frappe.get_doc(
                {
                    "doctype": "User",
                    "email": email,
                    "first_name": email.split("@", 1)[0],
                    "enabled": 1,
                    "send_welcome_email": 0,
                }
            ).insert(ignore_permissions=True)
        user = frappe.get_doc("User", email)
        user.enabled = 1
        user.save(ignore_permissions=True)
        return user

    def _service_user(
        self,
        *,
        email: str = RESOLVER_USER,
        api_key: str = RESOLVER_API_KEY,
        api_secret: str = RESOLVER_API_SECRET,
    ) -> Any:
        if not frappe.db.exists("User", email):
            frappe.get_doc(
                {
                    "doctype": "User",
                    "email": email,
                    "first_name": "GBOS Identity Resolver Service",
                    "enabled": 1,
                    "send_welcome_email": 0,
                }
            ).insert(ignore_permissions=True)
        user = frappe.get_doc("User", email)
        user.enabled = 1
        user.user_type = "Website User"
        user.send_welcome_email = 0
        user.role_profile_name = None
        user.set("role_profiles", [])
        user.set("roles", [])
        user.append("roles", {"role": IDENTITY_RESOLVER_ROLE})
        user.api_key = api_key
        user.api_secret = api_secret
        user.save(ignore_permissions=True)
        frappe.clear_cache(user=email)
        return user

    def _identity(self, subject: str, *, business_status: str = "Active") -> Any:
        identity = frappe.get_doc(
            {
                "doctype": "GBOS External Identity",
                "team": self.team.name,
                "identity_provider": "email",
                "external_subject": subject,
                "identity_type": "User",
                "user": RESOLVER_MEMBER,
                "origin": "Manual",
                "business_status": "Active",
                "review_status": "Pending",
            }
        ).insert(ignore_permissions=True)
        frappe.db.set_value(
            identity.doctype,
            identity.name,
            {
                "review_status": "Approved",
                "business_status": business_status,
            },
        )
        return frappe.get_doc(identity.doctype, identity.name)

    def _headers(self, **overrides: str) -> dict[str, str]:
        headers = {
            "Authorization": f"token {RESOLVER_API_KEY}:{RESOLVER_API_SECRET}",
            "X-Site-ID": self.site_id,
            "X-Processing-Purpose": RESOLVER_PURPOSE,
            "X-Request-ID": "native-resolution-0001",
            "X-GBOS-Frappe-Auth-Ref": RESOLVER_AUTH_REF,
        }
        headers.update(overrides)
        return headers

    def _payload(self, subject: str, **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "site_id": self.site_id,
            "processing_purpose": RESOLVER_PURPOSE,
            "request_id": "native-resolution-0001",
            "auth_ref": RESOLVER_AUTH_REF,
            "lookups": [
                {
                    "identity_provider": "email",
                    "external_subject_ref": subject,
                    "expected_team_ref": self.team.name,
                    "expected_mapping_revision": 1,
                }
            ],
        }
        payload.update(overrides)
        return payload

    def _authenticate(
        self,
        headers: dict[str, str],
        *,
        expected_user: str = RESOLVER_USER,
    ) -> None:
        frappe.set_user("Guest")
        frappe.local.response = frappe._dict()
        frappe.local.request = Request.from_values(method="POST", headers=headers)
        validate_auth()
        self.assertEqual(frappe.session.user, expected_user)

    def _resolve(
        self,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self._authenticate(headers or self._headers())
        return identity_resolution_api.resolve(payload)

    def test_service_identity_is_enabled_no_desk_and_has_no_doctype_grants(self) -> None:
        user = frappe.get_doc("User", RESOLVER_USER)
        assigned_roles = {row.role for row in user.roles}
        permission_fields = (
            "read",
            "write",
            "create",
            "delete",
            "report",
            "export",
            "print",
            "email",
            "share",
        )
        standard_docperm_rows = frappe.get_all(
            "DocPerm",
            filters={"role": IDENTITY_RESOLVER_ROLE},
            fields=["parent", *permission_fields],
        )
        custom_docperm_rows = frappe.get_all(
            "Custom DocPerm",
            filters={"role": IDENTITY_RESOLVER_ROLE},
            fields=["parent", *permission_fields],
        )

        self.assertEqual(user.enabled, 1)
        self.assertEqual(user.user_type, "Website User")
        self.assertEqual(assigned_roles, {IDENTITY_RESOLVER_ROLE})
        self.assertFalse(user.get("role_profiles"))
        self.assertEqual(
            frappe.db.get_value("Role", IDENTITY_RESOLVER_ROLE, "desk_access"),
            0,
        )
        self.assertTrue(custom_docperm_rows)
        for row in [*standard_docperm_rows, *custom_docperm_rows]:
            self.assertTrue(
                all(int(row.get(fieldname) or 0) == 0 for fieldname in permission_fields),
                msg=f"unexpected service DocPerm grant on {row.parent}",
            )

    def test_frappe_api_token_authentication_is_exact_and_requires_enabled_user(self) -> None:
        self._authenticate(self._headers())

        wrong_secret_headers = self._headers()
        wrong_secret_headers["Authorization"] = (
            f"token {RESOLVER_API_KEY}:WrongSecret_0123456789ABCDEF"
        )
        with self.assertRaises(frappe.AuthenticationError):
            self._authenticate(wrong_secret_headers)

        self._service_user(
            email=OTHER_RESOLVER_USER,
            api_key=OTHER_RESOLVER_API_KEY,
            api_secret=OTHER_RESOLVER_API_SECRET,
        )
        other_service_headers = self._headers()
        other_service_headers["Authorization"] = (
            f"token {OTHER_RESOLVER_API_KEY}:{OTHER_RESOLVER_API_SECRET}"
        )
        self._authenticate(other_service_headers, expected_user=OTHER_RESOLVER_USER)
        wrong_service = identity_resolution_api.resolve(
            self._payload("extid:v1:email:WrongServiceToken01")
        )
        self.assertEqual(frappe.local.response["http_status_code"], 403)
        self.assertEqual(wrong_service, {"error": {"code": "identity_scope_mismatch"}})
        self.assertEqual(frappe.local.response["headers"]["Cache-Control"], "no-store")

        frappe.set_user("Administrator")
        frappe.db.set_value("User", RESOLVER_USER, "enabled", 0)
        frappe.clear_cache(user=RESOLVER_USER)
        with self.assertRaises(frappe.AuthenticationError):
            self._authenticate(self._headers())

    def test_exact_site_purpose_and_auth_ref_binding_is_fail_closed(self) -> None:
        subject = "extid:v1:email:NativeScopeBinding01"
        self._identity(subject)
        cases = (
            (
                self._payload(subject, site_id="other.localhost"),
                self._headers(**{"X-Site-ID": "other.localhost"}),
            ),
            (
                self._payload(subject, processing_purpose="metric_reporting"),
                self._headers(**{"X-Processing-Purpose": "metric_reporting"}),
            ),
            (
                self._payload(subject, auth_ref="other-resolver-v1"),
                self._headers(**{"X-GBOS-Frappe-Auth-Ref": "other-resolver-v1"}),
            ),
        )

        for payload, headers in cases:
            with self.subTest(payload=payload):
                response = self._resolve(payload, headers=headers)
                self.assertEqual(frappe.local.response["http_status_code"], 403)
                self.assertEqual(response, {"error": {"code": "identity_scope_mismatch"}})
                self.assertEqual(
                    frappe.local.response["headers"]["Cache-Control"],
                    "no-store",
                )
                self.assertFalse(identity_resolution_scope_active())

    def test_confirmed_and_revoked_mappings_use_the_real_database_boundary(self) -> None:
        confirmed_subject = "extid:v1:email:NativeConfirmed01"
        revoked_subject = "extid:v1:email:NativeRevoked01"
        confirmed = self._identity(confirmed_subject)
        revoked = self._identity(revoked_subject, business_status="Revoked")

        for identity, subject, status in (
            (confirmed, confirmed_subject, "confirmed"),
            (revoked, revoked_subject, "revoked"),
        ):
            with self.subTest(status=status):
                response = self._resolve(self._payload(subject))
                resolution = response["resolutions"][0]
                self.assertEqual(resolution["mapping_ref"], identity.name)
                self.assertEqual(resolution["mapping_revision"], identity.revision)
                self.assertEqual(resolution["team_ref"], self.team.name)
                self.assertEqual(resolution["target_ref"], RESOLVER_MEMBER)
                self.assertEqual(resolution["status"], status)
                self.assertEqual(
                    frappe.local.response["headers"]["Cache-Control"],
                    "no-store",
                )
                self.assertNotIn("http_status_code", frappe.local.response)
                self.assertFalse(identity_resolution_scope_active())

    def test_unresolved_and_conflicts_are_closed_and_clear_request_guard(self) -> None:
        subject = "extid:v1:email:NativeRevisionConflict01"
        identity = self._identity(subject)

        unresolved = self._resolve(self._payload("extid:v1:email:NativeUnresolved01"))
        self.assertEqual(frappe.local.response["http_status_code"], 404)
        self.assertEqual(unresolved, {"error": {"code": "mapping_not_resolved"}})
        self.assertFalse(identity_resolution_scope_active())
        self.assertFalse(hasattr(frappe.local, RESOLVER_SCOPE_ATTRIBUTE))

        lookup = self._payload(subject)["lookups"][0]
        lookup["expected_mapping_revision"] = identity.revision + 1
        conflict = self._resolve(self._payload(subject, lookups=[lookup]))
        self.assertEqual(frappe.local.response["http_status_code"], 409)
        self.assertEqual(conflict, {"error": {"code": "mapping_revision_conflict"}})
        self.assertEqual(frappe.local.response["headers"]["Cache-Control"], "no-store")
        self.assertFalse(identity_resolution_scope_active())
        self.assertFalse(hasattr(frappe.local, RESOLVER_SCOPE_ATTRIBUTE))

        frappe.db.set_value(identity.doctype, identity.name, "identity_type", "Channel")
        corrupt_mapping = self._resolve(self._payload(subject))
        self.assertEqual(frappe.local.response["http_status_code"], 409)
        self.assertEqual(corrupt_mapping, {"error": {"code": "mapping_conflict"}})
        self.assertFalse(identity_resolution_scope_active())
        self.assertFalse(hasattr(frappe.local, RESOLVER_SCOPE_ATTRIBUTE))

    def test_direct_doctype_list_and_guarded_mapping_access_stay_denied(self) -> None:
        subject = "extid:v1:email:NativeDirectAccessDenied01"
        identity = self._identity(subject)
        frappe.set_user(RESOLVER_USER)

        with self.assertRaises(frappe.PermissionError):
            identity.check_permission("read")
        with self.assertRaises(frappe.PermissionError):
            frappe.get_list("GBOS External Identity", fields=["name"])
        with self.assertRaises(frappe.PermissionError):
            identity_resolution_api._mapping_rows(
                provider="email",
                external_subject=subject,
            )
        self.assertFalse(identity_resolution_scope_active())
