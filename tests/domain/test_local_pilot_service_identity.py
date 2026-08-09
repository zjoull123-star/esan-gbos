from __future__ import annotations

import importlib
import sys
from collections.abc import Generator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]
MODULE_PATH = ROOT / "apps" / "esan_gbos" / "esan_gbos" / "local_pilot.py"
USER = "gbos-materializer@localhost.invalid"
ROLE = "Agent TrustedMaterializer"
AUTH_REF = "agent-materializer-v1"
API_KEY = "MaterializerKey_0123456789ABCDEF"
API_SECRET = "MaterializerSecret_0123456789ABCDEF"
PURPOSES = [
    "observation_processing",
    "sales_follow_up",
    "procurement_coordination",
    "product_sample_management",
    "metric_reporting",
]


class _Database:
    def __init__(self, runtime: _Frappe) -> None:
        self._runtime = runtime

    def exists(self, doctype: str, name: str) -> bool:
        if doctype == "Role":
            return name == ROLE and self._runtime.role_exists
        if doctype == "User":
            return name == USER and self._runtime.user is not None
        return False

    def get_value(self, doctype: str, name: str, fieldname: str) -> Any:
        if (doctype, name, fieldname) == ("Role", ROLE, "desk_access"):
            return self._runtime.role_desk_access
        raise AssertionError(f"unexpected get_value: {doctype}.{name}.{fieldname}")

    def commit(self) -> None:
        self._runtime.commits += 1

    def rollback(self) -> None:
        self._runtime.rollbacks += 1


class _User:
    def __init__(
        self,
        runtime: _Frappe,
        values: dict[str, Any],
        *,
        stored_secret: str | None = None,
    ) -> None:
        self._runtime = runtime
        self._values = dict(values)
        self._stored_secret = stored_secret
        self.flags = SimpleNamespace()

    def get(self, fieldname: str, default: Any = None) -> Any:
        return self._values.get(fieldname, default)

    def get_password(self, fieldname: str, raise_exception: bool = True) -> str | None:
        assert fieldname == "api_secret"
        del raise_exception
        return self._stored_secret

    def insert(self, *, ignore_permissions: bool = False) -> _User:
        self._runtime.insert_permissions.append(ignore_permissions)
        if self._runtime.insert_error is not None:
            raise self._runtime.insert_error
        if self._runtime.user is not None:
            raise RuntimeError("duplicate user")
        roles = {_role_name(row) for row in self.get("roles", [])}
        self._values["user_type"] = (
            "System User" if self._runtime.role_desk_access and roles else "Website User"
        )
        secret = self.get("api_secret")
        assert isinstance(secret, str)
        self._stored_secret = secret
        self._values["api_secret"] = "*" * len(secret)
        self._runtime.user = self
        self._runtime.inserts += 1
        return self

    def save(self, *, ignore_permissions: bool = False) -> _User:
        self._runtime.save_permissions.append(ignore_permissions)
        self._runtime.saves += 1
        return self


def _role_name(row: Any) -> str:
    if isinstance(row, dict):
        return str(row.get("role") or "")
    return str(getattr(row, "role", ""))


class _Frappe(ModuleType):
    def __init__(self) -> None:
        super().__init__("frappe")
        self.local = SimpleNamespace(site="gbos.localhost")
        self.conf: dict[str, Any] = {
            "gbos_agent_materialization_identities": {
                AUTH_REF: {
                    "user": USER,
                    "site_id": "gbos.localhost",
                    "processing_purposes": list(PURPOSES),
                }
            }
        }
        self.role_exists = True
        self.role_desk_access = 0
        self.user: _User | None = None
        self.insert_error: Exception | None = None
        self.inserts = 0
        self.saves = 0
        self.commits = 0
        self.rollbacks = 0
        self.insert_permissions: list[bool] = []
        self.save_permissions: list[bool] = []
        self.db = _Database(self)

    def get_doc(self, doctype: str | dict[str, Any], name: str | None = None) -> _User:
        if isinstance(doctype, dict):
            assert doctype.get("doctype") == "User"
            return _User(self, doctype)
        assert (doctype, name) == ("User", USER)
        if self.user is None:
            raise RuntimeError("user not found")
        return self.user


def _existing_user(
    fake: _Frappe,
    *,
    api_key: str = API_KEY,
    api_secret: str = API_SECRET,
    roles: tuple[str, ...] = (ROLE,),
    enabled: int = 1,
    user_type: str = "Website User",
    send_welcome_email: int = 0,
    role_profile_name: str | None = None,
    role_profiles: tuple[str, ...] = (),
) -> _User:
    user = _User(
        fake,
        {
            "doctype": "User",
            "name": USER,
            "email": USER,
            "enabled": enabled,
            "user_type": user_type,
            "send_welcome_email": send_welcome_email,
            "api_key": api_key,
            "api_secret": "*" * len(api_secret),
            "roles": [{"role": role} for role in roles],
            "role_profile_name": role_profile_name,
            "role_profiles": [{"role_profile": role_profile} for role_profile in role_profiles],
        },
        stored_secret=api_secret,
    )
    fake.user = user
    return user


@pytest.fixture
def local_pilot(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[tuple[Any, _Frappe]]:
    monkeypatch.delenv("GBOS_PRODUCTION_ENABLED", raising=False)
    monkeypatch.delenv("GBOS_LOCAL_PILOT_SITE_ID", raising=False)
    monkeypatch.setenv("GBOS_MATERIALIZER_API_KEY", API_KEY)
    monkeypatch.setenv("GBOS_MATERIALIZER_API_SECRET", API_SECRET)
    fake = _Frappe()
    original_frappe = sys.modules.get("frappe")
    sys.modules["frappe"] = fake
    sys.modules.pop("esan_gbos.local_pilot", None)
    module = importlib.import_module("esan_gbos.local_pilot")
    yield module, fake
    sys.modules.pop("esan_gbos.local_pilot", None)
    if original_frappe is None:
        sys.modules.pop("frappe", None)
    else:
        sys.modules["frappe"] = original_frappe


def test_local_pilot_helper_exists_before_behavior_is_loaded() -> None:
    assert MODULE_PATH.exists(), "bench-only materializer helper is missing"


def test_provisioning_requires_literal_true_and_rolls_back(
    local_pilot: tuple[Any, _Frappe],
) -> None:
    module, fake = local_pilot

    for confirmation in (False, 1, "true", None):
        with pytest.raises(module.LocalPilotProvisioningError, match="confirmation required"):
            module.provision_materializer(confirmation)

    assert fake.inserts == 0
    assert fake.commits == 0
    assert fake.rollbacks == 4


@pytest.mark.parametrize("production_value", ["true", "TRUE", "1", "yes", " yes "])
def test_provisioning_refuses_production_environment(
    local_pilot: tuple[Any, _Frappe],
    monkeypatch: pytest.MonkeyPatch,
    production_value: str,
) -> None:
    module, fake = local_pilot
    monkeypatch.setenv("GBOS_PRODUCTION_ENABLED", production_value)

    with pytest.raises(module.LocalPilotProvisioningError, match="production environment"):
        module.provision_materializer(True)

    assert fake.inserts == 0
    assert fake.commits == 0
    assert fake.rollbacks == 1


def test_nondefault_site_requires_an_exact_closed_site_environment(
    local_pilot: tuple[Any, _Frappe],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = local_pilot
    fake.local.site = "pilot-01.localhost"
    fake.conf["gbos_agent_materialization_identities"][AUTH_REF]["site_id"] = "pilot-01.localhost"

    with pytest.raises(module.LocalPilotProvisioningError, match="site is not allowed"):
        module.provision_materializer(True)

    monkeypatch.setenv("GBOS_LOCAL_PILOT_SITE_ID", "pilot-01.localhost")
    receipt = module.provision_materializer(True)

    assert receipt["site_id"] == "pilot-01.localhost"
    assert fake.rollbacks == 1
    assert fake.commits == 1


def test_explicit_site_environment_must_not_drift_even_on_default_site(
    local_pilot: tuple[Any, _Frappe],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = local_pilot
    monkeypatch.setenv("GBOS_LOCAL_PILOT_SITE_ID", "other.localhost")

    with pytest.raises(module.LocalPilotProvisioningError, match="site is not allowed"):
        module.provision_materializer(True)

    assert fake.inserts == 0
    assert fake.commits == 0
    assert fake.rollbacks == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("user", "other@localhost.invalid"),
        ("site_id", "other.localhost"),
        ("processing_purposes", ["observation_processing"]),
        ("processing_purposes", ["sales_follow_up"]),
        ("processing_purposes", PURPOSES + ["metric_reporting"]),
        ("processing_purposes", PURPOSES + ["sales_follow_up"]),
    ],
)
def test_identity_config_drift_fails_closed(
    local_pilot: tuple[Any, _Frappe],
    field: str,
    value: Any,
) -> None:
    module, fake = local_pilot
    fake.conf["gbos_agent_materialization_identities"][AUTH_REF][field] = value

    with pytest.raises(module.LocalPilotProvisioningError, match="identity configuration"):
        module.provision_materializer(True)

    assert fake.inserts == 0
    assert fake.commits == 0
    assert fake.rollbacks == 1


@pytest.mark.parametrize(
    "configuration",
    [
        None,
        {},
        {"other-ref": {}},
        {AUTH_REF: {"user": USER, "site_id": "gbos.localhost"}},
        {
            AUTH_REF: {
                "user": USER,
                "site_id": "gbos.localhost",
                "processing_purposes": list(PURPOSES),
                "extra": "unsafe",
            }
        },
    ],
)
def test_identity_config_must_have_the_exact_auth_ref_shape(
    local_pilot: tuple[Any, _Frappe],
    configuration: Any,
) -> None:
    module, fake = local_pilot
    if configuration is None:
        fake.conf.clear()
    else:
        fake.conf["gbos_agent_materialization_identities"] = configuration

    with pytest.raises(module.LocalPilotProvisioningError, match="identity configuration"):
        module.provision_materializer(True)

    assert fake.inserts == 0
    assert fake.commits == 0
    assert fake.rollbacks == 1


@pytest.mark.parametrize(
    ("role_exists", "desk_access"),
    [(False, 0), (True, 1), (True, None)],
)
def test_trusted_materializer_role_must_exist_without_desk_access(
    local_pilot: tuple[Any, _Frappe],
    role_exists: bool,
    desk_access: Any,
) -> None:
    module, fake = local_pilot
    fake.role_exists = role_exists
    fake.role_desk_access = desk_access

    with pytest.raises(module.LocalPilotProvisioningError, match="service role"):
        module.provision_materializer(True)

    assert fake.inserts == 0
    assert fake.commits == 0
    assert fake.rollbacks == 1


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("GBOS_MATERIALIZER_API_KEY", None),
        ("GBOS_MATERIALIZER_API_SECRET", None),
        ("GBOS_MATERIALIZER_API_KEY", "a" * 14),
        ("GBOS_MATERIALIZER_API_SECRET", "a" * 129),
        ("GBOS_MATERIALIZER_API_KEY", "a" * 15 + ":"),
        ("GBOS_MATERIALIZER_API_SECRET", "a" * 15 + " "),
        ("GBOS_MATERIALIZER_API_KEY", "a" * 15 + "\r\n"),
        ("GBOS_MATERIALIZER_API_SECRET", "a" * 15 + "\x00"),
        ("GBOS_MATERIALIZER_API_SECRET", "密" * 16),
    ],
)
def test_credentials_are_read_only_from_strict_environment_values(
    local_pilot: tuple[Any, _Frappe],
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str | None,
) -> None:
    module, fake = local_pilot
    if value is None:
        monkeypatch.delenv(variable)
    elif "\x00" in value:
        environment = dict(module.os.environ)
        environment[variable] = value
        monkeypatch.setattr(module.os, "environ", environment)
    else:
        monkeypatch.setenv(variable, value)

    with pytest.raises(module.LocalPilotProvisioningError, match="credential is invalid") as error:
        module.provision_materializer(True)

    assert value is None or value not in repr(error.value)
    assert fake.inserts == 0
    assert fake.commits == 0
    assert fake.rollbacks == 1


def test_first_provision_uses_formal_password_field_and_no_desk_role(
    local_pilot: tuple[Any, _Frappe],
) -> None:
    module, fake = local_pilot

    receipt = module.provision_materializer(True)

    assert receipt == {
        "status": "created",
        "user": USER,
        "role": ROLE,
        "auth_ref": AUTH_REF,
        "site_id": "gbos.localhost",
    }
    assert API_KEY not in repr(receipt)
    assert API_SECRET not in repr(receipt)
    assert fake.user is not None
    assert fake.user.get("email") == USER
    assert fake.user.get("enabled") == 1
    assert fake.user.get("send_welcome_email") == 0
    assert fake.user.get("user_type") == "Website User"
    assert fake.user.get("api_key") == API_KEY
    assert fake.user.get("api_secret") == "*" * len(API_SECRET)
    assert fake.user.get_password("api_secret", raise_exception=False) == API_SECRET
    assert {_role_name(row) for row in fake.user.get("roles")} == {ROLE}
    assert fake.user.get("role_profile_name") in (None, "")
    assert fake.user.get("role_profiles", []) == []
    assert fake.user.flags.no_welcome_mail is True
    assert not hasattr(fake.user.flags, "ignore_validate")
    assert fake.insert_permissions == [True]
    assert fake.save_permissions == []
    assert fake.inserts == 1
    assert fake.saves == 0
    assert fake.commits == 1
    assert fake.rollbacks == 0


def test_exact_existing_identity_is_idempotently_skipped(
    local_pilot: tuple[Any, _Frappe],
) -> None:
    module, fake = local_pilot
    _existing_user(fake)

    receipt = module.provision_materializer(True)

    assert receipt["status"] == "skipped"
    assert set(receipt) == {"status", "user", "role", "auth_ref", "site_id"}
    assert API_KEY not in repr(receipt)
    assert API_SECRET not in repr(receipt)
    assert fake.inserts == 0
    assert fake.saves == 0
    assert fake.commits == 1
    assert fake.rollbacks == 0


@pytest.mark.parametrize(
    ("api_key", "api_secret"),
    [("DifferentKey_0123456789ABCDEF", API_SECRET), (API_KEY, "DifferentSecret_123456789")],
)
def test_existing_credential_drift_is_refused_without_rotation(
    local_pilot: tuple[Any, _Frappe],
    api_key: str,
    api_secret: str,
) -> None:
    module, fake = local_pilot
    original = _existing_user(fake, api_key=api_key, api_secret=api_secret)

    with pytest.raises(
        module.LocalPilotProvisioningError,
        match="existing identity drift",
    ) as error:
        module.provision_materializer(True)

    assert API_KEY not in repr(error.value)
    assert API_SECRET not in repr(error.value)
    assert original.get("api_key") == api_key
    assert original.get_password("api_secret", raise_exception=False) == api_secret
    assert fake.inserts == 0
    assert fake.saves == 0
    assert fake.commits == 0
    assert fake.rollbacks == 1


@pytest.mark.parametrize(
    "user_changes",
    [
        {"roles": (ROLE, "Administrator")},
        {"roles": (ROLE, "System Manager")},
        {"roles": (ROLE, "GBOS Admin")},
        {"roles": (ROLE, "Integration Admin")},
        {"roles": ()},
        {"enabled": 0},
        {"user_type": "System User"},
        {"send_welcome_email": 1},
        {"role_profile_name": "System Manager"},
        {"role_profiles": ("System Manager",)},
    ],
)
def test_existing_account_or_role_drift_is_refused_without_repair(
    local_pilot: tuple[Any, _Frappe],
    user_changes: dict[str, Any],
) -> None:
    module, fake = local_pilot
    _existing_user(fake, **user_changes)

    with pytest.raises(module.LocalPilotProvisioningError, match="existing identity drift"):
        module.provision_materializer(True)

    assert fake.inserts == 0
    assert fake.saves == 0
    assert fake.commits == 0
    assert fake.rollbacks == 1


def test_unexpected_frappe_errors_are_rolled_back_and_redacted(
    local_pilot: tuple[Any, _Frappe],
) -> None:
    module, fake = local_pilot
    fake.insert_error = RuntimeError(f"provider leaked {API_SECRET}")

    with pytest.raises(
        module.LocalPilotProvisioningError,
        match="materializer provisioning failed",
    ) as error:
        module.provision_materializer(True)

    assert API_SECRET not in repr(error.value)
    assert fake.commits == 0
    assert fake.rollbacks == 1


def test_helper_is_non_whitelisted_and_has_no_raw_secret_persistence_or_output() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")

    assert "frappe.whitelist" not in source
    assert "allow_guest" not in source
    assert "db_set" not in source
    assert "set_value" not in source
    assert "ignore_validate" not in source
    assert source.count("ignore_permissions=True") == 1
    assert '.get_password("api_secret", raise_exception=False)' in source
    assert "print(" not in source
    assert "logging" not in source
    assert ".logger" not in source
