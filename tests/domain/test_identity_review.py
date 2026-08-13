from __future__ import annotations

import hashlib
import importlib
import json
import re
import sys
from collections.abc import Generator, Mapping
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]
SUBJECT = "extid:v1:email:N6juwc4ZaH0TL-KQUdymKdFk4sSVi6FB1fQTOjPwaI8"
SENSITIVE_SUBJECT = "extid:v1:email:LV6GAKT7pm5calE6bndCH0B5zbhyjtErgQGWWEsLveI"
SENSITIVE_TARGET = "sensitive-target@example.invalid"
SUGGESTION_KEY = f"suggestion:v1:{'a' * 64}"
MAPPING_REF = "EID-01KZQEC7B9A41Q2ZCDPFGQ7V5K"
USER_CANDIDATE_REF = "USR-01KZQEC7B9A41Q2ZCDPFGQ7V5K"
EVIDENCE_REF = "EVR-01KZQEC7B9A41Q2ZCDPFGQ7V5K"


class _PermissionError(Exception):
    pass


class _DuplicateEntryError(Exception):
    pass


class _ResponseLost(Exception):
    pass


class _AddressMatchClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.next_changes: dict[str, Any] = {}
        self.fail = False

    def attest(self, request: Mapping[str, Any]) -> dict[str, Any]:
        assert set(request) == {
            "request_id",
            "site_id",
            "processing_purpose",
            "caller_ref",
            "evidence_ref",
            "address_role",
            "role_index",
            "opaque_address_ref",
            "candidate_target_ref",
            "candidate_target_type",
            "candidate_address",
        }
        self.calls.append(dict(request))
        if self.fail:
            raise RuntimeError("authority unavailable")
        observed = datetime.now(UTC).replace(microsecond=0)
        attestation = {
            "opaque_address_ref": request["opaque_address_ref"],
            "candidate_target_ref": request["candidate_target_ref"],
            "candidate_target_type": request["candidate_target_type"],
            "evidence_ref": request["evidence_ref"],
            "normalization_version": "email-address-v1",
            "matched": True,
            "observed_at": observed.isoformat().replace("+00:00", "Z"),
            "expires_at": (observed + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
            "digest": "sha256:" + chr(97 + len(self.calls) - 1) * 64,
        }
        attestation.update(self.next_changes)
        self.next_changes = {}
        return {
            "attestation_ref": f"EMA-01KZQEC7B9A41Q2ZCDPFGQ7V5{len(self.calls)}",
            "attestation": attestation,
        }


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
            assert (
                getattr(self.flags, "gbos_ai_draft_command", False)
                or getattr(self.flags, "gbos_ai_reopen_command", False)
                or getattr(self.flags, "gbos_human_identity_command", False)
                or getattr(self.flags, "gbos_identity_review_decision", False)
            )
        if self.doctype == "GBOS Review Case":
            assert getattr(self.flags, "gbos_review_command", False)
            assert getattr(self.flags, "gbos_ai_draft_command", False) or getattr(
                self.flags, "gbos_human_identity_command", False
            )
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
        if (doctype, fieldname) == ("User", "user_type"):
            return "System User" if name in self.runtime.enabled_users else None
        if (doctype, fieldname) == ("User", "email"):
            return name if name in self.runtime.enabled_users else None
        if (doctype, fieldname) == ("User", "modified"):
            return "2026-01-01T00:00:00+00:00" if name in self.runtime.enabled_users else None
        doc = self.runtime.docs.get((doctype, name))
        return None if doc is None else doc.get(fieldname)


class _Frappe(ModuleType):
    def __init__(self) -> None:
        super().__init__("frappe")
        self.PermissionError = _PermissionError
        self.DuplicateEntryError = _DuplicateEntryError
        self.session = SimpleNamespace(user="sales@example.invalid")
        self.address_match_client = _AddressMatchClient()
        self.local = SimpleNamespace(
            site="gbos.test",
            gbos_email_address_match_authority_client=self.address_match_client,
        )
        self.roles = {
            "sales@example.invalid": {"Sales User"},
            "reviewer@example.invalid": {"Reviewer"},
            "wrong-reviewer@example.invalid": {"Reviewer"},
            "admin@example.invalid": {"GBOS Admin"},
            "integration@example.invalid": {"Integration Admin"},
            "manager@example.invalid": {"Sales Manager"},
        }
        self.enabled_users = {
            "sales@example.invalid",
            "user-target@example.invalid",
            "reviewer@example.invalid",
            "wrong-reviewer@example.invalid",
            "admin@example.invalid",
            "integration@example.invalid",
            "manager@example.invalid",
        }
        self.team_members = {
            ("TEM-01", "sales@example.invalid"),
            ("TEM-01", "user-target@example.invalid"),
            ("TEM-01", "reviewer@example.invalid"),
            ("TEM-01", "manager@example.invalid"),
            ("TEM-02", "wrong-reviewer@example.invalid"),
        }
        self.docs: dict[tuple[str, str], _Doc] = {}
        self.idempotency: dict[str, tuple[str, dict[str, Any], str]] = {}
        self.idempotent_executions: dict[str, int] = {}
        self.fail_after_store = False
        self.case_status_history: list[tuple[str, str]] = []
        self._counters: dict[str, int] = {}
        self.db = _Database(self)
        self._add_doc(
            "GBOS Party Profile",
            "PTY-01",
            team="TEM-01",
            contact="CON-01",
            modified="2026-01-01T00:00:00+00:00",
        )
        self._add_doc(
            "GBOS Party Profile",
            "PTY-02",
            team="TEM-02",
            contact="CON-02",
            modified="2026-01-01T00:00:00+00:00",
        )
        self._add_doc(
            "Contact",
            "CON-01",
            email_id="customer@example.invalid",
            modified="2026-01-01T00:00:00+00:00",
        )
        self._add_doc(
            "Contact",
            "CON-02",
            email_id="cross-team@example.invalid",
            modified="2026-01-01T00:00:00+00:00",
        )

    def _add_doc(self, doctype: str, name: str, **values: Any) -> _Doc:
        doc = _Doc(self, {"doctype": doctype, "name": name, **values})
        self.docs[(doctype, name)] = doc
        return doc

    def next_name(self, doctype: str) -> str:
        prefixes = {"GBOS External Identity": "EID", "GBOS Review Case": "REV"}
        self._counters[doctype] = self._counters.get(doctype, 0) + 1
        if doctype == "GBOS External Identity" and self._counters[doctype] == 1:
            return MAPPING_REF
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
    if (
        not isinstance(subject, str)
        or not subject.startswith(prefix)
        or re.fullmatch(r"[A-Za-z0-9_-]{43}", subject[len(prefix) :]) is None
    ):
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
    "identity_ref",
    (
        "extid:v1:email:internal-user",
        "extid:v1:email:" + "A" * 42,
        "extid:v1:email:" + "A" * 44,
        "extid:v1:email:" + "A" * 43 + "=",
        "extid:v1:email:" + "A" * 42 + "+",
        "extid:v1:email:" + "A" * 42 + ".",
    ),
)
def test_identity_review_rejects_non_digest_external_refs_without_echo(
    identity_review: tuple[Any, _Frappe],
    identity_ref: str,
) -> None:
    service, fake = identity_review

    with pytest.raises(service.IdentityReviewError) as raised:
        service.materialize_association_suggestion(
            _materialize_request(external_subject_ref=identity_ref)
        )

    assert identity_ref not in repr(raised.value)
    assert not any(key[0] == "GBOS External Identity" for key in fake.docs)


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

    assert replay["name"] == MAPPING_REF
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
        {"external_subject_ref": "extid:v1:email:2SmKENGwc1g33EvYXaxkGw887yekfl1TpU8vP1svz_o"},
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


def _human_request(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "team": "TEM-01",
        "address_ref": SUBJECT,
        "target_type": "User",
        "target_ref": "user-target@example.invalid",
        "purpose": "employee_mapping",
        "evidence_ref": EVIDENCE_REF,
        "expected_revision": 0,
        "idempotency_key": "human-identity-0001",
        "request_id": "REQ-HUMAN-0001",
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize(
    ("actor", "target_type", "target_ref", "purpose"),
    (
        (
            "admin@example.invalid",
            "User",
            "user-target@example.invalid",
            "employee_mapping",
        ),
        (
            "integration@example.invalid",
            "User",
            "user-target@example.invalid",
            "employee_mapping",
        ),
        ("manager@example.invalid", "Party", "PTY-01", "customer_mapping"),
        ("reviewer@example.invalid", "Party", "PTY-01", "customer_mapping"),
    ),
)
def test_authorized_human_can_submit_without_ai_suggestion(
    identity_review: tuple[Any, _Frappe],
    actor: str,
    target_type: str,
    target_ref: str,
    purpose: str,
) -> None:
    service, fake = identity_review
    fake.session.user = actor

    receipt = service.submit_human_identity_for_review(
        _human_request(target_type=target_type, target_ref=target_ref, purpose=purpose)
    )

    mapping = fake.get_doc("GBOS External Identity", receipt["mapping_ref"])
    review_case = fake.get_doc("GBOS Review Case", receipt["review_case_ref"])
    assert mapping.origin == "Manual"
    assert mapping.review_status == "Pending"
    assert review_case.assigned_reviewer == actor
    assert review_case.origin == "Manual"
    assert "suggestion" not in repr(mapping.values).casefold()
    stored_evidence = json.loads(review_case.evidence_refs)
    assert stored_evidence[0] == EVIDENCE_REF
    assert stored_evidence[1].startswith("EMA-")
    assert stored_evidence[2].startswith("sha256:")
    assert "candidate_address" not in repr((mapping.values, review_case.values))
    assert "customer@example.invalid" not in repr((mapping.values, review_case.values))
    authority_request = fake.address_match_client.calls[0]
    expected_prefix = "USR" if target_type == "User" else "PTY"
    assert authority_request["candidate_target_ref"] == (
        expected_prefix + "-" + receipt["mapping_ref"].removeprefix("EID-")
    )
    assert "@" not in str(authority_request["candidate_target_ref"])
    assert set(receipt) == {
        "mapping_ref",
        "mapping_revision",
        "review_case_ref",
        "review_case_revision",
        "request_id",
    }


@pytest.mark.parametrize(
    ("actor", "overrides"),
    (
        ("sales@example.invalid", {}),
        (
            "manager@example.invalid",
            {"target_type": "User", "purpose": "employee_mapping"},
        ),
        (
            "integration@example.invalid",
            {"target_type": "Party", "target_ref": "PTY-01", "purpose": "customer_mapping"},
        ),
        (
            "wrong-reviewer@example.invalid",
            {"target_type": "Party", "target_ref": "PTY-01", "purpose": "customer_mapping"},
        ),
    ),
)
def test_sales_user_wrong_purpose_role_or_cross_team_human_command_fails(
    identity_review: tuple[Any, _Frappe],
    actor: str,
    overrides: dict[str, Any],
) -> None:
    service, fake = identity_review
    fake.session.user = actor

    with pytest.raises(_PermissionError):
        service.submit_human_identity_for_review(_human_request(**overrides))

    assert not any(key[0] == "GBOS External Identity" for key in fake.docs)


@pytest.mark.parametrize("extra", ({"origin": "AI"}, {"suggestion_key": SUGGESTION_KEY}))
def test_human_command_is_closed_to_ai_or_generic_suggestion_fields(
    identity_review: tuple[Any, _Frappe],
    extra: dict[str, Any],
) -> None:
    service, fake = identity_review
    fake.session.user = "admin@example.invalid"

    with pytest.raises(service.IdentityReviewError, match="fields"):
        service.submit_human_identity_for_review({**_human_request(), **extra})


def test_human_approval_requires_current_exact_bound_observer_attestation(
    identity_review: tuple[Any, _Frappe],
) -> None:
    service, fake = identity_review
    fake.session.user = "admin@example.invalid"
    submitted = service.submit_human_identity_for_review(_human_request())
    attestation = fake.address_match_client.calls[0]
    assert set(attestation) == {
        "request_id",
        "site_id",
        "processing_purpose",
        "caller_ref",
        "evidence_ref",
        "address_role",
        "role_index",
        "opaque_address_ref",
        "candidate_target_ref",
        "candidate_target_type",
        "candidate_address",
    }
    assert attestation["candidate_target_ref"] == USER_CANDIDATE_REF
    assert attestation["candidate_target_ref"] != "user-target@example.invalid"
    assert attestation["processing_purpose"] == "email_address_identity_confirmation"
    assert attestation["caller_ref"] == "frappe-identity-command"

    receipt = service.approve_human_identity_review(
        {
            "review_case_ref": submitted["review_case_ref"],
            "expected_review_case_revision": submitted["review_case_revision"],
            "expected_mapping_revision": submitted["mapping_revision"],
            "purpose": "employee_mapping",
            "evidence_ref": EVIDENCE_REF,
            "idempotency_key": "human-approve-0001",
            "request_id": "REQ-APPROVE-0001",
        }
    )

    mapping = fake.get_doc("GBOS External Identity", submitted["mapping_ref"])
    assert receipt["status"] == "approved"
    assert mapping.review_status == "Approved"
    assert mapping.business_status == "Active"
    assert len(fake.address_match_client.calls) == 2


def test_human_attestation_is_requested_only_after_mapping_ref_exists_and_failure_closes(
    identity_review: tuple[Any, _Frappe],
) -> None:
    service, fake = identity_review
    fake.session.user = "admin@example.invalid"
    fake.address_match_client.fail = True

    with pytest.raises(service.IdentityReviewError, match="authority"):
        service.submit_human_identity_for_review(_human_request())

    assert fake.address_match_client.calls[0]["candidate_target_ref"] == USER_CANDIDATE_REF
    assert not any(doc.doctype == "GBOS Review Case" for doc in fake.docs.values())


@pytest.mark.parametrize(
    "attestation_overrides",
    (
        {"matched": False},
        {"candidate_target_ref": "USR-01KZQEC7B9A41Q2ZCDPFGQ7V5M"},
        {"candidate_target_type": "Party"},
        {"evidence_ref": "EVR-01KZQEC7B9A41Q2ZCDPFGQ7V5M"},
        {"digest": "sha256:" + "b" * 63},
        {"expires_at": "2020-01-01T00:05:00Z"},
    ),
)
def test_human_approval_rejects_evidence_target_match_or_expiry_drift(
    identity_review: tuple[Any, _Frappe],
    attestation_overrides: dict[str, Any],
) -> None:
    service, fake = identity_review
    fake.session.user = "admin@example.invalid"
    submitted = service.submit_human_identity_for_review(_human_request())
    fake.address_match_client.next_changes = attestation_overrides

    with pytest.raises(service.IdentityReviewError, match="attestation"):
        service.approve_human_identity_review(
            {
                "review_case_ref": submitted["review_case_ref"],
                "expected_review_case_revision": submitted["review_case_revision"],
                "expected_mapping_revision": submitted["mapping_revision"],
                "purpose": "employee_mapping",
                "evidence_ref": EVIDENCE_REF,
                "idempotency_key": "human-approve-drift",
                "request_id": "REQ-APPROVE-DRIFT",
            }
        )
