from __future__ import annotations

import copy
import importlib
import json
import sys
from collections.abc import Callable, Mapping
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

IDENTITY_USER = "extid:v1:email:user-opaque"
IDENTITY_PARTY = "extid:v1:email:party-opaque"
SUGGESTION_KEY = f"suggestion:v1:{'a' * 64}"


class TestBFFError(Exception):
    __test__ = False

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}


class FakeDoc(dict[str, Any]):
    def __init__(self, values: Mapping[str, Any]) -> None:
        super().__init__(copy.deepcopy(dict(values)))
        self.flags = SimpleNamespace()

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as error:
            raise AttributeError(name) from error

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "flags":
            object.__setattr__(self, name, value)
        else:
            self[name] = value

    def save(self, *, ignore_permissions: bool = False) -> FakeDoc:
        assert ignore_permissions is True
        self["revision"] = int(self.get("revision") or 0) + 1
        return self


class FakeFrappe(ModuleType):
    class PermissionError(Exception):
        pass

    class DoesNotExistError(Exception):
        pass

    class ValidationError(Exception):
        pass

    class DuplicateEntryError(Exception):
        pass

    def __init__(self) -> None:
        super().__init__("frappe")
        self.session = SimpleNamespace(user="sales@example.invalid")
        self.local = SimpleNamespace(
            site="gbos.localhost",
            gbos_request_id="REQ-identity-001",
            request=SimpleNamespace(method="GET", headers={}),
            response={},
        )
        self.roles: dict[str, set[str]] = {
            "sales@example.invalid": {"Sales User"},
            "reviewer@example.invalid": {"Reviewer"},
            "owner@example.invalid": {"Sales User"},
            "admin@example.invalid": {"GBOS Admin"},
        }
        self.tables: dict[str, list[dict[str, Any]]] = {
            "GBOS Team Member": [
                {"parent": "TEM-01", "user": "sales@example.invalid", "enabled": 1},
                {"parent": "TEM-01", "user": "reviewer@example.invalid", "enabled": 1},
                {"parent": "TEM-01", "user": "owner@example.invalid", "enabled": 1},
                {"parent": "TEM-02", "user": "outsider@example.invalid", "enabled": 1},
            ],
            "User": [
                {"name": "sales@example.invalid", "full_name": "Sales User", "enabled": 1},
                {
                    "name": "reviewer@example.invalid",
                    "full_name": "Identity Reviewer",
                    "enabled": 1,
                },
                {"name": "owner@example.invalid", "full_name": "Mailbox Owner", "enabled": 1},
                {"name": "disabled@example.invalid", "full_name": "Disabled", "enabled": 0},
                {"name": "outsider@example.invalid", "full_name": "Other Team", "enabled": 1},
            ],
            "GBOS Party Profile": [
                {
                    "name": "PTY-01",
                    "party_name": "Acme Same Team",
                    "team": "TEM-01",
                    "contact": "CON-01",
                },
                {
                    "name": "PTY-02",
                    "party_name": "Other Team Party",
                    "team": "TEM-02",
                    "contact": "CON-02",
                },
            ],
            "Contact": [
                {"name": "CON-01", "full_name": "Alice Contact"},
                {"name": "CON-02", "full_name": "Outside Contact"},
            ],
            "GBOS External Identity": [],
            "GBOS Review Case": [],
        }
        self.db = SimpleNamespace(rollback=lambda: None)

    @staticmethod
    def whitelist(
        *_args: Any, **_kwargs: Any
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return lambda function: function

    def get_roles(self, user: str | None = None) -> list[str]:
        return sorted(self.roles.get(user or self.session.user, set()))

    def get_all(
        self,
        doctype: str,
        *,
        filters: Mapping[str, Any] | None = None,
        fields: list[str] | None = None,
        order_by: str | None = None,
        limit_start: int = 0,
        limit_page_length: int | None = None,
        page_length: int | None = None,
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        del order_by
        rows = [row for row in self.tables.get(doctype, []) if _matches(row, filters or {})]
        rows = sorted(rows, key=lambda row: str(row.get("name") or row.get("parent") or ""))
        length = limit_page_length if limit_page_length is not None else page_length
        rows = rows[limit_start : None if length is None else limit_start + length]
        if fields is None:
            return copy.deepcopy(rows)
        return [{field: copy.deepcopy(row.get(field)) for field in fields} for row in rows]

    get_list = get_all

    def get_doc(self, doctype: str, name: str, *, for_update: bool = False) -> FakeDoc:
        del for_update
        matches = [row for row in self.tables.get(doctype, []) if row.get("name") == name]
        if len(matches) != 1:
            raise self.DoesNotExistError
        row = matches[0]
        if not isinstance(row, FakeDoc):
            row = FakeDoc(row)
            self.tables[doctype][self.tables[doctype].index(matches[0])] = row
        return row

    @staticmethod
    def parse_json(value: str) -> Any:
        return json.loads(value)


def _matches(row: Mapping[str, Any], filters: Mapping[str, Any]) -> bool:
    for field, expected in filters.items():
        actual = row.get(field)
        if isinstance(expected, list) and len(expected) == 2:
            operator, operand = expected
            if operator == "in" and actual not in operand:
                return False
            if operator == "is" and operand == "set" and actual in (None, ""):
                return False
        elif actual != expected:
            return False
    return True


def _communication(
    *,
    identities: list[dict[str, Any]] | None = None,
    suggestions: list[dict[str, Any]] | None = None,
    classification: str = "Internal",
) -> dict[str, Any]:
    return {
        "observation_id": "OBS-01",
        "channel": "email",
        "occurred_at": "2026-08-10T01:00:00+00:00",
        "summary_zh": "客户询问交期。",
        "original_language": "zh",
        "classification": classification,
        "review_status": "Pending",
        "team_ref": "TEM-01",
        "party_ref": None,
        "evidence_count": 1,
        "evidence": [{"ref": "EVD-01", "locator": "context://EVD-01"}],
        "fact_proposals": [],
        "association_suggestions": suggestions
        if suggestions is not None
        else [
            {
                "type": "party",
                "target_ref": "MODEL-PROVENANCE-ONLY",
                "confidence": 0.91,
                "suggestion_key": SUGGESTION_KEY,
            }
        ],
        "participant_identities": identities
        if identities is not None
        else [{"identity_ref": IDENTITY_PARTY, "provider": "email", "status": "unresolved"}],
        "connector_account_user_ref": "owner@example.invalid",
        "model": {"name": "deepseek-v4-flash", "version": "2026-08-10"},
        "raw_access_allowed": classification != "Restricted",
        **({"original_text": "restricted raw body"} if classification == "Restricted" else {}),
    }


@pytest.fixture
def identity_module(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, FakeFrappe, SimpleNamespace]:
    fake = FakeFrappe()
    state = SimpleNamespace(
        communication=_communication(),
        observer_calls=[],
        materialize_calls=[],
        submit_calls=[],
        idempotency={},
    )

    common = ModuleType("esan_gbos.api.v1.common")
    common.BFFError = TestBFFError
    common.bff_endpoint = lambda _method: lambda function: function
    common.request_id = lambda: "REQ-identity-001"

    def require_roles(allowed: set[str] | frozenset[str]) -> None:
        if not set(fake.get_roles()) & set(allowed):
            raise TestBFFError("permission_denied", "Role is not permitted", status=403)

    common.require_roles = require_roles

    gateway = ModuleType("esan_gbos.api.v4.gateway")

    def call_local(service: str, **kwargs: Any) -> dict[str, Any]:
        state.observer_calls.append({"service": service, **kwargs})
        return {"communication": copy.deepcopy(state.communication)}

    gateway.call_local = call_local
    gateway.v4_success = lambda data, **meta: {
        "data": data,
        "meta": {"schema_version": "4.0", "request_id": "REQ-identity-001", **meta},
    }

    communication = ModuleType("esan_gbos.api.v4.communication")
    communication._scope = lambda: {
        "actor_ref": fake.session.user,
        "allowed_team_refs": ["TEM-01"],
        "scope": "team_and_self",
        "include_raw": False,
    }

    audit = ModuleType("esan_gbos.api.v1.audit")

    def run_idempotent(
        command: str,
        key: str,
        payload: dict[str, Any],
        execute: Callable[[], dict[str, Any]],
        *,
        api_version: str = "v1",
    ) -> tuple[dict[str, Any], bool, str]:
        del api_version
        marker = (command, key)
        frozen = json.dumps(payload, sort_keys=True)
        if marker in state.idempotency:
            prior_payload, result, original_request_id = state.idempotency[marker]
            if prior_payload != frozen:
                raise TestBFFError("idempotency_conflict", "payload changed", status=409)
            return copy.deepcopy(result), True, original_request_id
        result = execute()
        state.idempotency[marker] = (frozen, copy.deepcopy(result), "REQ-identity-001")
        return result, False, "REQ-identity-001"

    audit.run_idempotent = run_idempotent

    review = ModuleType("esan_gbos.domain.identity_review")

    class IdentityReviewError(ValueError):
        pass

    def materialize(payload: dict[str, Any]) -> dict[str, Any]:
        state.materialize_calls.append(copy.deepcopy(payload))
        return {
            "doctype": "GBOS External Identity",
            "name": "EID-01",
            "review_status": "AI Draft",
            "revision": 1,
            "request_id": payload["request_id"],
        }

    def submit(payload: dict[str, Any]) -> dict[str, Any]:
        state.submit_calls.append(copy.deepcopy(payload))
        return {
            "doctype": "GBOS Review Case",
            "name": "REV-01",
            "review_status": "Pending",
            "revision": 1,
            "subject_name": "EID-01",
            "subject_revision": 2,
            "request_id": payload["request_id"],
        }

    review.IdentityReviewError = IdentityReviewError
    review.materialize_association_suggestion = materialize
    review.submit_for_review = submit

    module_names = {
        "frappe": fake,
        "esan_gbos.api.v1.common": common,
        "esan_gbos.api.v1.audit": audit,
        "esan_gbos.api.v4.gateway": gateway,
        "esan_gbos.api.v4.communication": communication,
        "esan_gbos.domain.identity_review": review,
    }
    previous = {name: sys.modules.get(name) for name in module_names}
    for name, value in module_names.items():
        monkeypatch.setitem(sys.modules, name, value)
    sys.modules.pop("esan_gbos.api.v4.identity", None)
    try:
        module = importlib.import_module("esan_gbos.api.v4.identity")
        yield module, fake, state
    finally:
        sys.modules.pop("esan_gbos.api.v4.identity", None)
        for name, value in previous.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def _mapping(
    identity_ref: str,
    *,
    name: str,
    review_status: str,
    business_status: str = "Active",
    identity_type: str = "Party",
    team: str = "TEM-01",
    revision: int = 1,
) -> dict[str, Any]:
    return {
        "name": name,
        "team": team,
        "identity_provider": "email",
        "external_subject": identity_ref,
        "identity_type": identity_type,
        "user": "sales@example.invalid" if identity_type == "User" else None,
        "party_profile": "PTY-01" if identity_type == "Party" else None,
        "business_status": business_status,
        "review_status": review_status,
        "revision": revision,
    }


def test_identity_states_overlay_local_draft_pending_confirmed_and_revoked_without_raw_refs(
    identity_module: tuple[Any, FakeFrappe, SimpleNamespace],
) -> None:
    identity, fake, state = identity_module
    refs = [f"extid:v1:email:opaque-{index}" for index in range(5)]
    state.communication = _communication(
        identities=[
            {"identity_ref": refs[0], "provider": "email", "status": "unresolved"},
            {"identity_ref": refs[1], "provider": "email", "status": "unresolved"},
            {"identity_ref": refs[2], "provider": "email", "status": "unresolved"},
            {
                "identity_ref": refs[3],
                "provider": "email",
                "status": "confirmed",
                "mapping_ref": "EID-03",
                "mapping_revision": 3,
                "target_type": "Party",
            },
            {
                "identity_ref": refs[4],
                "provider": "email",
                "status": "revoked",
                "mapping_ref": "EID-04",
                "mapping_revision": 4,
                "target_type": "Party",
            },
        ]
    )
    fake.tables["GBOS External Identity"] = [
        _mapping(refs[1], name="EID-01", review_status="AI Draft"),
        _mapping(refs[2], name="EID-02", review_status="Pending", revision=2),
        _mapping(refs[3], name="EID-03", review_status="Approved", revision=3),
        _mapping(
            refs[4],
            name="EID-04",
            review_status="Approved",
            business_status="Revoked",
            revision=4,
        ),
    ]

    response = identity.list_states("OBS-01")
    rendered = response["data"]

    assert [item["status"] for item in rendered["identities"]] == [
        "unresolved",
        "proposed",
        "pending",
        "confirmed",
        "revoked",
    ]
    assert rendered["identities"][3]["display_label"] == "Acme Same Team"
    assert rendered["connector_account_owner"] == {"display_label": "Mailbox Owner"}
    assert "external_subject" not in repr(rendered)
    assert "target_ref" not in repr(rendered)
    assert "MODEL-PROVENANCE-ONLY" not in repr(rendered)
    assert state.observer_calls[0]["payload"]["observation_id"] == "OBS-01"
    assert state.observer_calls[0]["payload"]["allowed_team_refs"] == ["TEM-01"]


def test_identity_state_fails_closed_on_duplicate_or_cross_team_mapping(
    identity_module: tuple[Any, FakeFrappe, SimpleNamespace],
) -> None:
    identity, fake, _state = identity_module
    fake.tables["GBOS External Identity"] = [
        _mapping(IDENTITY_PARTY, name="EID-01", review_status="Pending"),
        _mapping(IDENTITY_PARTY, name="EID-02", review_status="Pending"),
    ]
    with pytest.raises(TestBFFError) as duplicate:
        identity.get_state("OBS-01", IDENTITY_PARTY)
    assert duplicate.value.code == "internal_error"

    fake.tables["GBOS External Identity"] = [
        _mapping(
            IDENTITY_PARTY,
            name="EID-OTHER",
            review_status="Approved",
            team="TEM-02",
        )
    ]
    with pytest.raises(TestBFFError) as cross_team:
        identity.get_state("OBS-01", IDENTITY_PARTY)
    assert cross_team.value.code == "scope_mismatch"
    assert IDENTITY_PARTY not in str(cross_team.value)


@pytest.mark.parametrize(
    ("candidate_type", "expected"),
    [
        (
            "User",
            {
                "candidate_type": "User",
                "candidate_ref": "sales@example.invalid",
                "display_label": "Sales User",
            },
        ),
        (
            "Party",
            {
                "candidate_type": "Party",
                "candidate_ref": "PTY-01",
                "display_label": "Acme Same Team",
            },
        ),
        (
            "Contact",
            {
                "candidate_type": "Contact",
                "candidate_ref": "CON-01",
                "display_label": "Alice Contact",
            },
        ),
    ],
)
def test_candidate_queries_return_only_same_team_eligible_rows(
    identity_module: tuple[Any, FakeFrappe, SimpleNamespace],
    candidate_type: str,
    expected: dict[str, str],
) -> None:
    identity, _fake, _state = identity_module

    result = identity.list_candidates(
        "OBS-01",
        IDENTITY_PARTY,
        candidate_type,
        search=expected["display_label"].split()[0],
        page=1,
        page_size=20,
    )["data"]

    assert expected in result["candidates"]
    assert result["eligible_reviewers"] == [
        {
            "reviewer_ref": "reviewer@example.invalid",
            "display_label": "Identity Reviewer",
        }
    ]
    rendered = repr(result)
    assert "outsider@example.invalid" not in rendered
    assert "PTY-02" not in rendered
    assert "CON-02" not in rendered


def test_submit_for_review_uses_scoped_participant_suggestion_candidate_and_reviewer(
    identity_module: tuple[Any, FakeFrappe, SimpleNamespace],
) -> None:
    identity, _fake, state = identity_module

    response = identity.submit_for_review(
        observation_id="OBS-01",
        identity_ref=IDENTITY_PARTY,
        suggestion_key=SUGGESTION_KEY,
        selected_candidate_type="Contact",
        selected_candidate_ref="CON-01",
        assigned_reviewer="reviewer@example.invalid",
        expected_state="unresolved",
        expected_revision=0,
        idempotency_key="identity-submit-001",
    )

    assert response["data"] == {
        "status": "pending",
        "mapping_ref": "EID-01",
        "mapping_revision": 2,
        "review_case_ref": "REV-01",
        "review_case_revision": 1,
    }
    assert response["meta"]["replayed"] is False
    materialize = state.materialize_calls[0]
    assert materialize["external_subject_ref"] == IDENTITY_PARTY
    assert materialize["model_suggested_target_ref"] == "MODEL-PROVENANCE-ONLY"
    assert materialize["selected_candidate_type"] == "Contact"
    assert materialize["selected_candidate_ref"] == "CON-01"
    assert materialize["evidence_refs"] == ["EVD-01"]
    assert state.submit_calls[0]["name"] == "EID-01"
    assert state.submit_calls[0]["assigned_reviewer"] == "reviewer@example.invalid"
    assert materialize["idempotency_key"] != "identity-submit-001"
    assert state.submit_calls[0]["idempotency_key"] != materialize["idempotency_key"]


def test_submit_replays_original_and_changed_payload_conflicts(
    identity_module: tuple[Any, FakeFrappe, SimpleNamespace],
) -> None:
    identity, _fake, state = identity_module
    command = {
        "observation_id": "OBS-01",
        "identity_ref": IDENTITY_PARTY,
        "suggestion_key": SUGGESTION_KEY,
        "selected_candidate_type": "Party",
        "selected_candidate_ref": "PTY-01",
        "assigned_reviewer": "reviewer@example.invalid",
        "expected_state": "unresolved",
        "expected_revision": 0,
        "idempotency_key": "identity-submit-001",
    }

    first = identity.submit_for_review(**command)
    replay = identity.submit_for_review(**command)

    assert first["data"] == replay["data"]
    assert replay["meta"]["replayed"] is True
    assert replay["meta"]["original_request_id"] == "REQ-identity-001"
    assert len(state.materialize_calls) == 1
    with pytest.raises(TestBFFError) as conflict:
        identity.submit_for_review(**{**command, "selected_candidate_ref": "PTY-OTHER"})
    assert conflict.value.code == "idempotency_conflict"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"identity_ref": IDENTITY_USER}, "identity_mismatch"),
        ({"suggestion_key": f"suggestion:v1:{'b' * 64}"}, "suggestion_mismatch"),
        ({"selected_candidate_ref": "PTY-02"}, "candidate_ineligible"),
        ({"assigned_reviewer": "sales@example.invalid"}, "reviewer_ineligible"),
        ({"expected_revision": 1}, "revision_conflict"),
    ],
)
def test_submit_fails_closed_for_mismatched_or_stale_inputs(
    identity_module: tuple[Any, FakeFrappe, SimpleNamespace],
    change: dict[str, Any],
    code: str,
) -> None:
    identity, _fake, _state = identity_module
    command = {
        "observation_id": "OBS-01",
        "identity_ref": IDENTITY_PARTY,
        "suggestion_key": SUGGESTION_KEY,
        "selected_candidate_type": "Party",
        "selected_candidate_ref": "PTY-01",
        "assigned_reviewer": "reviewer@example.invalid",
        "expected_state": "unresolved",
        "expected_revision": 0,
        "idempotency_key": "identity-submit-001",
    }
    with pytest.raises(TestBFFError) as raised:
        identity.submit_for_review(**{**command, **change})
    assert raised.value.code == code
    assert IDENTITY_PARTY not in str(raised.value)


def test_submit_rejects_existing_pending_or_confirmed_mapping(
    identity_module: tuple[Any, FakeFrappe, SimpleNamespace],
) -> None:
    identity, fake, _state = identity_module
    command = {
        "observation_id": "OBS-01",
        "identity_ref": IDENTITY_PARTY,
        "suggestion_key": SUGGESTION_KEY,
        "selected_candidate_type": "Party",
        "selected_candidate_ref": "PTY-01",
        "assigned_reviewer": "reviewer@example.invalid",
        "expected_state": "unresolved",
        "expected_revision": 0,
        "idempotency_key": "identity-submit-001",
    }
    for status in ("Pending", "Approved"):
        fake.tables["GBOS External Identity"] = [
            _mapping(IDENTITY_PARTY, name="EID-01", review_status=status)
        ]
        with pytest.raises(TestBFFError) as raised:
            identity.submit_for_review(**command)
        assert raised.value.code == "revision_conflict"


def test_revoke_requires_admin_role_and_approved_active_current_mapping(
    identity_module: tuple[Any, FakeFrappe, SimpleNamespace],
) -> None:
    identity, fake, state = identity_module
    mapping = FakeDoc(
        _mapping(
            IDENTITY_PARTY,
            name="EID-01",
            review_status="Approved",
            revision=3,
        )
    )
    fake.tables["GBOS External Identity"] = [mapping]
    state.communication = _communication(
        identities=[
            {
                "identity_ref": IDENTITY_PARTY,
                "provider": "email",
                "status": "confirmed",
                "mapping_ref": "EID-01",
                "mapping_revision": 3,
                "target_type": "Party",
            }
        ]
    )

    with pytest.raises(TestBFFError) as denied:
        identity.revoke("OBS-01", IDENTITY_PARTY, "EID-01", 3, "identity-revoke-001")
    assert denied.value.code == "permission_denied"

    fake.session.user = "admin@example.invalid"
    response = identity.revoke("OBS-01", IDENTITY_PARTY, "EID-01", 3, "identity-revoke-001")

    assert response["data"] == {
        "status": "revoked",
        "mapping_ref": "EID-01",
        "mapping_revision": 4,
    }
    assert mapping.review_status == "Approved"
    assert mapping.business_status == "Revoked"
    assert mapping.flags.gbos_identity_status_command is True


def test_revoke_rejects_stale_revision_and_non_approved_state(
    identity_module: tuple[Any, FakeFrappe, SimpleNamespace],
) -> None:
    identity, fake, _state = identity_module
    fake.session.user = "admin@example.invalid"
    fake.tables["GBOS External Identity"] = [
        FakeDoc(_mapping(IDENTITY_PARTY, name="EID-01", review_status="Pending", revision=2))
    ]
    with pytest.raises(TestBFFError) as state_error:
        identity.revoke("OBS-01", IDENTITY_PARTY, "EID-01", 2, "identity-revoke-001")
    assert state_error.value.code == "invalid_transition"

    fake.tables["GBOS External Identity"] = [
        FakeDoc(_mapping(IDENTITY_PARTY, name="EID-01", review_status="Approved", revision=3))
    ]
    with pytest.raises(TestBFFError) as revision_error:
        identity.revoke("OBS-01", IDENTITY_PARTY, "EID-01", 2, "identity-revoke-002")
    assert revision_error.value.code == "revision_conflict"


def test_pending_review_queries_are_assigned_and_redact_external_subject_and_snapshot(
    identity_module: tuple[Any, FakeFrappe, SimpleNamespace],
) -> None:
    identity, fake, _state = identity_module
    fake.session.user = "reviewer@example.invalid"
    fake.tables["GBOS External Identity"] = [
        _mapping(IDENTITY_PARTY, name="EID-01", review_status="Pending", revision=2)
    ]
    fake.tables["GBOS Review Case"] = [
        {
            "name": "REV-01",
            "title": "Identity association review",
            "team": "TEM-01",
            "assigned_reviewer": "reviewer@example.invalid",
            "subject_doctype": "GBOS External Identity",
            "subject_name": "EID-01",
            "subject_revision": 2,
            "evidence_refs": json.dumps(["EVD-01"]),
            "policy_version": "identity-resolution-v1",
            "business_status": "Pending",
            "review_status": "Pending",
            "revision": 1,
            "modified": "2026-08-10T01:00:00+00:00",
            "subject_snapshot": json.dumps({"external_subject": IDENTITY_PARTY}),
        },
        {
            "name": "REV-OTHER",
            "title": "Identity association review",
            "team": "TEM-02",
            "assigned_reviewer": "other-reviewer@example.invalid",
            "subject_doctype": "GBOS External Identity",
            "subject_name": "EID-OTHER",
            "subject_revision": 1,
            "evidence_refs": json.dumps(["EVD-SECRET"]),
            "policy_version": "identity-resolution-v1",
            "business_status": "Pending",
            "review_status": "Pending",
            "revision": 1,
            "modified": "2026-08-10T02:00:00+00:00",
        },
    ]

    listed = identity.list_pending_reviews(page=1, page_size=20)["data"]
    detail = identity.get_pending_review("REV-01")["data"]["review"]

    assert [item["review_case_ref"] for item in listed["reviews"]] == ["REV-01"]
    assert detail["target"] == {
        "candidate_type": "Party",
        "candidate_ref": "PTY-01",
        "display_label": "Acme Same Team",
    }
    assert detail["mapping_revision"] == 2
    assert detail["evidence_refs"] == ["EVD-01"]
    assert detail["policy_version"] == "identity-resolution-v1"
    rendered = repr({"list": listed, "detail": detail})
    assert IDENTITY_PARTY not in rendered
    assert "external_subject" not in rendered
    assert "subject_snapshot" not in rendered
    with pytest.raises(TestBFFError) as denied:
        identity.get_pending_review("REV-OTHER")
    assert denied.value.code == "not_found"


@pytest.mark.parametrize(
    ("review_status", "business_status"),
    [("Approved", "Active"), ("Pending", "Revoked")],
)
def test_pending_review_detail_rejects_stale_or_decided_mapping(
    identity_module: tuple[Any, FakeFrappe, SimpleNamespace],
    review_status: str,
    business_status: str,
) -> None:
    identity, fake, _state = identity_module
    fake.session.user = "reviewer@example.invalid"
    fake.tables["GBOS External Identity"] = [
        _mapping(
            IDENTITY_PARTY,
            name="EID-01",
            review_status=review_status,
            business_status=business_status,
            revision=2,
        )
    ]
    fake.tables["GBOS Review Case"] = [
        {
            "name": "REV-01",
            "title": "Identity association review",
            "team": "TEM-01",
            "assigned_reviewer": "reviewer@example.invalid",
            "subject_doctype": "GBOS External Identity",
            "subject_name": "EID-01",
            "subject_revision": 2,
            "evidence_refs": json.dumps(["EVD-01"]),
            "policy_version": "identity-resolution-v1",
            "business_status": "Pending",
            "review_status": "Pending",
            "revision": 1,
            "modified": "2026-08-10T01:00:00+00:00",
        }
    ]

    with pytest.raises(TestBFFError) as raised:
        identity.get_pending_review("REV-01")

    assert raised.value.code == "internal_error"
    assert IDENTITY_PARTY not in str(raised.value)
