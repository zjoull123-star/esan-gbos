from __future__ import annotations

import importlib
import sys
from collections.abc import Generator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]
ENDPOINT_PATH = (
    ROOT / "apps" / "esan_gbos" / "esan_gbos" / "api" / "internal" / "identity_resolution.py"
)
ROLE = "Observer Identity Resolver"
USER = "resolver@example.invalid"
AUTH_REF = "observer-identity-resolver-v1"
SUBJECT = "extid:v1:email:OpaqueSender01"


class _PermissionError(Exception):
    pass


class _Database:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.rollbacks = 0
        self.raise_on_sql: Exception | None = None
        self.sql_calls: list[tuple[str, dict[str, Any]]] = []

    def sql(
        self,
        query: str,
        values: dict[str, Any],
        *,
        as_dict: bool,
    ) -> list[dict[str, Any]]:
        assert as_dict is True
        self.sql_calls.append((query, dict(values)))
        if self.raise_on_sql is not None:
            raise self.raise_on_sql
        return [
            dict(row)
            for row in self.rows.get(
                (str(values["identity_provider"]), str(values["external_subject"])),
                [],
            )
        ]

    def rollback(self) -> None:
        self.rollbacks += 1


class _Frappe(ModuleType):
    def __init__(self) -> None:
        super().__init__("frappe")
        self.PermissionError = _PermissionError
        self.session = SimpleNamespace(user=USER)
        self.local = SimpleNamespace(
            site="gbos.localhost",
            response={},
            request=SimpleNamespace(
                method="POST",
                headers={
                    "Authorization": "token resolver-key:resolver-secret",
                    "X-Site-ID": "gbos.localhost",
                    "X-Processing-Purpose": "identity_resolution",
                    "X-Request-ID": "resolution-0001",
                    "X-GBOS-Frappe-Auth-Ref": AUTH_REF,
                },
            ),
        )
        self.conf: dict[str, Any] = {
            "gbos_identity_resolver_identities": {
                AUTH_REF: {
                    "user": USER,
                    "site_id": "gbos.localhost",
                    "processing_purposes": ["identity_resolution"],
                }
            }
        }
        self.roles = {USER: {ROLE}}
        self.db = _Database()

    def whitelist(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs

        def decorate(function: Any) -> Any:
            return function

        return decorate

    def get_roles(self, user: str | None = None) -> list[str]:
        return sorted(self.roles.get(user or self.session.user, set()))


@pytest.fixture
def resolution_api() -> Generator[tuple[Any, _Frappe]]:
    fake = _Frappe()
    original_frappe = sys.modules.get("frappe")
    original_access = sys.modules.pop("esan_gbos.identity_resolver_access", None)
    original_endpoint = sys.modules.pop("esan_gbos.api.internal.identity_resolution", None)
    original_permissions = sys.modules.pop("esan_gbos.permissions", None)
    sys.modules["frappe"] = fake
    module = importlib.import_module("esan_gbos.api.internal.identity_resolution")
    yield module, fake
    sys.modules.pop("esan_gbos.api.internal.identity_resolution", None)
    sys.modules.pop("esan_gbos.identity_resolver_access", None)
    sys.modules.pop("esan_gbos.permissions", None)
    if original_endpoint is not None:
        sys.modules["esan_gbos.api.internal.identity_resolution"] = original_endpoint
    if original_access is not None:
        sys.modules["esan_gbos.identity_resolver_access"] = original_access
    if original_permissions is not None:
        sys.modules["esan_gbos.permissions"] = original_permissions
    if original_frappe is None:
        sys.modules.pop("frappe", None)
    else:
        sys.modules["frappe"] = original_frappe


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "site_id": "gbos.localhost",
        "processing_purpose": "identity_resolution",
        "request_id": "resolution-0001",
        "auth_ref": AUTH_REF,
        "lookups": [
            {
                "identity_provider": "email",
                "external_subject_ref": SUBJECT,
                "expected_team_ref": "TEM-01",
                "expected_mapping_revision": 4,
            }
        ],
    }
    payload.update(overrides)
    return payload


def _approved_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "mapping_ref": "EID-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "mapping_revision": 4,
        "team_ref": "TEM-01",
        "target_type": "User",
        "user_ref": "member@example.invalid",
        "party_ref": None,
        "review_status": "Approved",
        "business_status": "Active",
        "target_eligible": 1,
        "resolved_at": "2026-08-09T00:00:00Z",
        "crm_phone": "+8613800138000",
        "display_name": "Sensitive Name",
    }
    row.update(overrides)
    return row


def test_internal_identity_endpoint_is_authenticated_post_only_and_has_no_bypass() -> None:
    source = ENDPOINT_PATH.read_text(encoding="utf-8")

    assert "allow_guest" not in source
    assert "ignore_permissions" not in source
    assert source.count('@frappe.whitelist(methods=["POST"])') == 1


def test_confirmed_resolution_returns_only_the_frozen_contract_fields(
    resolution_api: tuple[Any, _Frappe],
) -> None:
    api, fake = resolution_api
    fake.db.rows[("email", SUBJECT)] = [_approved_row()]

    response = api.resolve(_payload())

    assert fake.local.response.get("http_status_code") is None
    assert fake.local.response["headers"]["Cache-Control"] == "no-store"
    assert set(response) == {"resolutions"}
    assert response["resolutions"] == [
        {
            "schema_version": "1.0",
            "site_id": "gbos.localhost",
            "identity_provider": "email",
            "external_subject_ref": SUBJECT,
            "mapping_ref": "EID-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "mapping_revision": 4,
            "team_ref": "TEM-01",
            "target_type": "User",
            "target_ref": "member@example.invalid",
            "status": "confirmed",
            "resolved_at": "2026-08-09T00:00:00Z",
        }
    ]
    assert "+8613800138000" not in repr(response)
    assert "Sensitive Name" not in repr(response)
    assert fake.db.sql_calls


def test_revoked_resolution_returns_only_the_frozen_contract_fields(
    resolution_api: tuple[Any, _Frappe],
) -> None:
    api, fake = resolution_api
    fake.db.rows[("email", SUBJECT)] = [
        _approved_row(
            business_status="Revoked",
            resolved_at="2026-08-10T01:02:03+08:00",
        )
    ]

    response = api.resolve(_payload())

    assert fake.local.response.get("http_status_code") is None
    assert response == {
        "resolutions": [
            {
                "schema_version": "1.0",
                "site_id": "gbos.localhost",
                "identity_provider": "email",
                "external_subject_ref": SUBJECT,
                "mapping_ref": "EID-01ARZ3NDEKTSV4RRFFQ69G5FAV",
                "mapping_revision": 4,
                "team_ref": "TEM-01",
                "target_type": "User",
                "target_ref": "member@example.invalid",
                "status": "revoked",
                "resolved_at": "2026-08-10T01:02:03+08:00",
            }
        ]
    }
    assert set(response["resolutions"][0]) == {
        "schema_version",
        "site_id",
        "identity_provider",
        "external_subject_ref",
        "mapping_ref",
        "mapping_revision",
        "team_ref",
        "target_type",
        "target_ref",
        "status",
        "resolved_at",
    }
    assert "+8613800138000" not in repr(response)
    assert "Sensitive Name" not in repr(response)


@pytest.mark.parametrize(
    "row",
    (
        _approved_row(target_type="User", target_eligible=0),
        _approved_row(
            target_type="Party",
            user_ref=None,
            party_ref="PTY-01",
            target_eligible=0,
        ),
    ),
)
def test_approved_active_mapping_fails_closed_when_live_target_is_ineligible(
    resolution_api: tuple[Any, _Frappe],
    row: dict[str, Any],
) -> None:
    api, fake = resolution_api
    fake.db.rows[("email", SUBJECT)] = [row]

    response = api.resolve(_payload())

    assert fake.local.response["http_status_code"] == 404
    assert response == {"error": {"code": "mapping_not_resolved"}}
    for protected_ref in (row.get("user_ref"), row.get("party_ref")):
        if protected_ref is not None:
            assert protected_ref not in repr(response)


def test_resolver_reads_live_user_membership_and_party_team_eligibility_in_one_query(
    resolution_api: tuple[Any, _Frappe],
) -> None:
    api, fake = resolution_api
    fake.db.rows[("email", SUBJECT)] = [_approved_row()]

    api.resolve(_payload())

    query = fake.db.sql_calls[0][0]
    assert "`tabUser`" in query
    assert "`tabGBOS Team Member`" in query
    assert "`tabGBOS Party Profile`" in query
    assert "target_eligible" in query


def test_revoked_mapping_remains_resolvable_as_revoked_after_target_loses_eligibility(
    resolution_api: tuple[Any, _Frappe],
) -> None:
    api, fake = resolution_api
    fake.db.rows[("email", SUBJECT)] = [_approved_row(business_status="Revoked", target_eligible=0)]

    response = api.resolve(_payload())

    assert fake.local.response.get("http_status_code") is None
    assert response["resolutions"][0]["status"] == "revoked"


@pytest.mark.parametrize(
    ("review_status", "business_status"),
    (
        ("AI Draft", "Active"),
        ("Pending", "Active"),
        ("Rejected", "Active"),
        ("Superseded", "Archived"),
        ("Approved", "Archived"),
    ),
)
def test_non_authoritative_rows_never_resolve(
    resolution_api: tuple[Any, _Frappe],
    review_status: str,
    business_status: str,
) -> None:
    api, fake = resolution_api
    fake.db.rows[("email", SUBJECT)] = [
        _approved_row(review_status=review_status, business_status=business_status)
    ]

    response = api.resolve(_payload())

    assert fake.local.response["http_status_code"] == 404
    assert response == {"error": {"code": "mapping_not_resolved"}}


@pytest.mark.parametrize(
    ("mutation", "status", "code"),
    (
        ({"expected_team_ref": "TEM-OTHER"}, 403, "team_scope_mismatch"),
        ({"expected_mapping_revision": 5}, 409, "mapping_revision_conflict"),
    ),
)
def test_team_and_revision_pins_fail_closed(
    resolution_api: tuple[Any, _Frappe],
    mutation: dict[str, Any],
    status: int,
    code: str,
) -> None:
    api, fake = resolution_api
    fake.db.rows[("email", SUBJECT)] = [_approved_row()]
    lookup = {**_payload()["lookups"][0], **mutation}

    response = api.resolve(_payload(lookups=[lookup]))

    assert fake.local.response["http_status_code"] == status
    assert response == {"error": {"code": code}}


@pytest.mark.parametrize(
    ("mutation", "status", "code"),
    (
        ({"expected_team_ref": "TEM-OTHER"}, 403, "team_scope_mismatch"),
        ({"expected_mapping_revision": 5}, 409, "mapping_revision_conflict"),
    ),
)
def test_revoked_resolution_honors_team_and_revision_pins(
    resolution_api: tuple[Any, _Frappe],
    mutation: dict[str, Any],
    status: int,
    code: str,
) -> None:
    api, fake = resolution_api
    fake.db.rows[("email", SUBJECT)] = [_approved_row(business_status="Revoked")]
    lookup = {**_payload()["lookups"][0], **mutation}

    response = api.resolve(_payload(lookups=[lookup]))

    assert fake.local.response["http_status_code"] == status
    assert response == {"error": {"code": code}}


def test_zero_or_multiple_approved_rows_fail_closed(
    resolution_api: tuple[Any, _Frappe],
) -> None:
    api, fake = resolution_api

    missing = api.resolve(_payload())
    fake.db.rows[("email", SUBJECT)] = [
        _approved_row(),
        _approved_row(mapping_ref="EID-01ARZ3NDEKTSV4RRFFQ69G5FB0"),
    ]
    conflicting = api.resolve(_payload())

    assert missing == {"error": {"code": "mapping_not_resolved"}}
    assert conflicting == {"error": {"code": "mapping_conflict"}}
    assert fake.local.response["http_status_code"] == 409


@pytest.mark.parametrize(
    "rows",
    (
        [_approved_row(business_status="Revoked"), _approved_row(business_status="Revoked")],
        [_approved_row(), _approved_row(business_status="Revoked")],
    ),
)
def test_duplicate_or_conflicting_authoritative_rows_fail_closed(
    resolution_api: tuple[Any, _Frappe],
    rows: list[dict[str, Any]],
) -> None:
    api, fake = resolution_api
    fake.db.rows[("email", SUBJECT)] = rows

    response = api.resolve(_payload())

    assert fake.local.response["http_status_code"] == 409
    assert response == {"error": {"code": "mapping_conflict"}}


@pytest.mark.parametrize(
    "unsafe_timestamp",
    (
        "not-a-date",
        "2026-08-10T01:02:03",
        "2026-08-10T01:02:03Z\nprivate-data",
    ),
)
def test_resolution_rejects_timestamps_outside_the_closed_datetime_contract(
    resolution_api: tuple[Any, _Frappe],
    unsafe_timestamp: str,
) -> None:
    api, fake = resolution_api
    fake.db.rows[("email", SUBJECT)] = [_approved_row(resolved_at=unsafe_timestamp)]

    response = api.resolve(_payload())

    assert fake.local.response["http_status_code"] == 409
    assert response == {"error": {"code": "mapping_conflict"}}
    assert unsafe_timestamp not in repr(response)


@pytest.mark.parametrize(
    "lookups",
    (
        [],
        [
            {
                "identity_provider": "carrier_pigeon",
                "external_subject_ref": "extid:v1:carrier_pigeon:Opaque",
                "expected_team_ref": "TEM-01",
            }
        ],
        [
            {
                "identity_provider": "email",
                "external_subject_ref": SUBJECT,
                "expected_team_ref": "TEM-01",
            },
            {
                "identity_provider": "email",
                "external_subject_ref": SUBJECT,
                "expected_team_ref": "TEM-01",
            },
        ],
        [
            {
                "identity_provider": "email",
                "external_subject_ref": SUBJECT,
                "expected_team_ref": "TEM-01",
            }
            for _ in range(101)
        ],
    ),
)
def test_batch_shape_provider_duplicates_and_size_are_closed(
    resolution_api: tuple[Any, _Frappe],
    lookups: list[dict[str, Any]],
) -> None:
    api, fake = resolution_api

    response = api.resolve(_payload(lookups=lookups))

    assert fake.local.response["http_status_code"] == 422
    assert response == {"error": {"code": "invalid_resolution_request"}}
    assert not fake.db.sql_calls


def test_token_headers_config_and_exact_service_role_are_bound_per_request(
    resolution_api: tuple[Any, _Frappe],
) -> None:
    api, fake = resolution_api
    fake.db.rows[("email", SUBJECT)] = [_approved_row()]

    fake.local.request.headers["Authorization"] = "Bearer unsafe"
    unauthenticated = api.resolve(_payload())
    fake.local.request.headers["Authorization"] = "token resolver-key:resolver-secret"
    fake.local.request.headers["X-Request-ID"] = "different"
    mismatched = api.resolve(_payload())
    fake.local.request.headers["X-Request-ID"] = "resolution-0001"
    fake.roles[USER] = {"Agent TrustedMaterializer"}
    wrong_role = api.resolve(_payload())
    fake.roles[USER] = {ROLE, "GBOS Admin"}
    privileged_role_drift = api.resolve(_payload())
    fake.roles[USER] = {ROLE, "System Manager"}
    system_role_drift = api.resolve(_payload())

    assert unauthenticated == {"error": {"code": "authentication_required"}}
    assert mismatched == {"error": {"code": "identity_scope_mismatch"}}
    assert wrong_role == {"error": {"code": "authentication_required"}}
    assert privileged_role_drift == {"error": {"code": "authentication_required"}}
    assert system_role_drift == {"error": {"code": "authentication_required"}}
    assert fake.local.response["http_status_code"] == 401


def test_request_scope_is_removed_after_internal_exception_and_error_is_redacted(
    resolution_api: tuple[Any, _Frappe],
) -> None:
    api, fake = resolution_api
    access = importlib.import_module("esan_gbos.identity_resolver_access")
    fake.db.raise_on_sql = RuntimeError("raw-person@example.invalid")

    response = api.resolve(_payload())

    assert fake.local.response["http_status_code"] == 500
    assert fake.local.response["headers"]["Cache-Control"] == "no-store"
    assert response == {"error": {"code": "internal_error"}}
    assert "raw-person" not in repr(response)
    assert not access.identity_resolution_scope_active()


def test_resolver_role_has_no_general_doctype_or_list_bypass(
    resolution_api: tuple[Any, _Frappe],
) -> None:
    _api, fake = resolution_api
    permissions = importlib.import_module("esan_gbos.permissions")
    doc = SimpleNamespace(doctype="GBOS External Identity", team="TEM-01")

    assert permissions.integration_permission_query(USER) == "1=0"
    for permission_type in (
        "read",
        "write",
        "create",
        "delete",
        "report",
        "export",
        "print",
        "email",
        "share",
    ):
        assert not permissions.has_gbos_permission(
            doc,
            user=USER,
            permission_type=permission_type,
        )
