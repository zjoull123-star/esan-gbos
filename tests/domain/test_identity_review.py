from __future__ import annotations

import hashlib
import importlib
import json
import sys
from collections.abc import Generator
from copy import deepcopy
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]
SUBJECT = "extid:v1:email:OpaqueSubject01"
SENSITIVE_SUBJECT = "extid:v1:email:SensitiveSubjectSentinel"
SENSITIVE_TARGET = "sensitive-target@example.invalid"
SUGGESTION_KEY = f"suggestion:v1:{'a' * 64}"


class _PermissionError(Exception):
    pass


class _DuplicateEntryError(Exception):
    pass


class _ResponseLost(Exception):
    pass


class _Doc:
    def __init__(self, runtime: _Frappe, values: dict[str, Any]) -> None:
        object.__setattr__(self, "runtime", runtime)
        object.__setattr__(self, "values", dict(values))
        object.__setattr__(self, "flags", SimpleNamespace())

    def __getattr__(self, fieldname: str) -> Any:
        try:
            return self.values[fieldname]
        except KeyError as error:
            raise AttributeError(fieldname) from error

    def __setattr__(self, fieldname: str, value: Any) -> None:
        self.values[fieldname] = value

    def get(self, fieldname: str, default: Any = None) -> Any:
        return self.values.get(fieldname, default)

    def insert(self, *, ignore_permissions: bool) -> _Doc:
        assert ignore_permissions is True
        doctype = str(self.doctype)
        name = self.get("name") or self.runtime.next_name(doctype)
        key = (doctype, name)
        if key in self.runtime.docs:
            raise _DuplicateEntryError
        self.name = name
        self.revision = 1
        self.runtime.docs[key] = self
        if doctype == "GBOS Review Case":
            self.runtime.case_status_history.append((self.review_status, self.business_status))
        return self

    def save(self, *, ignore_permissions: bool) -> _Doc:
        assert ignore_permissions is True
        if self.doctype == "GBOS External Identity":
            assert getattr(self.flags, "gbos_ai_draft_command", False) or getattr(
                self.flags, "gbos_ai_reopen_command", False
            )
        if self.doctype == "GBOS Review Case":
            assert getattr(self.flags, "gbos_review_command", False)
            assert getattr(self.flags, "gbos_ai_draft_command", False)
        self.revision = int(self.revision) + 1
        self.runtime.docs[(self.doctype, self.name)] = self
        if self.doctype == "GBOS Review Case":
            self.runtime.case_status_history.append((self.review_status, self.business_status))
        return self


class _Database:
    def __init__(self, runtime: _Frappe) -> None:
        self.runtime = runtime

    def exists(self, doctype: str, filters: str | dict[str, Any]) -> bool:
        if isinstance(filters, str):
            return (doctype, filters) in self.runtime.docs
        if doctype == "GBOS Team Member":
            return (
                str(filters.get("parent")),
                str(filters.get("user")),
            ) in self.runtime.team_members and filters.get("enabled") == 1
        return any(
            doc.doctype == doctype
            and all(doc.get(fieldname) == value for fieldname, value in filters.items())
            for doc in self.runtime.docs.values()
        )

    def get_value(
        self,
        doctype: str,
        name: str,
        fieldname: str,
        **kwargs: Any,
    ) -> Any:
        del kwargs
        if (doctype, fieldname) == ("User", "enabled"):
            return int(name in self.runtime.enabled_users)
        doc = self.runtime.docs.get((doctype, name))
        return None if doc is None else doc.get(fieldname)


class _Frappe(ModuleType):
    def __init__(self) -> None:
        super().__init__("frappe")
        self.PermissionError = _PermissionError
        self.DuplicateEntryError = _DuplicateEntryError
        self.session = SimpleNamespace(user="sales@example.invalid")
        self.roles = {
            "sales@example.invalid": {"Sales User"},
            "reviewer@example.invalid": {"Reviewer"},
            "wrong-reviewer@example.invalid": {"Reviewer"},
            "admin@example.invalid": {"GBOS Admin"},
        }
        self.enabled_users = {
            "sales@example.invalid",
            "user-target@example.invalid",
            "reviewer@example.invalid",
            "wrong-reviewer@example.invalid",
            "admin@example.invalid",
        }
        self.team_members = {
            ("TEM-01", "sales@example.invalid"),
            ("TEM-01", "user-target@example.invalid"),
            ("TEM-01", "reviewer@example.invalid"),
            ("TEM-02", "wrong-reviewer@example.invalid"),
        }
        self.docs: dict[tuple[str, str], _Doc] = {}
        self.idempotency: dict[str, tuple[str, dict[str, Any], str]] = {}
        self.idempotent_executions: dict[str, int] = {}
        self.fail_after_store = False
        self.case_status_history: list[tuple[str, str]] = []
        self._counters: dict[str, int] = {}
        self.db = _Database(self)
        self._add_doc("GBOS Party Profile", "PTY-01", team="TEM-01", contact="CON-01")
        self._add_doc("GBOS Party Profile", "PTY-02", team="TEM-02", contact="CON-02")

    def _add_doc(self, doctype: str, name: str, **values: Any) -> _Doc:
        doc = _Doc(self, {"doctype": doctype, "name": name, **values})
        self.docs[(doctype, name)] = doc
        return doc

    def next_name(self, doctype: str) -> str:
        prefixes = {"GBOS External Identity": "EID", "GBOS Review Case": "REV"}
        self._counters[doctype] = self._counters.get(doctype, 0) + 1
        return f"{prefixes[doctype]}-{self._counters[doctype]:04d}"

    def get_roles(self, user: str | None = None) -> list[str]:
        return sorted(self.roles.get(user or self.session.user, set()))

    def get_doc(
        self,
        doctype: str | dict[str, Any],
        name: str | None = None,
        **kwargs: Any,
    ) -> _Doc:
        del kwargs
        if isinstance(doctype, dict):
            return _Doc(self, doctype)
        return self.docs[(doctype, str(name))]

    def get_all(
        self,
        doctype: str,
        *,
        filters: dict[str, Any],
        fields: list[str],
        limit_page_length: int,
    ) -> list[dict[str, Any]]:
        assert doctype == "GBOS Party Profile"
        assert fields == ["name"]
        rows = [
            {"name": doc.name}
            for doc in self.docs.values()
            if doc.doctype == doctype
            and all(doc.get(fieldname) == value for fieldname, value in filters.items())
        ]
        return rows[:limit_page_length]


def _canonical_hash(value: dict[str, Any]) -> str:
    serialized = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def _validate_external_subject(provider: object, subject: object) -> None:
    prefix = f"extid:v1:{provider}:"
    if provider not in {"email", "wecom", "whatsapp", "phone", "manual_import"}:
        raise ValueError("identity provider is not allowed")
    if not isinstance(subject, str) or not subject.startswith(prefix) or len(subject) > 160:
        raise ValueError("external subject reference is invalid")


def _snapshot(doc: _Doc) -> dict[str, Any]:
    return {
        "doctype": doc.doctype,
        "name": doc.name,
        "revision": int(doc.revision),
        "team": doc.team,
        "identity_provider": doc.identity_provider,
        "external_subject": doc.external_subject,
        "identity_type": doc.identity_type,
        "user": doc.get("user"),
        "party_profile": doc.get("party_profile"),
        "origin": doc.origin,
        "business_status": doc.business_status,
        "review_status": doc.review_status,
    }


def _case_payload(case: _Doc) -> dict[str, Any]:
    return {
        "title": case.title,
        "team": case.team,
        "assigned_reviewer": case.assigned_reviewer,
        "subject_doctype": case.subject_doctype,
        "subject_name": case.subject_name,
        "subject_revision": case.subject_revision,
        "subject_payload_sha256": case.subject_payload_sha256,
        "subject_snapshot": json.loads(case.subject_snapshot),
        "evidence_refs": json.loads(case.evidence_refs),
        "policy_version": case.policy_version,
    }


@pytest.fixture
def identity_review(monkeypatch: pytest.MonkeyPatch) -> Generator[tuple[Any, _Frappe]]:
    fake = _Frappe()
    external_module = ModuleType(
        "esan_gbos.gbos.doctype.gbos_external_identity.gbos_external_identity"
    )
    external_module.validate_external_subject = _validate_external_subject  # type: ignore[attr-defined]
    review_module = ModuleType("esan_gbos.gbos.doctype.gbos_review_case.gbos_review_case")
    review_module.build_subject_snapshot = _snapshot  # type: ignore[attr-defined]
    review_module.build_case_payload = _case_payload  # type: ignore[attr-defined]
    review_dto = ModuleType("esan_gbos.domain.review_dto")
    review_dto.canonical_payload_hash = _canonical_hash  # type: ignore[attr-defined]

    def run_idempotent(
        command: str,
        key: str,
        payload: dict[str, Any],
        execute: Any,
        *,
        api_version: str,
    ) -> tuple[dict[str, Any], bool, str]:
        assert api_version == "domain"
        digest = _canonical_hash(
            {"command": command, "actor": fake.session.user, "payload": payload}
        )
        existing = fake.idempotency.get(key)
        if existing is not None:
            if existing[0] != digest:
                raise ValueError("idempotency_conflict")
            return deepcopy(existing[1]), True, existing[2]
        fake.idempotent_executions[command] = fake.idempotent_executions.get(command, 0) + 1
        result = execute()
        fake.idempotency[key] = (digest, deepcopy(result), str(payload["request_id"]))
        if fake.fail_after_store:
            fake.fail_after_store = False
            raise _ResponseLost("response lost")
        return result, False, str(payload["request_id"])

    audit_module = ModuleType("esan_gbos.api.v1.audit")
    audit_module.run_idempotent = run_idempotent  # type: ignore[attr-defined]
    modules = {
        "frappe": fake,
        external_module.__name__: external_module,
        review_module.__name__: review_module,
        review_dto.__name__: review_dto,
        audit_module.__name__: audit_module,
    }
    originals = {name: sys.modules.get(name) for name in modules}
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    sys.modules.pop("esan_gbos.domain.identity_review", None)
    module = importlib.import_module("esan_gbos.domain.identity_review")
    yield module, fake
    sys.modules.pop("esan_gbos.domain.identity_review", None)
    for name, original in originals.items():
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original


def _materialize_request(**overrides: Any) -> dict[str, Any]:
    request = {
        "team": "TEM-01",
        "identity_provider": "email",
        "external_subject_ref": SUBJECT,
        "observation_id": "OBS-0001",
        "suggestion_key": SUGGESTION_KEY,
        "association_type": "user",
        "model_suggested_target_ref": "model-user-hint",
        "selected_candidate_type": "User",
        "selected_candidate_ref": "user-target@example.invalid",
        "evidence_refs": ["EVD-0001"],
        "policy_version": "identity-association-v1",
        "idempotency_key": "materialize-0001",
        "request_id": "REQ-0001",
    }
    request.update(overrides)
    return request


def _submit_request(name: str, **overrides: Any) -> dict[str, Any]:
    request = {
        "name": name,
        "team": "TEM-01",
        "observation_id": "OBS-0001",
        "suggestion_key": SUGGESTION_KEY,
        "association_type": "user",
        "model_suggested_target_ref": "model-user-hint",
        "selected_candidate_type": "User",
        "selected_candidate_ref": "user-target@example.invalid",
        "assigned_reviewer": "reviewer@example.invalid",
        "expected_revision": 1,
        "evidence_refs": ["EVD-0001"],
        "policy_version": "identity-association-v1",
        "idempotency_key": "submit-review-0001",
        "request_id": "REQ-0002",
    }
    request.update(overrides)
    return request


def _rematerialize_request(name: str, **overrides: Any) -> dict[str, Any]:
    request = {
        **_materialize_request(),
        "name": name,
        "expected_revision": 3,
        "idempotency_key": "rematerialize-0001",
        "request_id": "REQ-0003",
    }
    request.update(overrides)
    return request


@pytest.mark.parametrize(
    (
        "association_type",
        "model_ref",
        "selected_type",
        "selected_ref",
        "identity_type",
        "user",
        "party",
    ),
    (
        (
            "user",
            "model-user-hint",
            "User",
            "user-target@example.invalid",
            "User",
            "user-target@example.invalid",
            None,
        ),
        ("party", "model-party-hint", "Party", "PTY-01", "Party", None, "PTY-01"),
        ("contact", "model-contact-hint", "Contact", "CON-01", "Party", None, "PTY-01"),
    ),
)
def test_materializes_only_exact_same_team_frappe_candidates_as_ai_draft(
    identity_review: tuple[Any, _Frappe],
    association_type: str,
    model_ref: str,
    selected_type: str,
    selected_ref: str,
    identity_type: str,
    user: str | None,
    party: str | None,
) -> None:
    service, fake = identity_review

    result = service.materialize_association_suggestion(
        _materialize_request(
            association_type=association_type,
            model_suggested_target_ref=model_ref,
            selected_candidate_type=selected_type,
            selected_candidate_ref=selected_ref,
        )
    )

    mapping = fake.docs[("GBOS External Identity", result["name"])]
    assert result == {
        "doctype": "GBOS External Identity",
        "name": mapping.name,
        "review_status": "AI Draft",
        "revision": 1,
        "request_id": "REQ-0001",
    }
    assert mapping.team == "TEM-01"
    assert mapping.identity_provider == "email"
    assert mapping.external_subject == SUBJECT
    assert mapping.identity_type == identity_type
    assert mapping.get("user") == user
    assert mapping.get("party_profile") == party
    assert mapping.origin == "AI"
    assert mapping.review_status == "AI Draft"
    assert mapping.business_status == "Active"
    provenance = {
        "team": "TEM-01",
        "observation_id": "OBS-0001",
        "suggestion_key": SUGGESTION_KEY,
        "association_type": association_type,
        "model_suggested_target_ref": model_ref,
        "selected_candidate_type": selected_type,
        "selected_candidate_ref": selected_ref,
        "evidence_refs": ["EVD-0001"],
        "policy_version": "identity-association-v1",
    }
    assert mapping.origin_reference == f"association:v1:{_canonical_hash(provenance)}"


@pytest.mark.parametrize(
    "overrides",
    (
        {"selected_candidate_ref": SENSITIVE_TARGET},
        {"selected_candidate_ref": "disabled@example.invalid"},
        {"selected_candidate_type": "Party", "selected_candidate_ref": "PTY-02"},
        {"selected_candidate_type": "Contact", "selected_candidate_ref": "CON-02"},
        {"association_type": "channel"},
    ),
)
def test_model_target_is_never_authoritative_and_failures_are_redacted(
    identity_review: tuple[Any, _Frappe],
    overrides: dict[str, Any],
) -> None:
    service, fake = identity_review

    with pytest.raises(service.IdentityReviewError) as raised:
        service.materialize_association_suggestion(_materialize_request(**overrides))

    assert SUBJECT not in repr(raised.value)
    assert SENSITIVE_TARGET not in repr(raised.value)
    assert not any(key[0] == "GBOS External Identity" for key in fake.docs)


def test_arbitrary_model_hint_never_becomes_the_selected_authoritative_candidate(
    identity_review: tuple[Any, _Frappe],
) -> None:
    service, fake = identity_review

    result = service.materialize_association_suggestion(
        _materialize_request(model_suggested_target_ref=SENSITIVE_TARGET)
    )

    mapping = fake.docs[("GBOS External Identity", result["name"])]
    assert mapping.user == "user-target@example.invalid"
    assert SENSITIVE_TARGET not in repr(result)


def test_contact_requires_exactly_one_same_team_party_profile(
    identity_review: tuple[Any, _Frappe],
) -> None:
    service, fake = identity_review
    fake._add_doc("GBOS Party Profile", "PTY-03", team="TEM-01", contact="CON-01")

    with pytest.raises(service.IdentityReviewError, match="candidate"):
        service.materialize_association_suggestion(
            _materialize_request(
                association_type="contact",
                model_suggested_target_ref="model-contact-hint",
                selected_candidate_type="Contact",
                selected_candidate_ref="CON-01",
            )
        )


def test_materialization_replays_original_and_changed_payload_or_candidate_conflicts(
    identity_review: tuple[Any, _Frappe],
) -> None:
    service, fake = identity_review
    request = _materialize_request()

    first = service.materialize_association_suggestion(request)
    replay = service.materialize_association_suggestion(dict(request))

    assert replay == first
    assert fake.idempotent_executions["identity_review.materialize"] == 1
    assert len([key for key in fake.docs if key[0] == "GBOS External Identity"]) == 1
    with pytest.raises(ValueError, match="idempotency_conflict"):
        service.materialize_association_suggestion(
            {**request, "policy_version": "identity-association-v2"}
        )
    with pytest.raises(service.IdentityReviewError, match="candidate"):
        service.materialize_association_suggestion(
            {**request, "idempotency_key": "materialize-0002", "request_id": "REQ-0003"}
        )


def test_materialization_response_loss_retry_returns_the_original_draft(
    identity_review: tuple[Any, _Frappe],
) -> None:
    service, fake = identity_review
    request = _materialize_request()
    fake.fail_after_store = True

    with pytest.raises(_ResponseLost):
        service.materialize_association_suggestion(request)
    replay = service.materialize_association_suggestion(request)

    assert replay["name"] == "EID-0001"
    assert fake.idempotent_executions["identity_review.materialize"] == 1


def test_rejected_mapping_is_corrected_in_place_with_monotonic_revision_and_history_preserved(
    identity_review: tuple[Any, _Frappe],
) -> None:
    service, fake = identity_review
    first_mapping = service.materialize_association_suggestion(_materialize_request())
    first_submit_request = _submit_request(first_mapping["name"])
    first_case = service.submit_for_review(first_submit_request)
    mapping = fake.docs[("GBOS External Identity", first_mapping["name"])]
    rejected_case = fake.docs[("GBOS Review Case", first_case["name"])]
    mapping.review_status = "Rejected"
    mapping.revision = 3
    rejected_case.review_status = "Rejected"
    rejected_case.business_status = "Rejected"
    rejected_history = deepcopy(rejected_case.values)

    corrected = service.rematerialize_rejected_association_suggestion(
        _rematerialize_request(
            mapping.name,
            association_type="party",
            model_suggested_target_ref="model-party-hint",
            selected_candidate_type="Party",
            selected_candidate_ref="PTY-01",
        )
    )

    assert corrected == {
        "doctype": "GBOS External Identity",
        "name": mapping.name,
        "review_status": "AI Draft",
        "revision": 4,
        "request_id": "REQ-0003",
    }
    assert mapping.identity_provider == "email"
    assert mapping.external_subject == SUBJECT
    assert mapping.team == "TEM-01"
    assert mapping.identity_type == "Party"
    assert mapping.user is None
    assert mapping.party_profile == "PTY-01"
    assert mapping.flags.gbos_ai_reopen_command is True
    assert rejected_case.values == rejected_history

    second_case = service.submit_for_review(
        _submit_request(
            mapping.name,
            association_type="party",
            model_suggested_target_ref="model-party-hint",
            selected_candidate_type="Party",
            selected_candidate_ref="PTY-01",
            expected_revision=4,
            idempotency_key="submit-review-0002",
            request_id="REQ-0004",
        )
    )

    assert second_case["name"] == "REV-0002"
    assert second_case["subject_name"] == mapping.name
    assert second_case["subject_revision"] == 5
    assert mapping.review_status == "Pending"
    assert mapping.revision == 5
    assert rejected_case.values == rejected_history
    assert rejected_case.subject_revision == 2
    assert rejected_case.subject_revision != mapping.revision
    assert len([key for key in fake.docs if key[0] == "GBOS External Identity"]) == 1
    assert len([key for key in fake.docs if key[0] == "GBOS Review Case"]) == 2

    old_submit_replay = service.submit_for_review(first_submit_request)
    assert old_submit_replay["name"] == first_case["name"]
    assert mapping.review_status == "Pending"
    assert mapping.party_profile == "PTY-01"
    mapping.review_status = "Approved"
    mapping.revision = 6
    assert mapping.review_status == "Approved"
    assert mapping.revision > second_case["subject_revision"]


@pytest.mark.parametrize(
    "overrides",
    (
        {"expected_revision": 2},
        {"team": "TEM-02"},
        {"identity_provider": "wecom"},
        {"external_subject_ref": "extid:v1:email:OtherOpaque"},
        {"selected_candidate_ref": "disabled@example.invalid"},
        {"selected_candidate_type": "Party", "selected_candidate_ref": "PTY-02"},
    ),
)
def test_rejected_rematerialization_fails_closed_without_mutating_mapping(
    identity_review: tuple[Any, _Frappe],
    overrides: dict[str, Any],
) -> None:
    service, fake = identity_review
    first = service.materialize_association_suggestion(_materialize_request())
    mapping = fake.docs[("GBOS External Identity", first["name"])]
    mapping.review_status = "Rejected"
    mapping.revision = 3
    before = deepcopy(mapping.values)

    with pytest.raises((service.IdentityReviewError, _PermissionError)):
        service.rematerialize_rejected_association_suggestion(
            _rematerialize_request(mapping.name, **overrides)
        )

    assert mapping.values == before
    assert not any(key[0] == "GBOS Review Case" for key in fake.docs)


def test_rejected_rematerialization_replays_after_response_loss_without_second_mapping(
    identity_review: tuple[Any, _Frappe],
) -> None:
    service, fake = identity_review
    first = service.materialize_association_suggestion(_materialize_request())
    mapping = fake.docs[("GBOS External Identity", first["name"])]
    mapping.review_status = "Rejected"
    mapping.revision = 3
    request = _rematerialize_request(mapping.name)
    fake.fail_after_store = True

    with pytest.raises(_ResponseLost):
        service.rematerialize_rejected_association_suggestion(request)
    replay = service.rematerialize_rejected_association_suggestion(request)

    assert replay["name"] == mapping.name
    assert replay["revision"] == 4
    assert fake.idempotent_executions["identity_review.rematerialize"] == 1
    assert len([key for key in fake.docs if key[0] == "GBOS External Identity"]) == 1
    with pytest.raises(ValueError, match="idempotency_conflict"):
        service.rematerialize_rejected_association_suggestion(
            {**request, "policy_version": "identity-association-v2"}
        )


def test_submit_revalidates_target_and_creates_one_revision_pinned_pending_case(
    identity_review: tuple[Any, _Frappe],
) -> None:
    service, fake = identity_review
    mapping_result = service.materialize_association_suggestion(_materialize_request())
    crm_before = {
        key: deepcopy(doc.values)
        for key, doc in fake.docs.items()
        if key[0] in {"GBOS Party Profile", "Contact"}
    }

    result = service.submit_for_review(_submit_request(mapping_result["name"]))

    mapping = fake.docs[("GBOS External Identity", mapping_result["name"])]
    case = fake.docs[("GBOS Review Case", result["name"])]
    snapshot = json.loads(case.subject_snapshot)
    assert mapping.review_status == "Pending"
    assert mapping.revision == 2
    assert result == {
        "doctype": "GBOS Review Case",
        "name": case.name,
        "review_status": "Pending",
        "revision": 2,
        "subject_name": mapping.name,
        "subject_revision": 2,
        "request_id": "REQ-0002",
    }
    assert fake.case_status_history == [("AI Draft", "Pending"), ("Pending", "Pending")]
    assert case.assigned_reviewer == "reviewer@example.invalid"
    assert case.subject_doctype == "GBOS External Identity"
    assert case.subject_name == mapping.name
    assert case.subject_revision == mapping.revision
    assert case.subject_payload_sha256 == _canonical_hash(snapshot)
    assert case.evidence_refs == json.dumps(["EVD-0001"], sort_keys=True)
    assert case.policy_version == "identity-association-v1"
    assert case.case_payload_sha256 == _canonical_hash(_case_payload(case))
    assert case.origin_reference == mapping.origin_reference
    assert crm_before == {
        key: doc.values
        for key, doc in fake.docs.items()
        if key[0] in {"GBOS Party Profile", "Contact"}
    }


@pytest.mark.parametrize("blocker", ["pending", "same_revision"])
def test_new_review_round_rejects_only_pending_or_same_revision_cases(
    identity_review: tuple[Any, _Frappe],
    blocker: str,
) -> None:
    service, fake = identity_review
    first = service.materialize_association_suggestion(_materialize_request())
    mapping = fake.docs[("GBOS External Identity", first["name"])]
    mapping.review_status = "Rejected"
    mapping.revision = 3
    service.rematerialize_rejected_association_suggestion(_rematerialize_request(mapping.name))
    fake._add_doc(
        "GBOS Review Case",
        "REV-BLOCKER",
        subject_doctype="GBOS External Identity",
        subject_name=mapping.name,
        subject_revision=2 if blocker == "pending" else 4,
        business_status="Pending" if blocker == "pending" else "Rejected",
        review_status="Pending" if blocker == "pending" else "Rejected",
    )

    with pytest.raises(service.IdentityReviewError, match="submitted"):
        service.submit_for_review(
            _submit_request(
                mapping.name,
                expected_revision=4,
                idempotency_key=f"submit-{blocker}-blocker",
            )
        )

    assert mapping.review_status == "AI Draft"
    assert mapping.revision == 4


@pytest.mark.parametrize(
    ("submit_overrides", "mutate"),
    (
        ({"expected_revision": 2}, None),
        ({"assigned_reviewer": "wrong-reviewer@example.invalid"}, None),
        ({"assigned_reviewer": "sales@example.invalid"}, None),
        ({"team": "TEM-02"}, None),
        ({"policy_version": "identity-association-v2"}, None),
        ({"evidence_refs": ["EVD-CHANGED"]}, None),
        (
            {"selected_candidate_type": "Party", "selected_candidate_ref": "PTY-01"},
            None,
        ),
        ({}, "disable_reviewer"),
        ({}, "disable_target"),
    ),
)
def test_submit_fails_closed_for_stale_scope_reviewer_or_target(
    identity_review: tuple[Any, _Frappe],
    submit_overrides: dict[str, Any],
    mutate: str | None,
) -> None:
    service, fake = identity_review
    mapping_result = service.materialize_association_suggestion(_materialize_request())
    if mutate == "disable_target":
        fake.enabled_users.remove("user-target@example.invalid")
    if mutate == "disable_reviewer":
        fake.enabled_users.remove("reviewer@example.invalid")

    with pytest.raises((service.IdentityReviewError, _PermissionError)):
        service.submit_for_review(_submit_request(mapping_result["name"], **submit_overrides))

    assert not any(key[0] == "GBOS Review Case" for key in fake.docs)


def test_submit_replay_survives_response_loss_and_duplicate_submission_fails_closed(
    identity_review: tuple[Any, _Frappe],
) -> None:
    service, fake = identity_review
    mapping_result = service.materialize_association_suggestion(_materialize_request())
    request = _submit_request(mapping_result["name"])
    fake.fail_after_store = True

    with pytest.raises(_ResponseLost):
        service.submit_for_review(request)
    replay = service.submit_for_review(request)

    assert replay["name"] == "REV-0001"
    assert fake.idempotent_executions["identity_review.submit"] == 1
    assert len([key for key in fake.docs if key[0] == "GBOS Review Case"]) == 1
    with pytest.raises(service.IdentityReviewError, match="draft|submitted"):
        service.submit_for_review(
            {**request, "idempotency_key": "submit-review-0002", "request_id": "REQ-0003"}
        )


def test_closed_requests_and_sensitive_values_never_leak_in_errors_or_results(
    identity_review: tuple[Any, _Frappe],
) -> None:
    service, _fake = identity_review
    request = _materialize_request(
        external_subject_ref=SENSITIVE_SUBJECT,
        model_suggested_target_ref=SENSITIVE_TARGET,
    )

    with pytest.raises(service.IdentityReviewError) as raised:
        service.materialize_association_suggestion({**request, "unexpected": "field"})

    assert SENSITIVE_SUBJECT not in repr(raised.value)
    assert SENSITIVE_TARGET not in repr(raised.value)
    source = ROOT / "apps/esan_gbos/esan_gbos/domain/identity_review.py"
    if source.exists():
        assert "@frappe.whitelist" not in source.read_text(encoding="utf-8")
