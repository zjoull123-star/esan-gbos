from __future__ import annotations

import base64
import hashlib
import importlib.util
import sys
from collections.abc import Generator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]
CONTROLLER = (
    ROOT
    / "apps"
    / "esan_gbos"
    / "esan_gbos"
    / "gbos"
    / "doctype"
    / "gbos_external_identity"
    / "gbos_external_identity.py"
)
HOOKS = ROOT / "apps" / "esan_gbos" / "esan_gbos" / "hooks.py"
MAPPING_REF = "EID-01ARZ3NDEKTSV4RRFFQ69G5FAV"


def _subject(provider: str, label: str) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(label.encode()).digest()).rstrip(b"=").decode()
    return f"extid:v1:{provider}:{digest}"


class _PermissionError(Exception):
    pass


class _ValidationError(Exception):
    pass


class _Database:
    def __init__(self) -> None:
        self.enabled_users = {"member@example.invalid"}
        self.team_members = {("TEM-01", "member@example.invalid")}
        self.party_teams = {"PTY-01": "TEM-01", "PTY-OTHER": "TEM-02"}
        self.duplicates: set[tuple[str, str]] = set()

    def exists(self, doctype: str, filters: str | dict[str, Any]) -> bool:
        if doctype == "GBOS Team Member":
            assert isinstance(filters, dict)
            return (
                str(filters.get("parent")),
                str(filters.get("user")),
            ) in self.team_members and filters.get("enabled") == 1
        if doctype == "GBOS External Identity":
            assert isinstance(filters, dict)
            return (
                str(filters.get("identity_provider")),
                str(filters.get("external_subject")),
            ) in self.duplicates
        return False

    def get_value(self, doctype: str, name: str, fieldname: str, **kwargs: Any) -> Any:
        del kwargs
        if (doctype, fieldname) == ("User", "enabled"):
            return int(name in self.enabled_users)
        if (doctype, fieldname) == ("GBOS Party Profile", "team"):
            return self.party_teams.get(name)
        raise AssertionError(f"unexpected get_value: {doctype}.{name}.{fieldname}")


class _Frappe(ModuleType):
    def __init__(self) -> None:
        super().__init__("frappe")
        self.PermissionError = _PermissionError
        self.ValidationError = _ValidationError
        self.db = _Database()
        self.observer_calls: list[dict[str, Any]] = []
        self.observer_error: Exception | None = None
        self.mappings: list[dict[str, Any]] = []

    @staticmethod
    def throw(message: str, **kwargs: Any) -> None:
        del kwargs
        raise _ValidationError(message)

    def get_all(
        self,
        doctype: str,
        *,
        filters: dict[str, Any],
        fields: list[str],
        limit_page_length: int,
        limit_start: int = 0,
    ) -> list[dict[str, Any]]:
        assert doctype == "GBOS External Identity"
        matched = [
            {field: row.get(field) for field in fields}
            for row in self.mappings
            if all(row.get(field) == expected for field, expected in filters.items())
        ]
        return matched[limit_start : limit_start + limit_page_length]


class _Base:
    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)
        self.flags = SimpleNamespace()
        self._before = values.pop("_before", None)
        self._is_new = self._before is None
        self.base_validated = False

    def get(self, fieldname: str, default: Any = None) -> Any:
        return getattr(self, fieldname, default)

    def is_new(self) -> bool:
        return self._is_new

    def get_doc_before_save(self) -> Any:
        return self._before

    def validate(self) -> None:
        self.base_validated = True
        if self._before is not None:
            self.revision = int(getattr(self, "revision", 0) or 0) + 1

    def db_set(self, fieldname: str, value: Any = None, **kwargs: Any) -> None:
        del kwargs
        setattr(self, fieldname, value)


@pytest.fixture
def authority_module() -> Generator[tuple[Any, _Frappe]]:
    fake_frappe = _Frappe()
    fake_base = ModuleType("esan_gbos.gbos.doctype.base")
    fake_base.GBOSDocument = _Base
    original_frappe = sys.modules.get("frappe")
    original_base = sys.modules.get("esan_gbos.gbos.doctype.base")
    original_gateway = sys.modules.get("esan_gbos.api.v4.gateway")
    fake_gateway = ModuleType("esan_gbos.api.v4.gateway")

    def call_local(service: str, **kwargs: Any) -> dict[str, Any]:
        fake_frappe.observer_calls.append({"service": service, **kwargs})
        if fake_frappe.observer_error is not None:
            raise fake_frappe.observer_error
        payload = kwargs["payload"]
        return {
            "denial": {
                "mapping_ref": payload["mapping_ref"],
                "deny_through_revision": payload["deny_through_revision"],
                "status": "denied",
            }
        }

    fake_gateway.call_local = call_local  # type: ignore[attr-defined]
    sys.modules["frappe"] = fake_frappe
    sys.modules["esan_gbos.gbos.doctype.base"] = fake_base
    sys.modules["esan_gbos.api.v4.gateway"] = fake_gateway
    spec = importlib.util.spec_from_file_location("_external_identity_authority_test", CONTROLLER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module, fake_frappe
    if original_frappe is None:
        sys.modules.pop("frappe", None)
    else:
        sys.modules["frappe"] = original_frappe
    if original_base is None:
        sys.modules.pop("esan_gbos.gbos.doctype.base", None)
    else:
        sys.modules["esan_gbos.gbos.doctype.base"] = original_base
    if original_gateway is None:
        sys.modules.pop("esan_gbos.api.v4.gateway", None)
    else:
        sys.modules["esan_gbos.api.v4.gateway"] = original_gateway


@pytest.mark.parametrize(
    ("provider", "subject"),
    (
        ("email", _subject("email", "email")),
        ("wecom", _subject("wecom", "wecom")),
        ("whatsapp", _subject("whatsapp", "whatsapp")),
        ("phone", _subject("phone", "phone")),
        ("manual_import", _subject("manual_import", "manual_import")),
    ),
)
def test_external_subject_accepts_only_closed_provider_scoped_tokens(
    authority_module: tuple[Any, _Frappe],
    provider: str,
    subject: str,
) -> None:
    module, _fake = authority_module

    module.validate_external_subject(provider, subject)


@pytest.mark.parametrize(
    ("provider", "subject"),
    (
        ("gmail", "extid:v1:gmail:Opaque01"),
        ("email", "person@example.invalid"),
        ("phone", "+8613800138000"),
        ("email", "extid:v1:phone:Opaque01"),
        ("email", "extid:v1:email:person@example.invalid"),
        ("phone", "extid:v1:phone:+8613800138000"),
        ("email", "extid:v1:email:internal-user"),
        ("email", "extid:v1:email:" + "A" * 42),
        ("email", "extid:v1:email:" + "A" * 44),
        ("email", "extid:v1:email:" + "A" * 43 + "="),
        ("email", "extid:v1:email:" + "A" * 42 + "+"),
        ("email", "extid:v1:email:" + "A" * 42 + "."),
        ("email", "extid:v1:email: leading"),
        ("email", "extid:v1:email:line\nbreak"),
        ("email", "extid:v1:email:" + "a" * 129),
    ),
)
def test_external_subject_rejects_raw_mismatched_or_unbounded_values_without_echo(
    authority_module: tuple[Any, _Frappe],
    provider: str,
    subject: str,
) -> None:
    module, _fake = authority_module

    with pytest.raises(ValueError) as error:
        module.validate_external_subject(provider, subject)

    assert subject not in str(error.value)


def _identity(module: Any, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "doctype": "GBOS External Identity",
        "name": MAPPING_REF,
        "team": "TEM-01",
        "identity_provider": "email",
        "external_subject": _subject("email", "default"),
        "identity_type": "User",
        "user": "member@example.invalid",
        "party_profile": None,
        "origin": "Manual",
        "business_status": "Active",
        "review_status": "Pending",
    }
    values.update(overrides)
    return module.GBOSExternalIdentity(**values)


@pytest.mark.parametrize(
    "overrides",
    (
        {"identity_type": "User", "user": None},
        {"identity_type": "User", "party_profile": "PTY-01"},
        {"identity_type": "Party", "user": "member@example.invalid", "party_profile": "PTY-01"},
        {"identity_type": "Party", "user": None, "party_profile": None},
        {"identity_type": "Channel", "user": "member@example.invalid"},
        {"identity_type": "Channel", "user": None, "party_profile": "PTY-01"},
        {"identity_type": "Unknown", "user": None},
    ),
)
def test_identity_type_requires_exactly_its_closed_target(
    authority_module: tuple[Any, _Frappe],
    overrides: dict[str, Any],
) -> None:
    module, _fake = authority_module
    identity = _identity(module, **overrides)

    with pytest.raises(_ValidationError, match="target"):
        identity.validate()


def test_user_and_party_targets_must_be_enabled_and_same_team(
    authority_module: tuple[Any, _Frappe],
) -> None:
    module, fake = authority_module
    disabled = _identity(module, user="disabled@example.invalid")
    cross_team_party = _identity(
        module,
        identity_type="Party",
        user=None,
        party_profile="PTY-OTHER",
    )

    with pytest.raises(_ValidationError, match="same team"):
        disabled.validate()
    with pytest.raises(_ValidationError, match="same team"):
        cross_team_party.validate()

    fake.db.enabled_users.add("member@example.invalid")
    valid_user = _identity(module)
    valid_party = _identity(module, identity_type="Party", user=None, party_profile="PTY-01")
    valid_channel = _identity(module, identity_type="Channel", user=None, party_profile=None)
    valid_user.validate()
    valid_party.validate()
    valid_channel.validate()
    assert valid_user.base_validated


def test_duplicate_provider_subject_is_rejected_without_echo(
    authority_module: tuple[Any, _Frappe],
) -> None:
    module, fake = authority_module
    identity = _identity(module)
    fake.db.duplicates.add((identity.identity_provider, identity.external_subject))

    with pytest.raises(_ValidationError) as error:
        identity.validate()

    assert identity.external_subject not in str(error.value)


def test_ai_creation_and_direct_status_bypass_are_closed(
    authority_module: tuple[Any, _Frappe],
) -> None:
    module, _fake = authority_module
    illegal_ai = _identity(module, origin="AI", review_status="Approved")
    before = SimpleNamespace(
        review_status="Pending",
        business_status="Active",
        identity_provider="email",
        external_subject=_subject("email", "default"),
        identity_type="User",
        user="member@example.invalid",
        party_profile=None,
        team="TEM-01",
    )
    bypass = _identity(module, _before=before, review_status="Approved")

    with pytest.raises(_ValidationError, match="AI Draft"):
        illegal_ai.validate()
    with pytest.raises(_PermissionError):
        bypass.validate()

    bypass.flags.gbos_identity_review_decision = True
    bypass.validate()
    assert module.is_authoritative_mapping(bypass)


@pytest.mark.parametrize(
    ("origin", "review_status"),
    (
        ("Manual", "AI Draft"),
        ("Manual", "Approved"),
        ("Manual", "Rejected"),
        ("Manual", "Superseded"),
        ("Integration", "Approved"),
    ),
)
def test_new_non_ai_mapping_must_enter_through_pending_review(
    authority_module: tuple[Any, _Frappe],
    origin: str,
    review_status: str,
) -> None:
    module, _fake = authority_module

    with pytest.raises((_PermissionError, _ValidationError)):
        _identity(module, origin=origin, review_status=review_status).validate()


def test_generic_db_set_cannot_bypass_identity_status_boundary(
    authority_module: tuple[Any, _Frappe],
) -> None:
    module, _fake = authority_module
    identity = _identity(module)

    with pytest.raises(_PermissionError):
        identity.db_set("review_status", "Approved")

    assert identity.review_status == "Pending"


def test_rejected_ai_mapping_can_only_be_reopened_by_the_dedicated_command(
    authority_module: tuple[Any, _Frappe],
) -> None:
    module, _fake = authority_module
    before = SimpleNamespace(
        revision=4,
        review_status="Rejected",
        business_status="Active",
        origin="AI",
        origin_reference="association:v1:old",
        last_request_id="REQ-old",
        identity_provider="email",
        external_subject=_subject("email", "default"),
        identity_type="User",
        user="member@example.invalid",
        party_profile=None,
        team="TEM-01",
    )
    mapping = _identity(
        module,
        _before=before,
        revision=4,
        origin="AI",
        origin_reference="association:v1:corrected",
        last_request_id="REQ-corrected",
        review_status="AI Draft",
        identity_type="Party",
        user=None,
        party_profile="PTY-01",
    )
    mapping.flags.gbos_ai_reopen_command = True

    mapping.validate()

    assert mapping.review_status == "AI Draft"
    assert mapping.business_status == "Active"
    assert mapping.revision == 5
    assert mapping.identity_provider == before.identity_provider
    assert mapping.external_subject == before.external_subject
    assert mapping.team == before.team

    direct_target_change = _identity(
        module,
        _before=before,
        revision=4,
        origin="AI",
        origin_reference="association:v1:changed-without-command",
        last_request_id="REQ-illegal",
        review_status="Rejected",
        identity_type="Party",
        user=None,
        party_profile="PTY-01",
    )
    with pytest.raises(_PermissionError):
        direct_target_change.validate()


@pytest.mark.parametrize(
    "changes",
    (
        {
            "identity_provider": "wecom",
            "external_subject": _subject("wecom", "default"),
        },
        {"external_subject": _subject("email", "different")},
        {"team": "TEM-02", "identity_type": "Channel", "user": None},
    ),
)
def test_rejected_mapping_scope_cannot_be_mutated_without_reopen_command(
    authority_module: tuple[Any, _Frappe],
    changes: dict[str, Any],
) -> None:
    module, _fake = authority_module
    before = SimpleNamespace(
        revision=4,
        review_status="Rejected",
        business_status="Active",
        origin="AI",
        origin_reference="association:v1:old",
        last_request_id="REQ-old",
        identity_provider="email",
        external_subject=_subject("email", "default"),
        identity_type="User",
        user="member@example.invalid",
        party_profile=None,
        team="TEM-01",
    )
    mapping = _identity(
        module,
        _before=before,
        revision=4,
        origin="AI",
        origin_reference="association:v1:old",
        last_request_id="REQ-old",
        review_status="Rejected",
        **changes,
    )

    with pytest.raises(_PermissionError):
        mapping.validate()


@pytest.mark.parametrize(
    ("before_review", "before_business"),
    (
        ("Approved", "Active"),
        ("Pending", "Active"),
        ("AI Draft", "Active"),
        ("Rejected", "Revoked"),
        ("Superseded", "Archived"),
    ),
)
def test_reopen_command_cannot_reopen_non_rejected_active_mapping(
    authority_module: tuple[Any, _Frappe],
    before_review: str,
    before_business: str,
) -> None:
    module, _fake = authority_module
    before = SimpleNamespace(
        revision=4,
        review_status=before_review,
        business_status=before_business,
        origin="AI",
        origin_reference="association:v1:old",
        last_request_id="REQ-old",
        identity_provider="email",
        external_subject=_subject("email", "default"),
        identity_type="User",
        user="member@example.invalid",
        party_profile=None,
        team="TEM-01",
    )
    mapping = _identity(
        module,
        _before=before,
        revision=4,
        origin="AI",
        origin_reference="association:v1:corrected",
        last_request_id="REQ-corrected",
        review_status="AI Draft",
    )
    mapping.flags.gbos_ai_reopen_command = True

    with pytest.raises(_PermissionError):
        mapping.validate()


@pytest.mark.parametrize(
    "changes",
    (
        {
            "identity_provider": "wecom",
            "external_subject": _subject("wecom", "default"),
        },
        {"external_subject": _subject("email", "different")},
        {"team": "TEM-02", "identity_type": "Channel", "user": None},
    ),
)
def test_reopen_command_keeps_provider_subject_and_team_immutable(
    authority_module: tuple[Any, _Frappe],
    changes: dict[str, Any],
) -> None:
    module, _fake = authority_module
    before = SimpleNamespace(
        revision=4,
        review_status="Rejected",
        business_status="Active",
        origin="AI",
        origin_reference="association:v1:old",
        last_request_id="REQ-old",
        identity_provider="email",
        external_subject=_subject("email", "default"),
        identity_type="User",
        user="member@example.invalid",
        party_profile=None,
        team="TEM-01",
    )
    mapping = _identity(
        module,
        _before=before,
        revision=4,
        origin="AI",
        origin_reference="association:v1:corrected",
        last_request_id="REQ-corrected",
        review_status="AI Draft",
        **changes,
    )
    mapping.flags.gbos_ai_reopen_command = True

    with pytest.raises(_PermissionError):
        mapping.validate()


@pytest.mark.parametrize(
    ("review_status", "business_status", "expected"),
    (
        ("Approved", "Active", True),
        ("Pending", "Active", False),
        ("Rejected", "Active", False),
        ("Superseded", "Active", False),
        ("Approved", "Revoked", False),
        ("Approved", "Archived", False),
    ),
)
def test_only_approved_active_mapping_is_authoritative(
    authority_module: tuple[Any, _Frappe],
    review_status: str,
    business_status: str,
    expected: bool,
) -> None:
    module, _fake = authority_module

    assert (
        module.is_authoritative_mapping(
            _identity(
                module,
                review_status=review_status,
                business_status=business_status,
            )
        )
        is expected
    )


@pytest.mark.parametrize(
    ("decision", "expected"),
    (
        ("Approved", ("Approved", "Active")),
        ("Rejected", ("Rejected", "Active")),
        ("Superseded", ("Superseded", "Archived")),
    ),
)
def test_review_outcome_maps_to_a_closed_non_ambiguous_identity_state(
    authority_module: tuple[Any, _Frappe],
    decision: str,
    expected: tuple[str, str],
) -> None:
    module, _fake = authority_module

    assert module.review_state_for_decision(decision) == expected


def test_review_outcome_rejects_non_decisions(
    authority_module: tuple[Any, _Frappe],
) -> None:
    module, _fake = authority_module

    with pytest.raises(ValueError, match="review decision"):
        module.review_state_for_decision("Pending")


@pytest.mark.parametrize(
    ("before_status", "after_status", "reason"),
    (
        (
            {"review_status": "Approved", "business_status": "Active"},
            {"review_status": "Approved", "business_status": "Revoked"},
            "revoked",
        ),
        (
            {"review_status": "Pending", "business_status": "Active"},
            {"review_status": "Superseded", "business_status": "Archived"},
            "superseded",
        ),
    ),
)
def test_revocation_and_supersession_durably_deny_observer_before_save_returns(
    authority_module: tuple[Any, _Frappe],
    before_status: dict[str, str],
    after_status: dict[str, str],
    reason: str,
) -> None:
    module, fake = authority_module
    before = SimpleNamespace(
        revision=4,
        identity_provider="email",
        external_subject=_subject("email", "default"),
        identity_type="User",
        user="member@example.invalid",
        party_profile=None,
        team="TEM-01",
        **before_status,
    )
    mapping = _identity(module, _before=before, revision=4, **after_status)
    mapping.flags.gbos_identity_status_command = True
    mapping.flags.gbos_identity_review_decision = reason == "superseded"

    mapping.validate()

    assert mapping.base_validated is True
    assert mapping.revision == 5
    assert len(fake.observer_calls) == 1
    call = fake.observer_calls[0]
    assert call["service"] == "Observer"
    assert call["method"] == "POST"
    assert call["path"] == "/internal/v1/identity-authority/deny"
    assert call["purpose"] == "identity_authority"
    assert call["payload"] == {
        "identity_provider": "email",
        "external_subject_ref": _subject("email", "default"),
        "mapping_ref": MAPPING_REF,
        "team_ref": "TEM-01",
        "deny_through_revision": 4,
        "reason": reason,
        "idempotency_key": call["idempotency_key"],
    }
    assert call["payload"]["deny_through_revision"] < mapping.revision
    assert "member@example.invalid" not in repr(call)


def test_authority_loss_is_rejected_when_observer_denial_is_not_durable(
    authority_module: tuple[Any, _Frappe],
) -> None:
    module, fake = authority_module
    before = SimpleNamespace(
        revision=4,
        review_status="Approved",
        business_status="Active",
        identity_provider="email",
        external_subject=_subject("email", "default"),
        identity_type="User",
        user="member@example.invalid",
        party_profile=None,
        team="TEM-01",
    )
    mapping = _identity(
        module,
        _before=before,
        revision=4,
        review_status="Approved",
        business_status="Revoked",
    )
    mapping.flags.gbos_identity_status_command = True
    fake.observer_error = RuntimeError("private observer failure")

    with pytest.raises(RuntimeError, match="private observer failure"):
        mapping.validate()

    assert len(fake.observer_calls) == 1


def test_invalid_status_bypass_cannot_emit_an_observer_denial(
    authority_module: tuple[Any, _Frappe],
) -> None:
    module, fake = authority_module
    before = SimpleNamespace(
        revision=4,
        review_status="Approved",
        business_status="Active",
        identity_provider="email",
        external_subject=_subject("email", "default"),
        identity_type="User",
        user="member@example.invalid",
        party_profile=None,
        team="TEM-01",
    )
    mapping = _identity(
        module,
        _before=before,
        revision=4,
        review_status="Approved",
        business_status="Revoked",
    )

    with pytest.raises(_PermissionError):
        mapping.validate()

    assert fake.observer_calls == []


@pytest.mark.parametrize(
    ("handler", "document", "method"),
    (
        (
            "deny_ineligible_user_mappings",
            SimpleNamespace(name="member@example.invalid", enabled=0),
            "on_update",
        ),
        (
            "deny_ineligible_user_mappings",
            SimpleNamespace(name="member@example.invalid", enabled=1),
            "on_trash",
        ),
        (
            "deny_ineligible_team_member_mappings",
            SimpleNamespace(
                parent="TEM-01",
                user="member@example.invalid",
                enabled=1,
                get_doc_before_save=lambda: None,
            ),
            "on_trash",
        ),
        (
            "deny_ineligible_team_member_mappings",
            SimpleNamespace(
                parent="TEM-02",
                user="member@example.invalid",
                enabled=1,
                get_doc_before_save=lambda: SimpleNamespace(
                    parent="TEM-01",
                    user="member@example.invalid",
                    enabled=1,
                ),
            ),
            "on_update",
        ),
        (
            "deny_ineligible_party_mappings",
            SimpleNamespace(name="PTY-01", team="TEM-OTHER"),
            "on_update",
        ),
        (
            "deny_ineligible_party_mappings",
            SimpleNamespace(name="PTY-01", team="TEM-01"),
            "on_trash",
        ),
    ),
)
def test_live_target_ineligibility_synchronously_denies_cached_confirmed_mapping(
    authority_module: tuple[Any, _Frappe],
    handler: str,
    document: object,
    method: str,
) -> None:
    module, fake = authority_module
    fake.mappings = [
        {
            "name": MAPPING_REF,
            "revision": 4,
            "team": "TEM-01",
            "identity_provider": "email",
            "external_subject": _subject("email", "default"),
            "identity_type": "Party" if handler.endswith("party_mappings") else "User",
            "user": None if handler.endswith("party_mappings") else "member@example.invalid",
            "party_profile": "PTY-01" if handler.endswith("party_mappings") else None,
            "review_status": "Approved",
            "business_status": "Active",
        }
    ]

    getattr(module, handler)(document, method)

    assert len(fake.observer_calls) == 1
    call = fake.observer_calls[0]
    assert call["payload"]["reason"] == "target_ineligible"
    assert call["payload"]["deny_through_revision"] == 4
    assert "member@example.invalid" not in repr(call)
    assert "PTY-01" not in repr(call)


def test_target_ineligibility_denial_failure_raises_to_abort_the_frappe_transaction(
    authority_module: tuple[Any, _Frappe],
) -> None:
    module, fake = authority_module
    fake.mappings = [
        {
            "name": MAPPING_REF,
            "revision": 4,
            "team": "TEM-01",
            "identity_provider": "email",
            "external_subject": _subject("email", "default"),
            "identity_type": "User",
            "user": "member@example.invalid",
            "party_profile": None,
            "review_status": "Approved",
            "business_status": "Active",
        }
    ]
    fake.observer_error = RuntimeError("private observer failure")

    with pytest.raises(RuntimeError, match="private observer failure"):
        module.deny_ineligible_user_mappings(
            SimpleNamespace(name="member@example.invalid", enabled=0),
            "on_update",
        )

    assert len(fake.observer_calls) == 1


def test_team_parent_save_denies_a_removed_child_membership_before_commit(
    authority_module: tuple[Any, _Frappe],
) -> None:
    module, fake = authority_module
    fake.mappings = [
        {
            "name": MAPPING_REF,
            "revision": 4,
            "team": "TEM-01",
            "identity_provider": "email",
            "external_subject": _subject("email", "default"),
            "identity_type": "User",
            "user": "member@example.invalid",
            "party_profile": None,
            "review_status": "Approved",
            "business_status": "Active",
        }
    ]
    before = SimpleNamespace(members=[SimpleNamespace(user="member@example.invalid", enabled=1)])
    team = SimpleNamespace(
        name="TEM-01",
        members=[],
        get_doc_before_save=lambda: before,
    )

    module.deny_removed_team_member_mappings(team, "on_update")

    assert len(fake.observer_calls) == 1
    assert fake.observer_calls[0]["payload"]["reason"] == "target_ineligible"
    assert "member@example.invalid" not in repr(fake.observer_calls[0])


def test_authority_loss_enumeration_does_not_leave_mappings_past_one_page_stale(
    authority_module: tuple[Any, _Frappe],
) -> None:
    module, fake = authority_module
    fake.mappings = [
        {
            "name": f"mapping-{index}",
            "review_status": "Approved",
            "business_status": "Active",
        }
        for index in range(501)
    ]

    rows = module._active_authority_mappings({})

    assert len(rows) == 501


def test_live_target_authority_hooks_cover_disable_transfer_and_delete_events() -> None:
    source = HOOKS.read_text(encoding="utf-8")

    assert 'doc_events["User"]["on_update"]' in source
    assert 'doc_events["User"]["on_trash"]' in source
    assert 'doc_events["GBOS Team Member"]' in source
    assert 'doc_events["GBOS Team"]' in source
    assert 'doc_events["GBOS Party Profile"]' in source
    assert "deny_ineligible_user_mappings" in source
    assert "deny_ineligible_team_member_mappings" in source
    assert "deny_removed_team_member_mappings" in source
    assert "deny_ineligible_party_mappings" in source
