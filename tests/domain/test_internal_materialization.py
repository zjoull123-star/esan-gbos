from __future__ import annotations

import hashlib
import importlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]


class _PermissionError(Exception):
    pass


class _DoesNotExistError(Exception):
    pass


class _ValidationError(Exception):
    pass


class _DuplicateEntryError(Exception):
    pass


@dataclass
class _Member:
    user: str
    team_role: str
    enabled: int = 1


class _Doc:
    def __init__(self, runtime: _Frappe, values: dict[str, Any]) -> None:
        object.__setattr__(self, "_runtime", runtime)
        object.__setattr__(self, "_values", dict(values))
        object.__setattr__(self, "flags", SimpleNamespace())

    def __getattr__(self, name: str) -> Any:
        try:
            return self._values[name]
        except KeyError as error:
            raise AttributeError(name) from error

    def __setattr__(self, name: str, value: Any) -> None:
        self._values[name] = value

    def get(self, name: str, default: Any = None) -> Any:
        return self._values.get(name, default)

    def check_permission(self, permission_type: str) -> None:
        self._runtime.permissions.append((self.doctype, self.get("name"), permission_type))

    def insert(self, *, set_name: str | None = None) -> _Doc:
        name = set_name or self.get("name") or f"{self.doctype}-0001"
        key = (self.doctype, name)
        if key in self._runtime.docs:
            raise _DuplicateEntryError
        self.name = name
        if self.doctype in {
            "GBOS Work Item",
            "GBOS Review Case",
            "GBOS Informal Observation",
        }:
            self.revision = 1
        self._runtime.docs[key] = self
        return self

    def save(self) -> _Doc:
        self._runtime.docs[(self.doctype, self.name)] = self
        return self


class _Database:
    def __init__(self, runtime: _Frappe) -> None:
        self._runtime = runtime

    def exists(self, doctype: str, name: str) -> bool:
        return (doctype, name) in self._runtime.docs

    def get_value(self, doctype: str, name: str, fieldname: str, **kwargs: Any) -> Any:
        del kwargs
        if doctype == "User" and fieldname == "enabled":
            return int(name in self._runtime.enabled_users)
        doc = self._runtime.docs.get((doctype, name))
        return None if doc is None else doc.get(fieldname)

    def rollback(self) -> None:
        self._runtime.rollbacks += 1


class _Frappe(ModuleType):
    def __init__(self) -> None:
        super().__init__("frappe")
        self.PermissionError = _PermissionError
        self.DoesNotExistError = _DoesNotExistError
        self.ValidationError = _ValidationError
        self.DuplicateEntryError = _DuplicateEntryError
        self.session = SimpleNamespace(user="materializer@example.invalid")
        self.local = SimpleNamespace(
            site="gbos.localhost",
            response={},
            request=SimpleNamespace(
                method="POST",
                headers={
                    "Authorization": "token materializer-key:materializer-secret",
                    "X-Site-ID": "gbos.localhost",
                    "X-Processing-Purpose": "sales_follow_up",
                    "X-Request-ID": "task-0001",
                    "X-GBOS-Frappe-Auth-Ref": "agent-materializer-v1",
                },
            ),
        )
        self.conf: dict[str, Any] = {
            "gbos_agent_materialization_identities": {
                "agent-materializer-v1": {
                    "user": "materializer@example.invalid",
                    "site_id": "gbos.localhost",
                    "processing_purposes": ["sales_follow_up"],
                }
            }
        }
        self.roles = {
            "materializer@example.invalid": {
                "Agent TrustedMaterializer",
                "GBOS Admin",
            },
            "reviewer@example.invalid": {"Reviewer"},
        }
        self.enabled_users = {
            "materializer@example.invalid",
            "reviewer@example.invalid",
        }
        self.docs: dict[tuple[str, str], _Doc] = {}
        self.permissions: list[tuple[str, str | None, str]] = []
        self.rollbacks = 0
        self.db = _Database(self)

    def whitelist(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs

        def decorate(function: Any) -> Any:
            return function

        return decorate

    def get_roles(self, user: str | None = None) -> list[str]:
        return sorted(self.roles.get(user or self.session.user, set()))

    def get_doc(self, doctype: str | dict[str, Any], name: str | None = None) -> _Doc:
        if isinstance(doctype, dict):
            return _Doc(self, doctype)
        try:
            return self.docs[(doctype, str(name))]
        except KeyError as error:
            raise _DoesNotExistError from error

    @staticmethod
    def parse_json(value: str) -> Any:
        return json.loads(value)


@pytest.fixture
def materialization_api() -> tuple[Any, _Frappe]:
    fake = _Frappe()
    original = sys.modules.get("frappe")
    sys.modules["frappe"] = fake
    sys.modules.pop("esan_gbos.api.internal.materialization", None)
    module = importlib.import_module("esan_gbos.api.internal.materialization")
    yield module, fake
    sys.modules.pop("esan_gbos.api.internal.materialization", None)
    if original is None:
        sys.modules.pop("frappe", None)
    else:
        sys.modules["frappe"] = original


def _bound_payload(**values: Any) -> dict[str, Any]:
    return {
        "site_id": "gbos.localhost",
        "processing_purpose": "sales_follow_up",
        "request_id": "task-0001",
        "auth_ref": "agent-materializer-v1",
        **values,
    }


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _subject(fake: _Frappe, *, revision: int = 3) -> _Doc:
    subject = _Doc(
        fake,
        {
            "doctype": "GBOS Work Item",
            "name": "WRK-0001",
            "title": "Follow up",
            "team": "TEM-0001",
            "assigned_to": None,
            "priority": "Medium",
            "due_date": None,
            "reference_doctype": None,
            "reference_name": None,
            "blocked_reason": None,
            "origin": "Manual",
            "origin_reference": None,
            "business_status": "Open",
            "review_status": "Pending",
            "revision": revision,
        },
    )
    team = _Doc(
        fake,
        {
            "doctype": "GBOS Team",
            "name": "TEM-0001",
            "members": [_Member("reviewer@example.invalid", "Reviewer")],
        },
    )
    fake.docs[(subject.doctype, subject.name)] = subject
    fake.docs[(team.doctype, team.name)] = team
    return subject


def test_internal_endpoint_is_503_when_identity_mapping_is_not_configured(
    materialization_api: tuple[Any, _Frappe],
) -> None:
    api, fake = materialization_api
    fake.conf.clear()

    response = api.resolve_context(
        _bound_payload(
            task_id="task-0001",
            proposal_id="proposal-0001",
            subject_type="GBOS Work Item",
            subject_ref="WRK-0001",
            subject_revision=3,
        )
    )

    assert fake.local.response["http_status_code"] == 503
    assert response["error"] == {"code": "service_unconfigured"}
    assert fake.docs == {}


def test_internal_post_boundary_never_enables_guest_or_ignores_permissions() -> None:
    source = (
        ROOT / "apps" / "esan_gbos" / "esan_gbos" / "api" / "internal" / "materialization.py"
    ).read_text(encoding="utf-8")

    assert "allow_guest" not in source
    assert "ignore_permissions" not in source
    assert source.count('@frappe.whitelist(methods=["POST"])') == 2
    assert '"doctype": "Integration Request"' in source


def test_resolve_context_requires_token_auth_and_all_bound_headers(
    materialization_api: tuple[Any, _Frappe],
) -> None:
    api, fake = materialization_api
    _subject(fake)
    fake.local.request.headers["Authorization"] = "Bearer not-a-frappe-api-token"

    response = api.resolve_context(
        _bound_payload(
            task_id="task-0001",
            proposal_id="proposal-0001",
            subject_type="GBOS Work Item",
            subject_ref="WRK-0001",
            subject_revision=3,
        )
    )

    assert fake.local.response["http_status_code"] == 401
    assert response["error"] == {"code": "authentication_required"}


def test_resolve_context_rejects_header_and_body_scope_mismatch(
    materialization_api: tuple[Any, _Frappe],
) -> None:
    api, fake = materialization_api
    _subject(fake)
    fake.local.request.headers["X-Processing-Purpose"] = "different_purpose"

    response = api.resolve_context(
        _bound_payload(
            task_id="task-0001",
            proposal_id="proposal-0001",
            subject_type="GBOS Work Item",
            subject_ref="WRK-0001",
            subject_revision=3,
        )
    )

    assert fake.local.response["http_status_code"] == 403
    assert response["error"] == {"code": "identity_scope_mismatch"}


def test_resolve_context_returns_pinned_snapshot_and_unique_dual_role_reviewer(
    materialization_api: tuple[Any, _Frappe],
) -> None:
    api, fake = materialization_api
    subject = _subject(fake)

    response = api.resolve_context(
        _bound_payload(
            task_id="task-0001",
            proposal_id="proposal-0001",
            subject_type="GBOS Work Item",
            subject_ref="WRK-0001",
            subject_revision=3,
        )
    )

    assert response["site_id"] == "gbos.localhost"
    assert response["request_id"] == "task-0001"
    assert response["team"] == "TEM-0001"
    assert response["assigned_reviewer"] == "reviewer@example.invalid"
    assert response["subject_snapshot"]["doctype"] == "GBOS Work Item"
    assert response["subject_snapshot"]["name"] == subject.name
    assert response["subject_snapshot"]["revision"] == 3
    assert "message_body" not in repr(response)
    assert len(response["subject_payload_digest"]) == 64
    assert ("GBOS Work Item", "WRK-0001", "read") in fake.permissions
    assert ("GBOS Team", "TEM-0001", "read") in fake.permissions


@pytest.mark.parametrize("reviewers", [[], ["reviewer-2@example.invalid"]])
def test_resolve_context_fails_closed_for_zero_or_multiple_qualified_reviewers(
    materialization_api: tuple[Any, _Frappe],
    reviewers: list[str],
) -> None:
    api, fake = materialization_api
    _subject(fake)
    team = fake.docs[("GBOS Team", "TEM-0001")]
    if reviewers:
        for user in reviewers:
            fake.roles[user] = {"Reviewer"}
            fake.enabled_users.add(user)
            team.members.append(_Member(user, "Reviewer"))
    else:
        team.members.clear()

    response = api.resolve_context(
        _bound_payload(
            task_id="task-0001",
            proposal_id="proposal-0001",
            subject_type="GBOS Work Item",
            subject_ref="WRK-0001",
            subject_revision=3,
        )
    )

    assert fake.local.response["http_status_code"] == 409
    assert response["error"] == {"code": "reviewer_scope_ambiguous"}


def test_apply_draft_creates_only_closed_work_item_and_persists_replay_receipt(
    materialization_api: tuple[Any, _Frappe],
) -> None:
    api, fake = materialization_api
    _subject(fake)
    fake.local.request.headers["X-Request-ID"] = "materialization-0001"
    values = {
        "title": "Prepare follow-up",
        "team": "TEM-0001",
        "reference_doctype": "GBOS Work Item",
        "reference_name": "WRK-0001",
        "origin": "AI",
        "origin_reference": "proposal-0001",
        "business_status": "Open",
        "review_status": "AI Draft",
    }
    intent = {"operation": "create", "doctype": "GBOS Work Item", "values": values}
    digest = api.canonical_request_digest(intent)
    payload = _bound_payload(
        request_id="materialization-0001",
        request_digest=digest,
        intent=intent,
    )

    first = api.apply_draft(payload)
    replay = api.apply_draft(payload)

    assert replay == first
    assert first["doctype"] == "GBOS Work Item"
    assert first["request_id"] == "materialization-0001"
    assert first["request_digest"] == digest
    assert first["revision"] == 1
    assert (
        len([key for key in fake.docs if key[0] == "GBOS Work Item" and key[1] != "WRK-0001"]) == 1
    )
    audits = [doc for (doctype, _), doc in fake.docs.items() if doctype == "Integration Request"]
    assert len(audits) == 1
    assert json.loads(audits[0].data)["request_digest"] == digest


def test_apply_draft_rejects_same_request_id_with_different_digest(
    materialization_api: tuple[Any, _Frappe],
) -> None:
    api, fake = materialization_api
    _subject(fake)
    fake.local.request.headers["X-Request-ID"] = "materialization-0001"
    values = {
        "title": "Prepare follow-up",
        "team": "TEM-0001",
        "reference_doctype": "GBOS Work Item",
        "reference_name": "WRK-0001",
        "origin": "AI",
        "origin_reference": "proposal-0001",
        "business_status": "Open",
        "review_status": "AI Draft",
    }
    intent = {"operation": "create", "doctype": "GBOS Work Item", "values": values}
    first_payload = _bound_payload(
        request_id="materialization-0001",
        request_digest=api.canonical_request_digest(intent),
        intent=intent,
    )
    api.apply_draft(first_payload)
    changed_intent = {
        **intent,
        "values": {**values, "title": "Changed title"},
    }

    response = api.apply_draft(
        {
            **first_payload,
            "intent": changed_intent,
            "request_digest": api.canonical_request_digest(changed_intent),
        }
    )

    assert fake.local.response["http_status_code"] == 409
    assert response["error"] == {"code": "idempotency_conflict"}


def test_apply_draft_creates_closed_informal_observation_with_exact_model(
    materialization_api: tuple[Any, _Frappe],
) -> None:
    api, fake = materialization_api
    _subject(fake)
    fake.local.request.headers["X-Request-ID"] = "materialization-observation-0001"
    intent = {
        "operation": "create",
        "doctype": "GBOS Informal Observation",
        "values": {
            "subject": "Communication observation",
            "summary_zh": "受控摘要",
            "team": "TEM-0001",
            "evidence_refs": [{"evidence_ref": "evidence-1", "locator_ref": "evidence-1"}],
            "model_name": "deepseek-v4-flash",
            "model_version": "deepseek-v4-flash",
            "is_official_metric": False,
            "origin": "AI",
            "origin_reference": "proposal-observation-0001",
            "review_status": "AI Draft",
        },
    }

    response = api.apply_draft(
        _bound_payload(
            request_id="materialization-observation-0001",
            request_digest=api.canonical_request_digest(intent),
            intent=intent,
        )
    )

    assert response["doctype"] == "GBOS Informal Observation"
    observation = fake.docs[(response["doctype"], response["name"])]
    assert observation.is_official_metric is False
    assert observation.model_name == "deepseek-v4-flash"
    assert observation.review_status == "AI Draft"


def test_apply_draft_creates_closed_revision_pinned_review_case(
    materialization_api: tuple[Any, _Frappe],
) -> None:
    api, fake = materialization_api
    _subject(fake)
    context = api.resolve_context(
        _bound_payload(
            task_id="task-0001",
            proposal_id="proposal-review-0001",
            subject_type="GBOS Work Item",
            subject_ref="WRK-0001",
            subject_revision=3,
        )
    )
    fake.local.request.headers["X-Request-ID"] = "materialization-review-0001"
    evidence = ["evidence-1"]
    case_payload = {
        "title": "Review follow-up",
        "team": context["team"],
        "assigned_reviewer": context["assigned_reviewer"],
        "subject_doctype": context["subject_type"],
        "subject_name": context["subject_ref"],
        "subject_revision": context["subject_revision"],
        "subject_payload_sha256": context["subject_payload_digest"],
        "subject_snapshot": context["subject_snapshot"],
        "evidence_refs": evidence,
        "policy_version": "action-guard-v1",
    }
    intent = {
        "operation": "create",
        "doctype": "GBOS Review Case",
        "values": {
            **case_payload,
            "subject_snapshot": json.dumps(
                context["subject_snapshot"],
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ),
            "evidence_refs": json.dumps(evidence, separators=(",", ":")),
            "case_payload_sha256": _canonical_digest(case_payload),
            "origin": "AI",
            "origin_reference": "proposal-review-0001",
            "business_status": "Pending",
            "review_status": "AI Draft",
        },
    }

    response = api.apply_draft(
        _bound_payload(
            request_id="materialization-review-0001",
            request_digest=api.canonical_request_digest(intent),
            intent=intent,
        )
    )
    fake.docs[("GBOS Work Item", "WRK-0001")].revision = 4
    replay = api.apply_draft(
        _bound_payload(
            request_id="materialization-review-0001",
            request_digest=api.canonical_request_digest(intent),
            intent=intent,
        )
    )

    assert response["doctype"] == "GBOS Review Case"
    assert replay == response
    case = fake.docs[(response["doctype"], response["name"])]
    assert case.assigned_reviewer == "reviewer@example.invalid"
    assert case.subject_revision == 3
    assert case.subject_payload_sha256 == context["subject_payload_digest"]


@pytest.mark.parametrize(
    "intent",
    [
        {
            "operation": "create",
            "doctype": "CRM Deal",
            "values": {"origin": "AI", "review_status": "AI Draft"},
        },
        {
            "operation": "create",
            "doctype": "GBOS Work Item",
            "values": {
                "title": "Prepare follow-up",
                "team": "TEM-0001",
                "reference_doctype": "Sales Order",
                "reference_name": "SO-0001",
                "origin": "AI",
                "origin_reference": "proposal-0001",
                "business_status": "Open",
                "review_status": "AI Draft",
            },
        },
        {
            "operation": "create",
            "doctype": "GBOS Informal Observation",
            "values": {
                "subject": "Observation",
                "summary_zh": "Controlled summary",
                "team": "TEM-0001",
                "evidence_refs": [{"evidence_ref": "evidence-1", "locator_ref": "evidence-1"}],
                "model_name": "different-model",
                "model_version": "different-model",
                "is_official_metric": False,
                "origin": "AI",
                "origin_reference": "proposal-0001",
                "review_status": "AI Draft",
            },
        },
    ],
)
def test_apply_draft_rejects_formal_arbitrary_or_wrong_model_intents(
    materialization_api: tuple[Any, _Frappe],
    intent: dict[str, Any],
) -> None:
    api, fake = materialization_api
    _subject(fake)
    fake.local.request.headers["X-Request-ID"] = "materialization-0001"

    response = api.apply_draft(
        _bound_payload(
            request_id="materialization-0001",
            request_digest=api.canonical_request_digest(intent),
            intent=intent,
        )
    )

    assert fake.local.response["http_status_code"] == 422
    assert response["error"] == {"code": "invalid_intent"}
    assert not any(doctype in {"CRM Deal", "Sales Order"} for doctype, _name in fake.docs)


def test_apply_draft_submit_only_advances_existing_ai_draft_without_permission_bypass(
    materialization_api: tuple[Any, _Frappe],
) -> None:
    api, fake = materialization_api
    draft = _Doc(
        fake,
        {
            "doctype": "GBOS Work Item",
            "name": "WRK-AI-0001",
            "origin": "AI",
            "business_status": "Open",
            "review_status": "AI Draft",
            "revision": 1,
        },
    )
    fake.docs[(draft.doctype, draft.name)] = draft
    fake.local.request.headers["X-Request-ID"] = "materialization-submit-0001"
    intent = {
        "operation": "submit",
        "doctype": "GBOS Work Item",
        "values": {
            "name": "WRK-AI-0001",
            "origin": "AI",
            "review_status": "Pending",
        },
    }

    response = api.apply_draft(
        _bound_payload(
            request_id="materialization-submit-0001",
            request_digest=api.canonical_request_digest(intent),
            intent=intent,
        )
    )

    assert response["name"] == "WRK-AI-0001"
    assert draft.review_status == "Pending"
    assert draft.business_status == "Open"
    assert draft.flags.gbos_ai_draft_command is True
    assert ("GBOS Work Item", "WRK-AI-0001", "write") in fake.permissions
