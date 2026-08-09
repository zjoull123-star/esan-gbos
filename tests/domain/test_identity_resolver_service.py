from __future__ import annotations

import importlib
import sys
from collections.abc import Generator
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

USER = "gbos-identity-resolver@localhost.invalid"
ROLE = "Observer Identity Resolver"
AUTH_REF = "observer-identity-resolver-v1"
API_KEY = "ResolverKey_0123456789ABCDEF"
API_SECRET = "ResolverSecret_0123456789ABCDEF"


class _Database:
    def __init__(self, runtime: _Frappe) -> None:
        self.runtime = runtime

    def exists(self, doctype: str, name: str) -> bool:
        if doctype == "Role":
            return name == ROLE and self.runtime.role_exists
        if doctype == "User":
            return name == USER and self.runtime.user is not None
        return False

    def get_value(self, doctype: str, name: str, fieldname: str) -> Any:
        if (doctype, name, fieldname) == ("Role", ROLE, "desk_access"):
            return self.runtime.role_desk_access
        raise AssertionError(f"unexpected get_value: {doctype}.{name}.{fieldname}")

    def commit(self) -> None:
        self.runtime.commits += 1

    def rollback(self) -> None:
        self.runtime.rollbacks += 1


class _User:
    def __init__(self, runtime: _Frappe, values: dict[str, Any], secret: str | None = None) -> None:
        self.runtime = runtime
        self.values = dict(values)
        self.secret = secret
        self.flags = SimpleNamespace()

    def get(self, fieldname: str, default: Any = None) -> Any:
        return self.values.get(fieldname, default)

    def get_password(self, fieldname: str, raise_exception: bool = True) -> str | None:
        assert fieldname == "api_secret"
        del raise_exception
        return self.secret

    def insert(self, *, ignore_permissions: bool) -> _User:
        assert ignore_permissions is True
        roles = {str(row["role"]) for row in self.get("roles", [])}
        self.values["user_type"] = (
            "System User" if self.runtime.role_desk_access and roles else "Website User"
        )
        self.secret = str(self.get("api_secret"))
        self.values["api_secret"] = "*" * len(self.secret)
        self.values["name"] = self.get("email")
        self.runtime.user = self
        return self


class _Frappe(ModuleType):
    def __init__(self) -> None:
        super().__init__("frappe")
        self.local = SimpleNamespace(site="gbos.localhost")
        self.conf: dict[str, Any] = {
            "gbos_identity_resolver_identities": {
                AUTH_REF: {
                    "user": USER,
                    "site_id": "gbos.localhost",
                    "processing_purposes": ["identity_resolution"],
                }
            }
        }
        self.role_exists = True
        self.role_desk_access = 0
        self.user: _User | None = None
        self.commits = 0
        self.rollbacks = 0
        self.db = _Database(self)

    def get_doc(self, doctype: str | dict[str, Any], name: str | None = None) -> _User:
        if isinstance(doctype, dict):
            assert doctype["doctype"] == "User"
            return _User(self, doctype)
        assert (doctype, name) == ("User", USER)
        if self.user is None:
            raise RuntimeError("missing user")
        return self.user


@pytest.fixture
def service_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[tuple[Any, _Frappe]]:
    monkeypatch.delenv("GBOS_PRODUCTION_ENABLED", raising=False)
    monkeypatch.delenv("GBOS_LOCAL_PILOT_SITE_ID", raising=False)
    monkeypatch.setenv("GBOS_IDENTITY_RESOLVER_API_KEY", API_KEY)
    monkeypatch.setenv("GBOS_IDENTITY_RESOLVER_API_SECRET", API_SECRET)
    fake = _Frappe()
    original_frappe = sys.modules.get("frappe")
    original_module = sys.modules.pop("esan_gbos.identity_resolver_service", None)
    sys.modules["frappe"] = fake
    module = importlib.import_module("esan_gbos.identity_resolver_service")
    yield module, fake
    sys.modules.pop("esan_gbos.identity_resolver_service", None)
    if original_module is not None:
        sys.modules["esan_gbos.identity_resolver_service"] = original_module
    if original_frappe is None:
        sys.modules.pop("frappe", None)
    else:
        sys.modules["frappe"] = original_frappe


def test_provisioner_requires_explicit_local_confirmation_and_rolls_back(
    service_helper: tuple[Any, _Frappe],
) -> None:
    helper, fake = service_helper

    with pytest.raises(helper.IdentityResolverProvisioningError, match="confirmation"):
        helper.provision_identity_resolver(False)

    assert fake.user is None
    assert fake.commits == 0
    assert fake.rollbacks == 1


def test_provisioner_creates_a_distinct_no_desk_exact_role_identity(
    service_helper: tuple[Any, _Frappe],
) -> None:
    helper, fake = service_helper

    receipt = helper.provision_identity_resolver(True)

    assert receipt == {
        "status": "created",
        "user": USER,
        "role": ROLE,
        "auth_ref": AUTH_REF,
        "site_id": "gbos.localhost",
    }
    assert fake.user is not None
    assert fake.user.get("enabled") == 1
    assert fake.user.get("send_welcome_email") == 0
    assert fake.user.get("user_type") == "Website User"
    assert {row["role"] for row in fake.user.get("roles")} == {ROLE}
    assert fake.user.get("role_profile_name") is None
    assert fake.user.get("role_profiles") == []
    assert fake.commits == 1
    assert API_KEY not in repr(receipt)
    assert API_SECRET not in repr(receipt)


@pytest.mark.parametrize(
    ("role_exists", "desk_access"),
    ((False, 0), (True, 1)),
)
def test_provisioner_refuses_missing_or_desk_enabled_role(
    service_helper: tuple[Any, _Frappe],
    role_exists: bool,
    desk_access: int,
) -> None:
    helper, fake = service_helper
    fake.role_exists = role_exists
    fake.role_desk_access = desk_access

    with pytest.raises(helper.IdentityResolverProvisioningError, match="service role"):
        helper.provision_identity_resolver(True)

    assert fake.user is None
    assert fake.commits == 0
    assert fake.rollbacks == 1


def test_existing_identity_with_any_role_or_credential_drift_is_not_repaired(
    service_helper: tuple[Any, _Frappe],
) -> None:
    helper, fake = service_helper
    fake.user = _User(
        fake,
        {
            "doctype": "User",
            "name": USER,
            "email": USER,
            "enabled": 1,
            "user_type": "Website User",
            "send_welcome_email": 0,
            "api_key": API_KEY,
            "api_secret": "*" * len(API_SECRET),
            "roles": [{"role": ROLE}, {"role": "Agent TrustedMaterializer"}],
            "role_profile_name": None,
            "role_profiles": [],
        },
        API_SECRET,
    )

    with pytest.raises(helper.IdentityResolverProvisioningError, match="identity drift"):
        helper.provision_identity_resolver(True)

    assert fake.commits == 0
    assert fake.rollbacks == 1
