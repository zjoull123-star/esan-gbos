from __future__ import annotations

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

    @staticmethod
    def throw(message: str, **kwargs: Any) -> None:
        del kwargs
        raise _ValidationError(message)


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
    sys.modules["frappe"] = fake_frappe
    sys.modules["esan_gbos.gbos.doctype.base"] = fake_base
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


@pytest.mark.parametrize(
    ("provider", "subject"),
    (
        ("email", "extid:v1:email:Opaque_01~-token"),
        ("wecom", "extid:v1:wecom:Opaque01"),
        ("whatsapp", "extid:v1:whatsapp:Opaque01"),
        ("phone", "extid:v1:phone:Opaque01"),
        ("manual_import", "extid:v1:manual_import:Opaque01"),
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
        "name": "EID-01",
        "team": "TEM-01",
        "identity_provider": "email",
        "external_subject": "extid:v1:email:Opaque01",
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
        external_subject="extid:v1:email:Opaque01",
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
