from __future__ import annotations

import hashlib
import importlib
import json
import sys
from collections.abc import Generator
from copy import deepcopy
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROLE = "Email Gateway Authority Consumer"
USER = "email-gateway-authority@localhost.invalid"
AUTH_REF = "email-gateway-authority-v1"
MAPPING = "EID-01ARZ3NDEKTSV4RRFFQ69G5FAV"
PARTY = "PTY-01ARZ3NDEKTSV4RRFFQ69G5FAV"
TEAM = "TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV"
OWNER = "owner@example.invalid"
_OWNER_REVISION_PAYLOAD = {
    "schema_version": "owner-eligibility-v1",
    "party_ref": PARTY,
    "party_revision": 2,
    "team_ref": TEAM,
    "team_revision": 3,
    "owner_user_ref": OWNER,
    "owner_enabled": 1,
    "owner_user_type": "System User",
    "membership_ref": "TM-0001",
    "membership_parent": TEAM,
    "membership_user": OWNER,
    "membership_enabled": 1,
    "membership_modified": "2026-08-13T00:00:00Z",
}
OWNER_REVISION = (
    "sha256:"
    + hashlib.sha256(
        json.dumps(
            _OWNER_REVISION_PAYLOAD,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
)
_PROJECT_REQUEST_KEYS = {
    "site_id",
    "processing_purpose",
    "request_id",
    "auth_ref",
    "mapping_ref",
    "expected_mapping_revision",
    "expected_team_ref",
}


class _PermissionError(Exception):
    pass


class _Database:
    def __init__(self) -> None:
        self.mapping_rows: list[dict[str, Any]] = []
        self.route_rows: list[dict[str, Any]] = []
        self.rollbacks = 0
        self.raise_on_sql: Exception | None = None

    def sql(
        self,
        query: str,
        values: dict[str, Any],
        *,
        as_dict: bool,
    ) -> list[dict[str, Any]]:
        assert as_dict is True
        if self.raise_on_sql is not None:
            raise self.raise_on_sql
        rows = self.route_rows if "owner_user_ref" in query else self.mapping_rows
        return deepcopy(rows)

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
                    "Authorization": "token authority-key:authority-secret",
                    "X-Site-ID": "gbos.localhost",
                    "X-Processing-Purpose": "email_gateway_authority",
                    "X-Request-ID": "gateway-authority-0001",
                    "X-GBOS-Frappe-Auth-Ref": AUTH_REF,
                },
            ),
        )
        self.conf = {
            "gbos_email_gateway_authority_identities": {
                AUTH_REF: {
                    "user": USER,
                    "site_id": "gbos.localhost",
                    "processing_purposes": ["email_gateway_authority"],
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

    def parse_json(self, value: str) -> Any:
        import json

        return json.loads(value)


@pytest.fixture
def authority_api() -> Generator[tuple[Any, _Frappe]]:
    fake = _Frappe()
    names = (
        "frappe",
        "esan_gbos.email_gateway_authority_access",
        "esan_gbos.api.internal.email_gateway_authority",
        "esan_gbos.domain.external_identity_projection",
        "esan_gbos.permissions",
    )
    originals = {name: sys.modules.get(name) for name in names}
    sys.modules["frappe"] = fake
    for name in names[1:]:
        sys.modules.pop(name, None)
    module = importlib.import_module("esan_gbos.api.internal.email_gateway_authority")
    yield module, fake
    for name, original in originals.items():
        sys.modules.pop(name, None)
        if original is not None:
            sys.modules[name] = original


def _mapping_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "mapping_ref": MAPPING,
        "mapping_revision": 4,
        "team_ref": TEAM,
        "target_type": "Party",
        "user_ref": None,
        "party_ref": PARTY,
        "review_status": "Approved",
        "business_status": "Active",
        "target_eligible": 1,
        "resolved_at": "2026-08-13T00:00:00Z",
        "external_subject": "raw-address-must-not-escape@example.invalid",
        "contact_email": "contact-must-not-escape@example.invalid",
    }
    row.update(overrides)
    return row


def _route_row(**overrides: Any) -> dict[str, Any]:
    row = {
        **_mapping_row(),
        "party_revision": 2,
        "party_status": "Active",
        "party_review_status": "Approved",
        "team_revision": 3,
        "owner_user_ref": OWNER,
        "owner_enabled": 1,
        "owner_user_type": "System User",
        "membership_ref": "TM-0001",
        "membership_parent": TEAM,
        "membership_user": OWNER,
        "membership_enabled": 1,
        "membership_modified": "2026-08-13T00:00:00Z",
        "owner_eligibility_revision": OWNER_REVISION,
        "document_owner": "forbidden-doc-owner@example.invalid",
        "contact_owner": "forbidden-contact@example.invalid",
        "cc_owner": "forbidden-cc@example.invalid",
        "gateway_prior_owner": "forbidden-prior@example.invalid",
        "deal_owner": "forbidden-deal@example.invalid",
    }
    row.update(overrides)
    return row


def _payload(**overrides: Any) -> dict[str, Any]:
    value = {
        "site_id": "gbos.localhost",
        "processing_purpose": "email_gateway_authority",
        "request_id": "gateway-authority-0001",
        "auth_ref": AUTH_REF,
        "mapping_ref": MAPPING,
        "expected_mapping_revision": 4,
        "expected_team_ref": TEAM,
        "expected_party_revision": 2,
        "expected_team_revision": 3,
        "expected_owner_eligibility_revision": OWNER_REVISION,
    }
    value.update(overrides)
    return value


def test_projection_returns_only_mapping_revision_status_type_and_team(
    authority_api: tuple[Any, _Frappe],
) -> None:
    api, fake = authority_api
    fake.db.mapping_rows = [_mapping_row()]

    response = api.project(
        {key: value for key, value in _payload().items() if key in _PROJECT_REQUEST_KEYS}
    )

    assert response == {
        "identity_projection": {
            "mapping_ref": MAPPING,
            "mapping_revision": 4,
            "status": "confirmed",
            "target_type": "Party",
            "team_ref": TEAM,
        }
    }
    assert fake.local.response["headers"]["Cache-Control"] == "no-store"
    assert "raw-address" not in repr(response)
    assert "contact-must-not-escape" not in repr(response)


@pytest.mark.parametrize(
    "row",
    (
        _mapping_row(target_type="Party", party_ref=None),
        _mapping_row(target_type="Party", user_ref=OWNER),
        _mapping_row(target_type="User", user_ref=None, party_ref=None),
    ),
)
def test_projection_fails_closed_for_invalid_target_shape_without_leakage(
    authority_api: tuple[Any, _Frappe],
    row: dict[str, Any],
) -> None:
    api, fake = authority_api
    fake.db.mapping_rows = [row]

    response = api.project(
        {key: value for key, value in _payload().items() if key in _PROJECT_REQUEST_KEYS}
    )

    assert response == {"error": {"code": "mapping_not_resolved"}}
    assert OWNER not in repr(response)


def test_route_returns_assigned_only_for_exact_current_eligible_owner(
    authority_api: tuple[Any, _Frappe],
) -> None:
    api, fake = authority_api
    fake.db.route_rows = [_route_row()]

    response = api.resolve_route(_payload())

    assert response["route_authority"] == {
        "route_status": "assigned",
        "party_ref": PARTY,
        "party_revision": 2,
        "team_ref": TEAM,
        "team_revision": 3,
        "owner_user_ref": OWNER,
        "owner_eligibility_revision": OWNER_REVISION,
        "resolved_at": "2026-08-13T00:00:00Z",
    }


@pytest.mark.parametrize(
    "rows",
    (
        [],
        [_route_row(), _route_row()],
        [_route_row(owner_user_ref=None)],
        [_route_row(owner_enabled=0)],
        [_route_row(owner_user_type="Website User")],
        [_route_row(membership_enabled=0)],
        [_route_row(membership_parent="TEM-CROSS")],
        [_route_row(party_status="Inactive")],
        [_route_row(party_review_status="Pending")],
        [_route_row(party_revision=None)],
        [_route_row(team_revision=None)],
        [_route_row(resolved_at="not-a-timestamp")],
        [_route_row(review_status="Pending")],
        [_route_row(target_type="User", party_ref=None, user_ref=OWNER)],
    ),
)
def test_missing_ambiguous_cross_team_disabled_or_unapproved_routes_are_unassigned(
    authority_api: tuple[Any, _Frappe],
    rows: list[dict[str, Any]],
) -> None:
    api, fake = authority_api
    fake.db.route_rows = rows

    response = api.resolve_route(_payload())

    assert response["route_authority"]["route_status"] == "unassigned"
    assert response["route_authority"]["safe_reason_code"] == "owner_unavailable"
    assert OWNER not in repr(response)


@pytest.mark.parametrize(
    "override",
    (
        {"expected_mapping_revision": 3},
        {"expected_team_ref": "TEM-CROSS"},
        {"expected_party_revision": 1},
        {"expected_team_revision": 2},
        {"expected_owner_eligibility_revision": "sha256:" + "b" * 64},
    ),
)
def test_stale_expected_revisions_fail_closed_without_owner_leakage(
    authority_api: tuple[Any, _Frappe],
    override: dict[str, Any],
) -> None:
    api, fake = authority_api
    fake.db.route_rows = [_route_row()]

    response = api.resolve_route(_payload(**override))

    assert response["route_authority"]["route_status"] == "unassigned"
    assert OWNER not in repr(response)


def test_route_never_infers_from_document_contact_cc_prior_gateway_or_deal(
    authority_api: tuple[Any, _Frappe],
) -> None:
    api, fake = authority_api
    fake.db.route_rows = [_route_row(owner_user_ref=None)]

    response = api.resolve_route(_payload())

    assert response["route_authority"]["route_status"] == "unassigned"
    for forbidden in (
        "forbidden-doc-owner",
        "forbidden-contact",
        "forbidden-cc",
        "forbidden-prior",
        "forbidden-deal",
    ):
        assert forbidden not in repr(response)


def test_exact_service_identity_and_request_scope_are_fail_closed_and_cleaned(
    authority_api: tuple[Any, _Frappe],
) -> None:
    api, fake = authority_api
    access = importlib.import_module("esan_gbos.email_gateway_authority_access")
    fake.db.raise_on_sql = RuntimeError("sensitive-user@example.invalid")

    response = api.resolve_route(_payload())

    assert response == {"error": {"code": "internal_error"}}
    assert fake.local.response["http_status_code"] == 500
    assert fake.local.response["headers"]["Cache-Control"] == "no-store"
    assert not access.email_gateway_authority_scope_active()
