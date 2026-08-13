from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import frappe
from frappe.auth import validate_auth
from frappe.tests import IntegrationTestCase
from werkzeug.wrappers import Request

from esan_gbos.api.internal import email_command_publication as publication_api
from esan_gbos.api.internal import email_gateway_authority as email_gateway_authority_api
from esan_gbos.api.v5.email_send import _approve_locked, approve, submit_for_review
from esan_gbos.domain.email_review_policy import (
    protect_live_email_send_snapshot,
    protected_user_ref,
)
from esan_gbos.domain.external_identity_projection import owner_eligibility_revision
from esan_gbos.domain.naming import make_gbos_name
from esan_gbos.domain.permissions import (
    EMAIL_COMMAND_PUBLICATION_ROLE,
    EMAIL_GATEWAY_AUTHORITY_ROLE,
)
from esan_gbos.gbos.doctype.gbos_command_publication.gbos_command_publication import (
    GBOSCommandPublication,
)
from esan_gbos.gbos.doctype.gbos_email_send_approval.gbos_email_send_approval import (
    approval_snapshot,
)

TEST_OWNER = "gbos-email-send-owner@example.invalid"
AUTHORITY_USER = "gbos-email-send-authority@localhost.invalid"
AUTHORITY_AUTH_REF = "email-gateway-authority-v1"
AUTHORITY_API_KEY = "EmailAuthorityKey_0123456789ABCDEF"
AUTHORITY_API_SECRET = "EmailAuthoritySecret_0123456789ABCDEF"
AUTHORITY_CONFIG_KEY = "gbos_email_gateway_authority_identities"
PUBLICATION_USER = "gbos-email-command-publication@localhost.invalid"
PUBLICATION_AUTH_REF = "email-command-publication-v1"
PUBLICATION_API_KEY = "EmailPublicationKey_0123456789ABCDEF"
PUBLICATION_API_SECRET = "EmailPublicationSecret_0123456789ABCDEF"
PUBLICATION_CONFIG_KEY = "gbos_email_command_publication_identities"
_MISSING = object()

IGNORE_TEST_RECORD_DEPENDENCIES = [
    "DocType",
    "GBOS Review Decision",
    "GBOS Approved Command",
    "GBOS Command Publication",
    "GBOS Team",
    "User",
]


class TestGBOSEmailSendApproval(IntegrationTestCase):
    def setUp(self) -> None:
        frappe.set_user("Administrator")
        if not frappe.db.exists("User", TEST_OWNER):
            frappe.get_doc(
                {
                    "doctype": "User",
                    "email": TEST_OWNER,
                    "first_name": "GBOS email send owner",
                    "send_welcome_email": 0,
                }
            ).insert(ignore_permissions=True)
        user = frappe.get_doc("User", TEST_OWNER)
        if "Sales User" not in frappe.get_roles(TEST_OWNER):
            user.add_roles("Sales User")
        self.team = frappe.get_doc(
            {
                "doctype": "GBOS Team",
                "team_name": "Email send native test team",
                "members": [{"user": TEST_OWNER, "team_role": "Member", "enabled": 1}],
            }
        ).insert(ignore_permissions=True)
        frappe.set_user(TEST_OWNER)

    def tearDown(self) -> None:
        frappe.set_user("Administrator")

    def test_doctype_has_no_standard_permissions_and_forbids_sensitive_fields(self) -> None:
        meta = frappe.get_meta("GBOS Email Send Approval")
        self.assertFalse(meta.permissions)
        fields = {field.fieldname for field in meta.fields}
        for forbidden in (
            "subject",
            "body",
            "mime",
            "sender_address",
            "recipient_address",
            "provider_payload",
            "send_state",
        ):
            self.assertNotIn(forbidden, fields)
        self.assertTrue(json.loads(meta.as_json()))

    def _live_snapshot(self) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "site_id": frappe.local.site,
            "processing_purpose": "customer_service",
            "team_ref": self.team.name,
            "assignee_user_name": TEST_OWNER,
            "approval_expires_at": (datetime.now(UTC) + timedelta(hours=1))
            .isoformat()
            .replace("+00:00", "Z"),
            "mailbox_ref": make_gbos_name("MBX"),
            "mailbox_config_revision": 1,
            "inbox_item_ref": make_gbos_name("INB"),
            "inbox_item_revision": 1,
            "conversation_ref": make_gbos_name("CNV"),
            "conversation_revision": 1,
            "reply_draft_ref": make_gbos_name("DRF"),
            "reply_draft_revision": 1,
            "reply_draft_digest": "sha256:" + "1" * 64,
            "participants": [
                {
                    "address_role": "sender",
                    "opaque_address_ref": "extid:v1:email:" + "A" * 43,
                },
                {
                    "address_role": "to",
                    "opaque_address_ref": "extid:v1:email:" + "B" * 43,
                    "identity_mapping_ref": make_gbos_name("EID"),
                    "identity_mapping_revision": 1,
                },
            ],
            "party_ref": make_gbos_name("PTY"),
            "party_revision": 1,
            "team_revision": int(self.team.revision),
            "owner_user_name": TEST_OWNER,
            "owner_eligibility_revision": "sha256:" + "2" * 64,
            "final_mime_evidence_ref": make_gbos_name("EVR"),
            "final_mime_digest": "sha256:" + "3" * 64,
            "evidence_refs": [make_gbos_name("EVR")],
            "stable_client_request_id": make_gbos_name("CLI"),
        }

    def _submit(self, live: dict[str, object]) -> tuple[dict[str, object], object]:
        frappe.local.gbos_request_id = None
        protected = protect_live_email_send_snapshot(
            live,
            site_id=frappe.local.site,
            authenticated_user_name=TEST_OWNER,
        )
        with patch(
            "esan_gbos.api.v5.email_send._derive_submission_snapshot",
            return_value=protected,
        ):
            response = submit_for_review(
                str(live["inbox_item_ref"]),
                str(live["reply_draft_ref"]),
                int(live["inbox_item_revision"]),
                int(live["reply_draft_revision"]),
                "email-send-submit-" + hashlib.sha256(repr(live).encode()).hexdigest(),
            )
        self.assertNotIn("error", response)
        case = frappe.get_doc("GBOS Review Case", response["data"]["review_case_ref"])
        return response, case

    def _service_user(self) -> None:
        if not frappe.db.exists("User", AUTHORITY_USER):
            frappe.get_doc(
                {
                    "doctype": "User",
                    "email": AUTHORITY_USER,
                    "first_name": "GBOS Email Authority",
                    "enabled": 1,
                    "user_type": "Website User",
                    "send_welcome_email": 0,
                    "api_key": AUTHORITY_API_KEY,
                    "api_secret": AUTHORITY_API_SECRET,
                    "roles": [{"role": EMAIL_GATEWAY_AUTHORITY_ROLE}],
                }
            ).insert(ignore_permissions=True)
        frappe.clear_cache(user=AUTHORITY_USER)

    def _native_authority_snapshot(self) -> tuple[dict[str, object], Any]:
        party = frappe.get_doc(
            {
                "doctype": "GBOS Party Profile",
                "party_name": f"Email authority {frappe.generate_hash(length=8)}",
                "team": self.team.name,
                "owner_user": TEST_OWNER,
                "origin": "Manual",
                "business_status": "Active",
                "review_status": "Pending",
            }
        ).insert(ignore_permissions=True)
        frappe.db.set_value(
            party.doctype,
            party.name,
            "review_status",
            "Approved",
            update_modified=False,
        )
        party = frappe.get_doc(party.doctype, party.name)
        live = self._live_snapshot()
        recipient = live["participants"][1]  # type: ignore[index]
        identity = frappe.get_doc(
            {
                "doctype": "GBOS External Identity",
                "team": self.team.name,
                "identity_provider": "email",
                "external_subject": recipient["opaque_address_ref"],
                "identity_type": "Party",
                "party_profile": party.name,
                "origin": "Manual",
                "business_status": "Active",
                "review_status": "Pending",
            }
        ).insert(ignore_permissions=True)
        frappe.db.set_value(
            identity.doctype,
            identity.name,
            "review_status",
            "Approved",
            update_modified=False,
        )
        identity = frappe.get_doc(identity.doctype, identity.name)
        member = frappe.db.get_value(
            "GBOS Team Member",
            {"parent": self.team.name, "user": TEST_OWNER, "enabled": 1},
            ["name", "parent", "user", "enabled", "modified"],
            as_dict=True,
        )
        owner = frappe.db.get_value(
            "User",
            TEST_OWNER,
            ["enabled", "user_type"],
            as_dict=True,
        )
        live.update(
            party_ref=party.name,
            party_revision=int(party.revision),
            team_revision=int(self.team.revision),
            owner_eligibility_revision=owner_eligibility_revision(
                party,
                {
                    "owner_enabled": owner.enabled,
                    "owner_user_type": owner.user_type,
                    "membership_ref": member.name,
                    "membership_parent": member.parent,
                    "membership_user": member.user,
                    "membership_enabled": member.enabled,
                    "membership_modified": member.modified,
                    "team_revision": int(self.team.revision),
                },
            ),
        )
        recipient["identity_mapping_ref"] = identity.name
        recipient["identity_mapping_revision"] = int(identity.revision)
        return live, identity

    def _authenticate_authority(self, request_id: str) -> None:
        self._service_user()
        frappe.set_user("Guest")
        frappe.local.login_manager = SimpleNamespace(user="Guest")
        frappe.local.response = frappe._dict()
        frappe.local.request = Request.from_values(
            method="POST",
            headers={
                "Authorization": f"token {AUTHORITY_API_KEY}:{AUTHORITY_API_SECRET}",
                "X-Site-ID": frappe.local.site,
                "X-Processing-Purpose": "email_gateway_authority",
                "X-Request-ID": request_id,
                "X-GBOS-Frappe-Auth-Ref": AUTHORITY_AUTH_REF,
            },
        )
        validate_auth()
        self.assertEqual(frappe.session.user, AUTHORITY_USER)

    def _publication_service_user(self) -> None:
        if not frappe.db.exists("User", PUBLICATION_USER):
            frappe.get_doc(
                {
                    "doctype": "User",
                    "email": PUBLICATION_USER,
                    "first_name": "GBOS Email Command Publication",
                    "enabled": 1,
                    "user_type": "Website User",
                    "send_welcome_email": 0,
                    "api_key": PUBLICATION_API_KEY,
                    "api_secret": PUBLICATION_API_SECRET,
                    "roles": [{"role": EMAIL_COMMAND_PUBLICATION_ROLE}],
                }
            ).insert(ignore_permissions=True)
        frappe.clear_cache(user=PUBLICATION_USER)

    def _publication_request(self, request_id: str) -> None:
        frappe.local.response = frappe._dict()
        frappe.local.request = Request.from_values(
            method="POST",
            headers={
                "Authorization": f"token {PUBLICATION_API_KEY}:{PUBLICATION_API_SECRET}",
                "X-Site-ID": frappe.local.site,
                "X-Processing-Purpose": "email_command_publication",
                "X-Request-ID": request_id,
                "X-GBOS-Frappe-Auth-Ref": PUBLICATION_AUTH_REF,
            },
        )

    def _authenticate_publication(self, request_id: str) -> None:
        self._publication_service_user()
        frappe.set_user("Guest")
        frappe.local.login_manager = SimpleNamespace(user="Guest")
        self._publication_request(request_id)
        validate_auth()
        self.assertEqual(frappe.session.user, PUBLICATION_USER)

    def _approved_publication(self, suffix: str) -> tuple[object, object]:
        live = self._live_snapshot()
        _submitted, case = self._submit(live)
        protected = protect_live_email_send_snapshot(
            live,
            site_id=frappe.local.site,
            authenticated_user_name=TEST_OWNER,
        )
        frappe.local.gbos_request_id = None
        with patch(
            "esan_gbos.api.v5.email_send._derive_approval_live_snapshot",
            return_value=protected,
        ):
            approved = approve(
                case.name,
                case.revision,
                "Native publication boundary approval.",
                "idem:v2:" + hashlib.sha256((case.name + suffix).encode()).hexdigest(),
            )
        self.assertNotIn("error", approved)
        publication = frappe.get_doc(
            "GBOS Command Publication", approved["data"]["command_publication_ref"]
        )
        return case, publication

    def _configure_publication_identity(self) -> None:
        frappe.conf[PUBLICATION_CONFIG_KEY] = {
            PUBLICATION_AUTH_REF: {
                "user": PUBLICATION_USER,
                "site_id": frappe.local.site,
                "processing_purposes": ["email_command_publication"],
            }
        }

    def test_specialized_approval_is_atomic_replay_stable_and_contains_no_raw_user(self) -> None:
        live = self._live_snapshot()
        submitted, case = self._submit(live)
        approval = frappe.get_doc(
            "GBOS Email Send Approval", submitted["data"]["email_send_approval_ref"]
        )
        idempotency_key = "idem:v2:" + hashlib.sha256(case.name.encode()).hexdigest()
        frappe.local.gbos_request_id = None
        protected_live = protect_live_email_send_snapshot(
            live,
            site_id=frappe.local.site,
            authenticated_user_name=TEST_OWNER,
        )
        with patch(
            "esan_gbos.api.v5.email_send._derive_approval_live_snapshot",
            return_value=protected_live,
        ):
            first = approve(
                case.name,
                case.revision,
                "Current owner approves the frozen reply.",
                idempotency_key,
            )
            frappe.local.gbos_request_id = None
            replay = approve(
                case.name,
                case.revision,
                "Current owner approves the frozen reply.",
                idempotency_key,
            )

        self.assertNotIn("error", first)
        self.assertEqual(replay["data"], first["data"])
        self.assertTrue(replay["meta"]["replayed"])
        self.assertEqual(
            frappe.db.count("GBOS Approved Command", {"review_case": case.name}),
            1,
        )
        self.assertEqual(
            frappe.db.count(
                "GBOS Command Publication",
                {"approved_command": first["data"]["approved_command_ref"]},
            ),
            1,
        )
        command = frappe.get_doc("GBOS Approved Command", first["data"]["approved_command_ref"])
        publication = frappe.get_doc(
            "GBOS Command Publication", first["data"]["command_publication_ref"]
        )
        protected = protected_user_ref(frappe.local.site, TEST_OWNER)
        self.assertEqual(approval.assignee_user_ref, protected)
        self.assertEqual(command.actor_user_ref, protected)
        for stored in (
            approval_snapshot(approval),
            frappe.parse_json(command.command_payload),
            frappe.parse_json(publication.command_payload),
        ):
            self.assertNotIn(TEST_OWNER, repr(stored))

        approval.mailbox_ref = make_gbos_name("MBX")
        with self.assertRaises(frappe.PermissionError):
            approval.save(ignore_permissions=True)
        command.payload_sha256 = "0" * 64
        with self.assertRaises(frappe.PermissionError):
            command.save(ignore_permissions=True)
        publication.command_payload = "{}"
        with self.assertRaises(frappe.PermissionError):
            publication.save(ignore_permissions=True)

    def test_native_fenced_publication_resolves_current_send_authority(self) -> None:
        previous_config = frappe.conf.get(AUTHORITY_CONFIG_KEY, _MISSING)
        previous_request = getattr(frappe.local, "request", _MISSING)
        previous_response = getattr(frappe.local, "response", _MISSING)
        previous_login_manager = getattr(frappe.local, "login_manager", _MISSING)
        try:
            live, identity = self._native_authority_snapshot()
            submitted, case = self._submit(live)
            protected = protect_live_email_send_snapshot(
                live,
                site_id=frappe.local.site,
                authenticated_user_name=TEST_OWNER,
            )
            frappe.local.gbos_request_id = None
            with patch(
                "esan_gbos.api.v5.email_send._derive_approval_live_snapshot",
                return_value=protected,
            ):
                approved = approve(
                    case.name,
                    case.revision,
                    "Native authority must revalidate the current route.",
                    "idem:v2:"
                    + hashlib.sha256((case.name + "-native-authority").encode()).hexdigest(),
                )
            self.assertNotIn("error", approved)
            command = frappe.get_doc(
                "GBOS Approved Command", approved["data"]["approved_command_ref"]
            )
            publication = frappe.get_doc(
                "GBOS Command Publication", approved["data"]["command_publication_ref"]
            )
            publication.flags.gbos_publication_worker = True
            publication.publication_status = "Claimed"
            publication.attempt = 1
            publication.generation = 1
            publication.worker_id = "native-email-authority-worker"
            publication.fence_token = make_gbos_name("FNC")
            publication.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
            publication.claim_request_id = "native-authority-claim"
            publication.save(ignore_permissions=True)
            command_payload = frappe.parse_json(command.command_payload)
            request_id = command_payload["request_id"]
            frappe.conf[AUTHORITY_CONFIG_KEY] = {
                AUTHORITY_AUTH_REF: {
                    "user": AUTHORITY_USER,
                    "site_id": frappe.local.site,
                    "processing_purposes": ["email_gateway_authority"],
                }
            }
            self._authenticate_authority(request_id)

            response = email_gateway_authority_api.resolve_email_send_command(
                {
                    "site_id": frappe.local.site,
                    "processing_purpose": "email_gateway_authority",
                    "request_id": request_id,
                    "auth_ref": AUTHORITY_AUTH_REF,
                    "publication_ref": publication.name,
                    "attempt": 1,
                    "generation": 1,
                    "fence_token": publication.fence_token,
                    "command_ref": command.name,
                    "payload_digest": publication.payload_digest,
                }
            )

            self.assertNotIn("error", response)
            authority = response["email_send_authority"]
            self.assertEqual(authority["audience"], "email-command-executor")
            self.assertEqual(authority["party_ref"], live["party_ref"])
            self.assertEqual(authority["participants"][1]["identity_mapping_ref"], identity.name)
            self.assertEqual(frappe.local.response["headers"]["Cache-Control"], "no-store")
            self.assertNotIn(TEST_OWNER, repr(response))
        finally:
            frappe.set_user("Administrator")
            if previous_config is _MISSING:
                frappe.conf.pop(AUTHORITY_CONFIG_KEY, None)
            else:
                frappe.conf[AUTHORITY_CONFIG_KEY] = previous_config
            for attribute, value in (
                ("request", previous_request),
                ("response", previous_response),
                ("login_manager", previous_login_manager),
            ):
                if value is _MISSING:
                    if hasattr(frappe.local, attribute):
                        delattr(frappe.local, attribute)
                else:
                    setattr(frappe.local, attribute, value)

    def test_native_initial_route_derives_current_party_team_and_owner(self) -> None:
        previous_config = frappe.conf.get(AUTHORITY_CONFIG_KEY, _MISSING)
        previous_request = getattr(frappe.local, "request", _MISSING)
        previous_response = getattr(frappe.local, "response", _MISSING)
        previous_login_manager = getattr(frappe.local, "login_manager", _MISSING)
        try:
            live, identity = self._native_authority_snapshot()
            request_id = f"native-initial-route-{frappe.generate_hash(length=8)}"
            frappe.conf[AUTHORITY_CONFIG_KEY] = {
                AUTHORITY_AUTH_REF: {
                    "user": AUTHORITY_USER,
                    "site_id": frappe.local.site,
                    "processing_purposes": ["email_gateway_authority"],
                }
            }
            self._authenticate_authority(request_id)

            response = email_gateway_authority_api.resolve_initial_route(
                {
                    "site_id": frappe.local.site,
                    "processing_purpose": "email_gateway_authority",
                    "request_id": request_id,
                    "auth_ref": AUTHORITY_AUTH_REF,
                    "mapping_ref": identity.name,
                    "expected_mapping_revision": int(identity.revision),
                    "expected_team_ref": self.team.name,
                }
            )

            self.assertNotIn("error", response)
            authority = response["route_authority"]
            self.assertEqual(authority["route_status"], "assigned")
            self.assertEqual(authority["party_ref"], live["party_ref"])
            self.assertEqual(authority["party_revision"], live["party_revision"])
            self.assertEqual(authority["team_ref"], self.team.name)
            self.assertEqual(authority["team_revision"], live["team_revision"])
            self.assertEqual(authority["owner_user_ref"], TEST_OWNER)
            self.assertEqual(
                authority["owner_eligibility_revision"],
                live["owner_eligibility_revision"],
            )
            self.assertEqual(frappe.local.response["headers"]["Cache-Control"], "no-store")
        finally:
            frappe.set_user("Administrator")
            if previous_config is _MISSING:
                frappe.conf.pop(AUTHORITY_CONFIG_KEY, None)
            else:
                frappe.conf[AUTHORITY_CONFIG_KEY] = previous_config
            for attribute, value in (
                ("request", previous_request),
                ("response", previous_response),
                ("login_manager", previous_login_manager),
            ):
                if value is _MISSING:
                    if hasattr(frappe.local, attribute):
                        delattr(frappe.local, attribute)
                else:
                    setattr(frappe.local, attribute, value)

    def test_native_publication_claim_heartbeat_ack_and_replay(self) -> None:
        previous_config = frappe.conf.get(PUBLICATION_CONFIG_KEY, _MISSING)
        previous_request = getattr(frappe.local, "request", _MISSING)
        previous_response = getattr(frappe.local, "response", _MISSING)
        previous_login_manager = getattr(frappe.local, "login_manager", _MISSING)
        try:
            _case, publication = self._approved_publication("-native-publication")
            self._configure_publication_identity()
            claim_request_id = "native-publication-claim"
            self._authenticate_publication(claim_request_id)
            claim_payload = {
                "site_id": frappe.local.site,
                "processing_purpose": "email_command_publication",
                "worker_id": "native-publication-worker",
                "lease_seconds": 30,
                "request_id": claim_request_id,
            }

            first_claim = publication_api.claim(claim_payload)
            replayed_claim = publication_api.claim(claim_payload)

            self.assertEqual(replayed_claim, first_claim)
            claim = first_claim["publication"]
            self.assertEqual(claim["publication_ref"], publication.name)
            self.assertEqual(claim["attempt"], 1)
            self.assertEqual(claim["generation"], 1)
            identity = {
                "site_id": frappe.local.site,
                "processing_purpose": "email_command_publication",
                "worker_id": "native-publication-worker",
                "publication_ref": publication.name,
                "attempt": claim["attempt"],
                "generation": claim["generation"],
                "fence_token": claim["fence_token"],
            }

            heartbeat_request_id = "native-publication-heartbeat"
            self._publication_request(heartbeat_request_id)
            heartbeat_payload = {
                **identity,
                "lease_seconds": 45,
                "request_id": heartbeat_request_id,
            }
            first_heartbeat = publication_api.heartbeat(heartbeat_payload)
            replayed_heartbeat = publication_api.heartbeat(heartbeat_payload)
            self.assertEqual(replayed_heartbeat, first_heartbeat)

            acknowledgement_request_id = "native-publication-acknowledgement"
            self._publication_request(acknowledgement_request_id)
            acknowledgement_payload = {
                **identity,
                "request_id": acknowledgement_request_id,
                "command_receipt_ref": make_gbos_name("ECR"),
                "send_outbox_ref": make_gbos_name("SOB"),
                "payload_digest": claim["payload_digest"],
            }
            first_acknowledgement = publication_api.acknowledge(acknowledgement_payload)
            replayed_acknowledgement = publication_api.acknowledge(acknowledgement_payload)

            self.assertEqual(replayed_acknowledgement, first_acknowledgement)
            self.assertEqual(first_acknowledgement["acknowledgement"]["status"], "acknowledged")
            stored = frappe.get_doc("GBOS Command Publication", publication.name)
            self.assertEqual(stored.publication_status, "Acknowledged")
            self.assertEqual(
                frappe.db.count(
                    "GBOS Command Publication", {"approved_command": stored.approved_command}
                ),
                1,
            )
            self.assertEqual(frappe.local.response["headers"]["Cache-Control"], "no-store")
            self.assertNotIn(TEST_OWNER, repr(first_claim))
            self.assertNotIn(TEST_OWNER, repr(first_acknowledgement))
        finally:
            frappe.set_user("Administrator")
            if previous_config is _MISSING:
                frappe.conf.pop(PUBLICATION_CONFIG_KEY, None)
            else:
                frappe.conf[PUBLICATION_CONFIG_KEY] = previous_config
            for attribute, value in (
                ("request", previous_request),
                ("response", previous_response),
                ("login_manager", previous_login_manager),
            ):
                if value is _MISSING:
                    if hasattr(frappe.local, attribute):
                        delattr(frappe.local, attribute)
                else:
                    setattr(frappe.local, attribute, value)

    def test_native_publication_expired_claim_is_rejected(self) -> None:
        previous_config = frappe.conf.get(PUBLICATION_CONFIG_KEY, _MISSING)
        previous_request = getattr(frappe.local, "request", _MISSING)
        previous_response = getattr(frappe.local, "response", _MISSING)
        previous_login_manager = getattr(frappe.local, "login_manager", _MISSING)
        try:
            _case, publication = self._approved_publication("-native-expired-publication")
            self._configure_publication_identity()
            claim_request_id = "native-expired-publication-claim"
            self._authenticate_publication(claim_request_id)
            claimed = publication_api.claim(
                {
                    "site_id": frappe.local.site,
                    "processing_purpose": "email_command_publication",
                    "worker_id": "native-expired-publication-worker",
                    "lease_seconds": 30,
                    "request_id": claim_request_id,
                }
            )["publication"]
            identity = {
                "site_id": frappe.local.site,
                "processing_purpose": "email_command_publication",
                "worker_id": "native-expired-publication-worker",
                "publication_ref": publication.name,
                "attempt": claimed["attempt"],
                "generation": claimed["generation"],
                "fence_token": claimed["fence_token"],
            }
            expired_at = datetime.now(UTC) + timedelta(minutes=10)

            heartbeat_request_id = "native-expired-publication-heartbeat"
            self._publication_request(heartbeat_request_id)
            with patch.object(publication_api, "_now", return_value=expired_at):
                heartbeat = publication_api.heartbeat(
                    {
                        **identity,
                        "lease_seconds": 30,
                        "request_id": heartbeat_request_id,
                    }
                )
            self.assertEqual(heartbeat, {"error": {"code": "claim_lease_expired"}})
            self.assertEqual(frappe.local.response["http_status_code"], 409)

            acknowledgement_request_id = "native-expired-publication-ack"
            self._publication_request(acknowledgement_request_id)
            with patch.object(publication_api, "_now", return_value=expired_at):
                acknowledgement = publication_api.acknowledge(
                    {
                        **identity,
                        "request_id": acknowledgement_request_id,
                        "command_receipt_ref": make_gbos_name("ECR"),
                        "send_outbox_ref": make_gbos_name("SOB"),
                        "payload_digest": claimed["payload_digest"],
                    }
                )
            self.assertEqual(acknowledgement, {"error": {"code": "claim_lease_expired"}})
            self.assertEqual(frappe.local.response["http_status_code"], 409)
            stored = frappe.get_doc("GBOS Command Publication", publication.name)
            self.assertEqual(stored.publication_status, "Claimed")
            self.assertIsNone(stored.gateway_send_outbox_ref)
        finally:
            frappe.set_user("Administrator")
            if previous_config is _MISSING:
                frappe.conf.pop(PUBLICATION_CONFIG_KEY, None)
            else:
                frappe.conf[PUBLICATION_CONFIG_KEY] = previous_config
            for attribute, value in (
                ("request", previous_request),
                ("response", previous_response),
                ("login_manager", previous_login_manager),
            ):
                if value is _MISSING:
                    if hasattr(frappe.local, attribute):
                        delattr(frappe.local, attribute)
                else:
                    setattr(frappe.local, attribute, value)

    def test_publication_insert_failure_rolls_back_decision_and_command(self) -> None:
        live = self._live_snapshot()
        _submitted, case = self._submit(live)
        protected = protect_live_email_send_snapshot(
            live,
            site_id=frappe.local.site,
            authenticated_user_name=TEST_OWNER,
        )
        idempotency_key = (
            "idem:v2:" + hashlib.sha256((case.name + "-rollback").encode()).hexdigest()
        )
        savepoint = "email_send_publication_failure"
        frappe.db.savepoint(savepoint)
        with (
            patch.object(
                GBOSCommandPublication,
                "validate",
                side_effect=frappe.ValidationError("injected publication failure"),
            ),
            self.assertRaises(frappe.ValidationError),
            patch(
                "esan_gbos.api.v5.email_send._derive_approval_live_snapshot",
                return_value=protected,
            ),
        ):
            _approve_locked(
                payload={
                    "review_case_name": case.name,
                    "expected_revision": int(case.revision),
                    "decision_note": "Approval must roll back atomically.",
                    "idempotency_key": idempotency_key,
                },
                actor=TEST_OWNER,
                issued_at=datetime.now(UTC),
            )
        frappe.db.rollback(save_point=savepoint)

        pending = frappe.get_doc("GBOS Review Case", case.name)
        self.assertEqual(pending.business_status, "Pending")
        self.assertEqual(
            frappe.db.count("GBOS Approved Command", {"review_case": case.name}),
            0,
        )
