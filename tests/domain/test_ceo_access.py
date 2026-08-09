from __future__ import annotations

import importlib
import importlib.util
import sys
from dataclasses import dataclass
from types import ModuleType
from typing import Any

import pytest


@dataclass
class _RoleRow:
    role: str


class _User:
    def __init__(self, name: str, roles: tuple[str, ...], *, user_type: str = "System User"):
        self.name = name
        self.roles = [_RoleRow(role) for role in roles]
        self.user_type = user_type
        self.save_calls: list[bool] = []

    def get(self, fieldname: str) -> list[_RoleRow]:
        assert fieldname == "roles"
        return self.roles

    def append(self, fieldname: str, value: dict[str, str]) -> None:
        assert fieldname == "roles"
        self.roles.append(_RoleRow(value["role"]))

    def save(self, *, ignore_permissions: bool = False) -> _User:
        self.save_calls.append(ignore_permissions)
        return self


class _Frappe(ModuleType):
    def __init__(self, users: dict[str, _User], ceo_names: list[str]) -> None:
        super().__init__("frappe")
        self.users = users
        self.ceo_names = ceo_names
        self.get_all_calls: list[tuple[str, dict[str, str], str]] = []

    def get_all(
        self,
        doctype: str,
        *,
        filters: dict[str, str],
        pluck: str,
    ) -> list[str]:
        self.get_all_calls.append((doctype, filters, pluck))
        return list(self.ceo_names)

    def get_doc(self, doctype: str, name: str) -> _User:
        assert doctype == "User"
        return self.users[name]


def _load_module(fake: _Frappe, monkeypatch: pytest.MonkeyPatch) -> Any:
    assert importlib.util.find_spec("esan_gbos.ceo_access") is not None
    monkeypatch.setitem(sys.modules, "frappe", fake)
    monkeypatch.delitem(sys.modules, "esan_gbos.ceo_access", raising=False)
    return importlib.import_module("esan_gbos.ceo_access")


def _role_names(user: _User) -> set[str]:
    return {row.role for row in user.roles}


def test_ceo_role_bundle_is_closed_and_includes_frappe_system_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module(_Frappe({}, []), monkeypatch)

    assert module.CEO_FULL_ACCESS_ROLES == (
        "CEO",
        "GBOS Admin",
        "Integration Admin",
        "Reviewer",
        "System Manager",
    )


def test_ceo_user_is_promoted_idempotently_without_losing_existing_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module(_Frappe({}, []), monkeypatch)
    user = _User("ceo@example.invalid", ("CEO", "Finance Readonly"), user_type="Website User")

    assert module.ensure_ceo_full_access(user) is True
    assert _role_names(user) == {
        "CEO",
        "Finance Readonly",
        "GBOS Admin",
        "Integration Admin",
        "Reviewer",
        "System Manager",
    }
    assert user.user_type == "System User"

    assert module.ensure_ceo_full_access(user) is False
    assert len(user.roles) == len(_role_names(user))


def test_non_ceo_user_is_never_elevated(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_module(_Frappe({}, []), monkeypatch)
    user = _User("sales@example.invalid", ("Sales User",))

    assert module.ensure_ceo_full_access(user) is False
    assert _role_names(user) == {"Sales User"}


def test_migration_backfill_saves_only_ceos_missing_the_full_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incomplete = _User("ceo-a@example.invalid", ("CEO",), user_type="Website User")
    complete = _User(
        "ceo-b@example.invalid",
        ("CEO", "GBOS Admin", "Integration Admin", "Reviewer", "System Manager"),
    )
    fake = _Frappe(
        {incomplete.name: incomplete, complete.name: complete},
        [complete.name, incomplete.name, incomplete.name],
    )
    module = _load_module(fake, monkeypatch)

    assert module.backfill_ceo_full_access() == 1
    assert incomplete.save_calls == [True]
    assert complete.save_calls == []
    assert fake.get_all_calls == [
        (
            "Has Role",
            {"role": "CEO", "parenttype": "User", "parentfield": "roles"},
            "parent",
        )
    ]
