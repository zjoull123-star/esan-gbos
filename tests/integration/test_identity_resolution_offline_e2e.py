from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Callable, Generator, Mapping
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from services.local_pilot_runtime.identity_resolution_worker import (
    DEFAULT_FRAPPE_BASE_URL,
    FrappeIdentityResolverClient,
    HeartbeatRunner,
    IdentityResolutionRunStatus,
    IdentityResolutionWorker,
)
from services.observer.observer.identity_resolution import (
    InMemoryIdentityResolutionRepository,
)
from services.observer.observer.identity_resolution_work import (
    InMemoryIdentityResolutionWorkRepository,
)
from services.observer.observer.identity_tokens import (
    HmacSha256IdentityTokenResolver,
    TransientIdentitySubject,
)
from services.observer.observer.models import ConnectorItem, EvidenceArtifact, TenantScope
from services.observer.observer.normalizers import EmailObservationNormalizer
from services.observer.observer.read_service import (
    IDENTITY_SELF_ACCESS_MAX_AGE,
    CommunicationAccess,
    CommunicationDetail,
    CommunicationSummary,
    InMemoryCommunicationRepository,
    LocalPilotReadService,
    RawAccessDenied,
    ScopeMismatch,
)

ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 8, 10, 9, tzinfo=UTC)
SCOPE = TenantScope("gbos.localhost", "observation_processing")
TEAM = "TEM-SALES"
OTHER_TEAM = "TEM-OTHER"
SENDER_SENTINEL = "Sender.Sensitive@Example.Invalid"
TARGET_SENTINEL = "target-sensitive@example.invalid"
BODY_SENTINEL = "private-body-sensitive-sentinel"
MAPPING_REF = "EID-01ARZ3NDEKTSV4RRFFQ69G5FAV"
SUGGESTION_KEY = f"suggestion:v1:{'a' * 64}"
MODEL = {"name": "deepseek-v4-flash", "version": "offline-fake-transport"}


class _PermissionError(Exception):
    pass


class _DuplicateEntryError(Exception):
    pass


class _ValidationError(Exception):
    pass


class _Doc:
    def __init__(self, runtime: _Frappe, values: Mapping[str, Any]) -> None:
        object.__setattr__(self, "runtime", runtime)
        object.__setattr__(self, "values", dict(values))
        object.__setattr__(self, "flags", SimpleNamespace())
        object.__setattr__(self, "_before", None)

    def __getattr__(self, fieldname: str) -> Any:
        try:
            return self.values[fieldname]
        except KeyError as error:
            raise AttributeError(fieldname) from error

    def __setattr__(self, fieldname: str, value: Any) -> None:
        if fieldname in {"runtime", "values", "flags", "_before"}:
            object.__setattr__(self, fieldname, value)
        else:
            self.values[fieldname] = value

    def __repr__(self) -> str:
        return f"_Doc(doctype={self.get('doctype')!r}, values=<redacted>)"

    def get(self, fieldname: str, default: Any = None) -> Any:
        return self.values.get(fieldname, default)

    def set(self, fieldname: str, value: Any) -> None:
        self.values[fieldname] = value

    def is_new(self) -> bool:
        name = self.get("name")
        return not isinstance(name, str) or (str(self.doctype), name) not in self.runtime.snapshots

    def get_doc_before_save(self) -> _Doc | None:
        before: _Doc | None = object.__getattribute__(self, "_before")
        return before

    def validate(self) -> None:
        return None

    def insert(self, *, ignore_permissions: bool) -> _Doc:
        assert ignore_permissions is True
        doctype = str(self.doctype)
        name = str(self.get("name") or self.runtime.next_name(doctype))
        key = (doctype, name)
        if key in self.runtime.docs:
            raise _DuplicateEntryError
        self.name = name
        self.revision = 1
        self.validate()
        self.runtime.docs[key] = self
        self.runtime.snapshots[key] = deepcopy(self.values)
        return self

    def save(self, *, ignore_permissions: bool) -> _Doc:
        assert ignore_permissions is True
        key = (str(self.doctype), str(self.name))
        persisted = self.runtime.snapshots[key]
        object.__setattr__(self, "_before", _Doc(self.runtime, persisted))
        self.validate()
        self.revision = int(self.revision) + 1
        self.runtime.docs[key] = self
        self.runtime.snapshots[key] = deepcopy(self.values)
        on_update = getattr(self, "on_update", None)
        if callable(on_update):
            on_update()
        object.__setattr__(self, "flags", SimpleNamespace())
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

        def matches(doc: _Doc) -> bool:
            for fieldname, expected in filters.items():
                actual = doc.get(fieldname)
                if isinstance(expected, list) and expected[:1] == ["!="]:
                    if len(expected) != 2 or actual == expected[1]:
                        return False
                elif actual != expected:
                    return False
            return True

        return any(doc.doctype == doctype and matches(doc) for doc in self.runtime.docs.values())

    def get_value(
        self,
        doctype: str,
        name: str,
        fieldname: str,
        **_kwargs: Any,
    ) -> Any:
        if (doctype, fieldname) == ("User", "enabled"):
            return int(name in self.runtime.enabled_users)
        doc = self.runtime.docs.get((doctype, name))
        return None if doc is None else doc.get(fieldname)


class _Frappe(ModuleType):
    def __init__(self) -> None:
        super().__init__("frappe")
        self.PermissionError = _PermissionError
        self.DuplicateEntryError = _DuplicateEntryError
        self.ValidationError = _ValidationError
        self.session = SimpleNamespace(user="sales@example.invalid")
        self.roles = {
            "sales@example.invalid": {"Sales User"},
            "reviewer@example.invalid": {"Reviewer"},
            "admin@example.invalid": {"GBOS Admin"},
            "ai-agent@example.invalid": {"AI Agent"},
        }
        self.enabled_users = {
            "sales@example.invalid",
            "reviewer@example.invalid",
            "admin@example.invalid",
            TARGET_SENTINEL,
        }
        self.team_members = {
            (TEAM, "sales@example.invalid"),
            (TEAM, "reviewer@example.invalid"),
            (TEAM, TARGET_SENTINEL),
        }
        self.docs: dict[tuple[str, str], _Doc] = {}
        self.snapshots: dict[tuple[str, str], dict[str, Any]] = {}
        self.idempotency: dict[str, tuple[str, dict[str, Any], str]] = {}
        self.executions: dict[str, int] = {}
        self.external_identity: Any = None
        self.review_case: Any = None
        self.db = _Database(self)
        self._counters: dict[str, int] = {}

    def next_name(self, doctype: str) -> str:
        self._counters[doctype] = self._counters.get(doctype, 0) + 1
        if doctype == "GBOS External Identity":
            assert self._counters[doctype] == 1
            return MAPPING_REF
        if doctype == "GBOS Review Case":
            return f"REV-{self._counters[doctype]:04d}"
        raise AssertionError(f"unexpected doctype: {doctype}")

    def get_roles(self, user: str | None = None) -> list[str]:
        return sorted(self.roles.get(user or str(self.session.user), set()))

    def get_doc(
        self,
        doctype: str | dict[str, Any],
        name: str | None = None,
        **_kwargs: Any,
    ) -> _Doc:
        if isinstance(doctype, dict):
            document_type = str(doctype.get("doctype"))
            document_class: type[_Doc] = _Doc
            if document_type == "GBOS External Identity" and self.external_identity is not None:
                document_class = self.external_identity.GBOSExternalIdentity
            elif document_type == "GBOS Review Case" and self.review_case is not None:
                document_class = self.review_case.GBOSReviewCase
            return document_class(self, doctype)
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
        return [
            {"name": doc.name}
            for doc in self.docs.values()
            if doc.doctype == doctype
            and all(doc.get(fieldname) == value for fieldname, value in filters.items())
        ][:limit_page_length]

    def parse_json(self, value: str) -> Any:
        return json.loads(value)

    def throw(self, message: str, *, title: str | None = None) -> None:
        del title
        raise _ValidationError(message)

    def count(self, doctype: str) -> int:
        return sum(key[0] == doctype for key in self.docs)

    def decide_identity_case(self, case_name: str, *, actor: str, decision: str) -> _Doc:
        case = self.docs[("GBOS Review Case", case_name)]
        self.session.user = actor
        if actor != case.assigned_reviewer or "Reviewer" not in self.roles.get(actor, set()):
            raise self.PermissionError
        case.flags.gbos_review_command = True
        case.business_status = decision
        case.last_request_id = "REQ-HUMAN-DECISION"
        return case.save(ignore_permissions=True)

    def revoke_identity(self, mapping_name: str, *, actor: str) -> _Doc:
        mapping = self.docs[("GBOS External Identity", mapping_name)]
        self.session.user = actor
        if "GBOS Admin" not in self.roles.get(actor, set()):
            raise self.PermissionError
        mapping.flags.gbos_identity_status_command = True
        mapping.business_status = "Revoked"
        mapping.last_request_id = "REQ-HUMAN-REVOCATION"
        return mapping.save(ignore_permissions=True)


@pytest.fixture
def frappe_identity_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[tuple[Any, _Frappe, Any]]:
    monkeypatch.syspath_prepend(str(ROOT / "apps" / "esan_gbos"))
    fake = _Frappe()
    base_module = ModuleType("esan_gbos.gbos.doctype.base")
    base_module.GBOSDocument = _Doc  # type: ignore[attr-defined]
    utils_module = ModuleType("frappe.utils")
    utils_module.now_datetime = lambda: NOW  # type: ignore[attr-defined]
    injected: dict[str, ModuleType] = {
        "frappe": fake,
        "frappe.utils": utils_module,
        base_module.__name__: base_module,
    }
    module_names = (
        "esan_gbos.gbos.doctype.gbos_external_identity.gbos_external_identity",
        "esan_gbos.gbos.doctype.gbos_review_case.gbos_review_case",
        "esan_gbos.domain.identity_review",
        "esan_gbos.api.v1.audit",
    )
    originals = {name: sys.modules.get(name) for name in (*injected, *module_names)}
    for name, module in injected.items():
        monkeypatch.setitem(sys.modules, name, module)
    for name in module_names:
        sys.modules.pop(name, None)

    external_identity = importlib.import_module(module_names[0])
    review_case = importlib.import_module(module_names[1])
    fake.external_identity = external_identity
    fake.review_case = review_case
    review_dto = importlib.import_module("esan_gbos.domain.review_dto")

    def run_idempotent(
        command: str,
        key: str,
        payload: dict[str, Any],
        execute: Callable[[], dict[str, Any]],
        *,
        api_version: str,
    ) -> tuple[dict[str, Any], bool, str]:
        assert api_version == "domain"
        digest = review_dto.canonical_payload_hash(
            {"command": command, "actor": fake.session.user, "payload": payload}
        )
        existing = fake.idempotency.get(key)
        if existing is not None:
            if existing[0] != digest:
                raise ValueError("idempotency_conflict")
            return deepcopy(existing[1]), True, existing[2]
        fake.executions[command] = fake.executions.get(command, 0) + 1
        result = execute()
        fake.idempotency[key] = (digest, deepcopy(result), str(payload["request_id"]))
        return result, False, str(payload["request_id"])

    audit_module = ModuleType("esan_gbos.api.v1.audit")
    audit_module.run_idempotent = run_idempotent  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, audit_module.__name__, audit_module)
    identity_review = importlib.import_module(module_names[2])
    try:
        yield identity_review, fake, external_identity
    finally:
        for name, original in originals.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


class _Clock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


class _InlineHeartbeatRunner:
    def run(self, execute: Callable[[], Any], heartbeat: Callable[[], object]) -> Any:
        del heartbeat
        return execute()


class _AuthorityTransport:
    def __init__(self, fake: _Frappe, authority: Any) -> None:
        self.fake = fake
        self.authority = authority
        self.outbound_requests: list[dict[str, Any]] = []

    def __repr__(self) -> str:
        return "_AuthorityTransport(state=<redacted>, requests=<redacted>)"

    def request(self, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        self.outbound_requests.append(deepcopy(kwargs))
        lookup = kwargs["payload"]["payload"]["lookups"][0]
        matching = [
            doc
            for doc in self.fake.docs.values()
            if doc.doctype == "GBOS External Identity"
            and doc.identity_provider == lookup["identity_provider"]
            and doc.external_subject == lookup["external_subject_ref"]
        ]
        if len(matching) != 1:
            return 404, {"message": {"error": {"code": "mapping_not_resolved"}}}
        mapping = matching[0]
        if mapping.team != lookup["expected_team_ref"]:
            return 403, {"message": {"error": {"code": "team_scope_mismatch"}}}
        if self.authority.is_authoritative_mapping(mapping):
            status = "confirmed"
        elif mapping.review_status == "Approved" and mapping.business_status == "Revoked":
            status = "revoked"
        else:
            return 404, {"message": {"error": {"code": "mapping_not_resolved"}}}
        resolved_at = NOW + timedelta(minutes=int(mapping.revision))
        return 200, {
            "message": {
                "resolutions": [
                    {
                        "schema_version": "1.0",
                        "site_id": SCOPE.site_id,
                        "identity_provider": mapping.identity_provider,
                        "external_subject_ref": mapping.external_subject,
                        "mapping_ref": mapping.name,
                        "mapping_revision": int(mapping.revision),
                        "team_ref": mapping.team,
                        "target_type": mapping.identity_type,
                        "target_ref": mapping.user,
                        "status": status,
                        "resolved_at": resolved_at.isoformat().replace("+00:00", "Z"),
                    }
                ]
            }
        }


class _WorkAuthorityFreshness:
    def __init__(
        self,
        repository: InMemoryIdentityResolutionWorkRepository,
        work_id: str,
    ) -> None:
        self.repository = repository
        self.work_id = work_id

    def is_confirmed_fresh(
        self,
        scope: TenantScope,
        identity_provider: str,
        identity_ref: str,
        team_ref: str,
        *,
        now: datetime,
        max_age: timedelta,
    ) -> bool:
        item = self.repository.get(scope, self.work_id)
        return bool(
            item is not None
            and item.identity_provider == identity_provider
            and item.identity_ref == identity_ref
            and item.team_ref == team_ref
            and item.status not in {"conflict", "dead_letter"}
            and item.last_resolution_status == "confirmed"
            and item.last_resolution_success_at is not None
            and now - max_age <= item.last_resolution_success_at <= now
        )


def _email_item(sequence: int, sender: str) -> ConnectorItem:
    return ConnectorItem(
        provider_event_id=f"email-fixture-{sequence:03d}",
        occurred_at=NOW + timedelta(minutes=sequence),
        source_cursor=f"uid:{sequence}",
        payload={
            "kind": "email_raw_delivery",
            "source_ref": f"evidence-email-{sequence:03d}",
            "body_evidence": EvidenceArtifact(
                media_type="text/plain; charset=utf-8",
                locator="message-body",
                role="derived-text",
                content=BODY_SENTINEL.encode(),
            ),
            "attachment_evidence": (),
            "identity_subjects": (TransientIdentitySubject(provider="email", subject=sender),),
        },
    )


def _normalize_email(
    normalizer: EmailObservationNormalizer,
    sequence: int,
    sender: str,
) -> Any:
    return normalizer.normalize(
        _email_item(sequence, sender),
        source_ref=f"evidence-email-{sequence:03d}",
    )


def _communication(observation_id: str, *, team_ref: str) -> CommunicationDetail:
    return CommunicationDetail(
        summary=CommunicationSummary(
            observation_id=observation_id,
            channel="email",
            occurred_at=NOW,
            summary_zh="离线身份解析测试消息",
            original_language="en",
            classification="Restricted",
            review_status="AI Draft",
            team_ref=team_ref,
            party_ref=None,
            evidence_count=1,
        ),
        evidence=({"ref": "EVD-0001", "locator": "message"},),
        fact_proposals=(),
        association_suggestions=(
            {
                "type": "user",
                "target_ref": "model-candidate-alias",
                "confidence": 0.75,
            },
        ),
        model=MODEL,
        original_text=BODY_SENTINEL,
    )


def _materialize_request(identity_ref: str) -> dict[str, Any]:
    return {
        "team": TEAM,
        "identity_provider": "email",
        "external_subject_ref": identity_ref,
        "observation_id": "OBS-EMAIL-0001",
        "suggestion_key": SUGGESTION_KEY,
        "association_type": "user",
        "model_suggested_target_ref": "model-candidate-alias",
        "selected_candidate_type": "User",
        "selected_candidate_ref": TARGET_SENTINEL,
        "evidence_refs": ["EVD-0001"],
        "policy_version": "identity-association-v1",
        "idempotency_key": "materialize-email-identity-0001",
        "request_id": "REQ-MATERIALIZE-0001",
    }


def _submit_request(mapping_name: str) -> dict[str, Any]:
    return {
        "name": mapping_name,
        "team": TEAM,
        "observation_id": "OBS-EMAIL-0001",
        "suggestion_key": SUGGESTION_KEY,
        "association_type": "user",
        "model_suggested_target_ref": "model-candidate-alias",
        "selected_candidate_type": "User",
        "selected_candidate_ref": TARGET_SENTINEL,
        "assigned_reviewer": "reviewer@example.invalid",
        "expected_revision": 1,
        "evidence_refs": ["EVD-0001"],
        "policy_version": "identity-association-v1",
        "idempotency_key": "submit-email-identity-0001",
        "request_id": "REQ-SUBMIT-0001",
    }


def _client(transport: _AuthorityTransport) -> FrappeIdentityResolverClient:
    return FrappeIdentityResolverClient(
        base_url=DEFAULT_FRAPPE_BASE_URL,
        unix_socket=None,
        site_id=SCOPE.site_id,
        auth_ref="observer-identity-resolver-v1",
        api_key="offline-resolver-key",
        api_secret="offline-resolver-secret",
        timeout_seconds=2,
        lease_duration=timedelta(seconds=10),
        transport=transport,
    )


def _worker(
    *,
    work: InMemoryIdentityResolutionWorkRepository,
    projection: InMemoryIdentityResolutionRepository,
    transport: _AuthorityTransport,
    clock: _Clock,
    worker_id: str,
) -> IdentityResolutionWorker:
    runner: HeartbeatRunner = _InlineHeartbeatRunner()
    return IdentityResolutionWorker(
        work_repository=work,
        projection_repository=projection,
        client=_client(transport),
        worker_id=worker_id,
        clock=clock,
        lease_duration=timedelta(seconds=10),
        unresolved_recheck=timedelta(minutes=5),
        successful_recheck=timedelta(hours=1),
        heartbeat_runner=runner,
    )


def test_email_identity_resolution_offline_e2e(
    frappe_identity_domain: tuple[Any, _Frappe, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    identity_review, fake_frappe, external_identity = frappe_identity_domain
    resolver = HmacSha256IdentityTokenResolver(b"offline-identity-hmac-key-material" * 2)
    normalizer = EmailObservationNormalizer(
        identity_resolver=resolver,
        site_id=SCOPE.site_id,
        purpose=SCOPE.processing_purpose,
    )
    first = _normalize_email(normalizer, 1, f" {SENDER_SENTINEL} ")
    second = _normalize_email(normalizer, 2, SENDER_SENTINEL.casefold())
    identity_ref = first.participants[0].identity_ref

    assert identity_ref == second.participants[0].identity_ref
    assert identity_ref.startswith("extid:v1:email:")
    assert SENDER_SENTINEL.casefold() not in identity_ref.casefold()
    assert (
        resolver.resolve("other.localhost", SCOPE.processing_purpose, "email", SENDER_SENTINEL)
        != identity_ref
    )
    assert (
        resolver.resolve(SCOPE.site_id, "entity_resolution", "email", SENDER_SENTINEL)
        != identity_ref
    )

    clock = _Clock()
    work = InMemoryIdentityResolutionWorkRepository()
    projection = InMemoryIdentityResolutionRepository()
    queued = work.enqueue(
        SCOPE,
        identity_provider="email",
        identity_ref=identity_ref,
        team_ref=TEAM,
        now=clock.value,
    )
    authority_transport = _AuthorityTransport(fake_frappe, external_identity)
    worker = _worker(
        work=work,
        projection=projection,
        transport=authority_transport,
        clock=clock,
        worker_id="identity-worker-offline-1",
    )
    reader_repository = InMemoryCommunicationRepository(
        identity_repository=projection,
        authority_freshness=_WorkAuthorityFreshness(work, queued.work_id),
        clock=clock,
    )
    reader_repository.put(
        SCOPE,
        _communication("OBS-EMAIL-0001", team_ref=TEAM),
        participant_refs=(identity_ref,),
    )
    reader = LocalPilotReadService(repository=reader_repository, cursor_secret=b"c" * 32)
    self_access = CommunicationAccess(
        team_refs=frozenset({"TEM-NONE"}),
        actor_ref=TARGET_SENTINEL,
    )

    assert worker.run_once(SCOPE).status is IdentityResolutionRunStatus.UNRESOLVED
    unresolved = work.get(SCOPE, queued.work_id)
    assert unresolved is not None
    assert unresolved.status == "unresolved"
    assert unresolved.last_resolution_status == "unresolved"
    assert projection.history(SCOPE, "email", identity_ref) == ()
    assert reader.list_communications(SCOPE, self_access).communications == ()
    with pytest.raises(ScopeMismatch):
        reader.get_communication(SCOPE, self_access, observation_id="OBS-EMAIL-0001")

    materialize_request = _materialize_request(identity_ref)
    materialized = identity_review.materialize_association_suggestion(materialize_request)
    mapping = fake_frappe.docs[("GBOS External Identity", materialized["name"])]
    assert mapping.review_status == "AI Draft"
    assert external_identity.is_authoritative_mapping(mapping) is False
    submitted = identity_review.submit_for_review(_submit_request(mapping.name))
    assert mapping.review_status == "Pending"
    assert external_identity.is_authoritative_mapping(mapping) is False
    with pytest.raises(_PermissionError) as ai_denied:
        fake_frappe.decide_identity_case(
            submitted["name"], actor="ai-agent@example.invalid", decision="Approved"
        )
    assert mapping.review_status == "Pending"

    review_case = fake_frappe.decide_identity_case(
        submitted["name"], actor="reviewer@example.invalid", decision="Approved"
    )
    assert review_case.review_status == "Approved"
    assert mapping.review_status == "Approved"
    assert mapping.business_status == "Active"
    assert external_identity.is_authoritative_mapping(mapping) is True

    clock.advance(timedelta(minutes=5))
    replayed_work = work.enqueue(
        SCOPE,
        identity_provider="email",
        identity_ref=second.participants[0].identity_ref,
        team_ref=TEAM,
        now=clock.value,
    )
    assert replayed_work.work_id == queued.work_id
    assert worker.run_once(SCOPE).status is IdentityResolutionRunStatus.CONFIRMED
    reader_repository.put(
        SCOPE,
        _communication("OBS-EMAIL-0002", team_ref=TEAM),
        participant_refs=(identity_ref,),
    )
    reader_repository.put(
        SCOPE,
        _communication("OBS-EMAIL-CROSS-TEAM", team_ref=OTHER_TEAM),
        participant_refs=(identity_ref,),
    )

    self_visible = reader.list_communications(SCOPE, self_access).communications
    assert {item.observation_id for item in self_visible} == {
        "OBS-EMAIL-0001",
        "OBS-EMAIL-0002",
    }
    same_team_visible = reader.list_communications(
        SCOPE, CommunicationAccess(team_refs=frozenset({TEAM}))
    ).communications
    assert {item.observation_id for item in same_team_visible} == {
        "OBS-EMAIL-0001",
        "OBS-EMAIL-0002",
    }
    with pytest.raises(ScopeMismatch) as cross_team_denied:
        reader.get_communication(
            SCOPE,
            self_access,
            observation_id="OBS-EMAIL-CROSS-TEAM",
        )
    raw_subject_access = CommunicationAccess(
        team_refs=frozenset({"TEM-NONE"}),
        actor_ref=SENDER_SENTINEL,
    )
    assert reader.list_communications(SCOPE, raw_subject_access).communications == ()
    with pytest.raises(ScopeMismatch) as raw_subject_denied:
        reader.get_communication(
            SCOPE,
            raw_subject_access,
            observation_id="OBS-EMAIL-0002",
        )
    with pytest.raises(RawAccessDenied) as raw_body_denied:
        reader.get_communication(
            SCOPE,
            self_access,
            observation_id="OBS-EMAIL-0002",
            include_raw=True,
        )

    fake_frappe.session.user = "sales@example.invalid"
    assert identity_review.materialize_association_suggestion(materialize_request) == materialized
    assert identity_review.submit_for_review(_submit_request(mapping.name)) == submitted
    assert fake_frappe.count("GBOS External Identity") == 1
    assert fake_frappe.count("GBOS Review Case") == 1
    assert fake_frappe.executions == {
        "identity_review.materialize": 1,
        "identity_review.submit": 1,
    }
    with pytest.raises(ValueError, match="already exists"):
        reader_repository.put(
            SCOPE,
            _communication("OBS-EMAIL-0002", team_ref=TEAM),
            participant_refs=(identity_ref,),
        )

    confirmed_history = projection.history(SCOPE, "email", identity_ref)
    assert len(confirmed_history) == 1
    confirmed = confirmed_history[0]
    assert confirmed.status == "confirmed"
    assert confirmed.team_ref == TEAM
    assert confirmed.target_type == "User"
    clock.advance(timedelta(hours=1))
    restarted = _worker(
        work=work,
        projection=projection,
        transport=authority_transport,
        clock=clock,
        worker_id="identity-worker-offline-restarted",
    )
    assert restarted.run_once(SCOPE).status is IdentityResolutionRunStatus.CONFIRMED
    assert projection.history(SCOPE, "email", identity_ref) == (confirmed,)

    revoked_mapping = fake_frappe.revoke_identity(mapping.name, actor="admin@example.invalid")
    assert revoked_mapping.review_status == "Approved"
    assert revoked_mapping.business_status == "Revoked"
    assert int(revoked_mapping.revision) > confirmed.mapping_revision
    assert external_identity.is_authoritative_mapping(revoked_mapping) is False
    clock.advance(timedelta(hours=1))
    assert restarted.run_once(SCOPE).status is IdentityResolutionRunStatus.REVOKED
    history = projection.history(SCOPE, "email", identity_ref)
    assert len(history) == 2
    assert history[0] == confirmed
    assert history[1].mapping_revision > history[0].mapping_revision
    assert history[1].status == "revoked"
    assert reader.list_communications(SCOPE, self_access).communications == ()
    revoked_work = work.get(SCOPE, queued.work_id)
    assert revoked_work is not None
    assert revoked_work.last_resolution_status == "revoked"
    assert timedelta(hours=2) == IDENTITY_SELF_ACCESS_MAX_AGE

    model_like_payload = {
        "model": MODEL,
        "association": {
            "type": "user",
            "target_ref": "model-candidate-alias",
        },
    }
    rendered_surfaces = "\n".join(
        (
            repr(_email_item(9, SENDER_SENTINEL)),
            repr(first),
            repr(unresolved),
            repr(confirmed),
            repr(history[1]),
            repr(mapping),
            repr(authority_transport),
            repr(ai_denied.value),
            repr(cross_team_denied.value),
            repr(raw_subject_denied.value),
            repr(raw_body_denied.value),
            json.dumps(authority_transport.outbound_requests, sort_keys=True),
            json.dumps(model_like_payload, sort_keys=True),
            caplog.text,
        )
    )
    for sentinel in (SENDER_SENTINEL, TARGET_SENTINEL, BODY_SENTINEL):
        assert sentinel.casefold() not in rendered_surfaces.casefold()
