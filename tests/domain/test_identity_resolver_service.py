from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Generator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

USER = "gbos-identity-resolver@localhost.invalid"
ROLE = "Observer Identity Resolver"
AUTH_REF = "observer-identity-resolver-v1"
API_KEY = "ResolverKey_0123456789ABCDEF"
API_SECRET = "ResolverSecret_0123456789ABCDEF"


def _credential_file(path: Path, value: str, *, mode: int = 0o400) -> Path:
    path.write_bytes(f"{value}\n".encode())
    os.chmod(path, mode)
    return path


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
        self.runtime.insertions += 1
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
        self.insertions = 0
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
    tmp_path: Path,
) -> Generator[tuple[Any, _Frappe]]:
    monkeypatch.delenv("GBOS_PRODUCTION_ENABLED", raising=False)
    monkeypatch.delenv("GBOS_LOCAL_PILOT_SITE_ID", raising=False)
    monkeypatch.delenv("GBOS_IDENTITY_RESOLVER_API_KEY", raising=False)
    monkeypatch.delenv("GBOS_IDENTITY_RESOLVER_API_SECRET", raising=False)
    secret_dir = tmp_path / "run" / "secrets"
    secret_dir.mkdir(parents=True)
    api_key_file = _credential_file(secret_dir / "frappe_identity_resolver_api_key", API_KEY)
    api_secret_file = _credential_file(
        secret_dir / "frappe_identity_resolver_api_secret", API_SECRET
    )
    monkeypatch.setenv("GBOS_IDENTITY_RESOLVER_API_KEY_FILE", str(api_key_file))
    monkeypatch.setenv("GBOS_IDENTITY_RESOLVER_API_SECRET_FILE", str(api_secret_file))
    fake = _Frappe()
    original_frappe = sys.modules.get("frappe")
    original_module = sys.modules.pop("esan_gbos.identity_resolver_service", None)
    sys.modules["frappe"] = fake
    module = importlib.import_module("esan_gbos.identity_resolver_service")
    monkeypatch.setattr(module, "_SECRET_DIRECTORY", secret_dir, raising=False)
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
    assert fake.insertions == 1
    assert API_KEY not in repr(receipt)
    assert API_SECRET not in repr(receipt)


def test_provisioner_accepts_exact_secret_paths_from_frappe_config(
    monkeypatch: pytest.MonkeyPatch,
    service_helper: tuple[Any, _Frappe],
) -> None:
    helper, fake = service_helper
    api_key_path = os.environ["GBOS_IDENTITY_RESOLVER_API_KEY_FILE"]
    api_secret_path = os.environ["GBOS_IDENTITY_RESOLVER_API_SECRET_FILE"]
    monkeypatch.delenv("GBOS_IDENTITY_RESOLVER_API_KEY_FILE")
    monkeypatch.delenv("GBOS_IDENTITY_RESOLVER_API_SECRET_FILE")
    fake.conf["gbos_identity_resolver_api_key_file"] = api_key_path
    fake.conf["gbos_identity_resolver_api_secret_file"] = api_secret_path

    receipt = helper.provision_identity_resolver(True)

    assert receipt["status"] == "created"
    assert fake.insertions == 1
    assert api_key_path not in repr(receipt)
    assert api_secret_path not in repr(receipt)


def test_provisioner_accepts_mode_0600_secret_files(
    service_helper: tuple[Any, _Frappe],
) -> None:
    helper, fake = service_helper
    os.chmod(os.environ["GBOS_IDENTITY_RESOLVER_API_KEY_FILE"], 0o600)
    os.chmod(os.environ["GBOS_IDENTITY_RESOLVER_API_SECRET_FILE"], 0o600)

    receipt = helper.provision_identity_resolver(True)

    assert receipt["status"] == "created"
    assert fake.insertions == 1


@pytest.mark.parametrize(
    ("case", "mode", "payload"),
    (
        ("group-readable", 0o640, f"{API_KEY}\n".encode()),
        ("oversize", 0o400, b"x" * 4097),
        ("multiple-lines", 0o400, f"{API_KEY}\nsecond-line\n".encode()),
        ("carriage-return", 0o400, f"{API_KEY}\r\n".encode()),
        ("invalid-utf8", 0o400, b"\xff"),
        ("empty", 0o400, b""),
    ),
)
def test_provisioner_rejects_unsafe_secret_file_mode_size_or_content_without_leaks(
    service_helper: tuple[Any, _Frappe],
    case: str,
    mode: int,
    payload: bytes,
) -> None:
    del case
    helper, fake = service_helper
    path = Path(os.environ["GBOS_IDENTITY_RESOLVER_API_KEY_FILE"])
    os.chmod(path, 0o600)
    path.write_bytes(payload)
    os.chmod(path, mode)

    with pytest.raises(
        helper.IdentityResolverProvisioningError,
        match="credential file is invalid",
    ) as raised:
        helper.provision_identity_resolver(True)

    assert str(path) not in str(raised.value)
    assert API_KEY not in repr(raised.value)
    assert "second-line" not in repr(raised.value)
    assert fake.user is None
    assert fake.insertions == 0
    assert fake.commits == 0


def test_provisioner_rejects_symlink_secret_file_without_path_leakage(
    service_helper: tuple[Any, _Frappe],
) -> None:
    helper, fake = service_helper
    path = Path(os.environ["GBOS_IDENTITY_RESOLVER_API_KEY_FILE"])
    target = path.with_name("credential-target-SENTINEL")
    target.write_text(f"{API_KEY}\n", encoding="utf-8")
    os.chmod(target, 0o400)
    path.unlink()
    path.symlink_to(target)

    with pytest.raises(
        helper.IdentityResolverProvisioningError,
        match="credential file is invalid",
    ) as raised:
        helper.provision_identity_resolver(True)

    assert str(path) not in repr(raised.value)
    assert str(target) not in repr(raised.value)
    assert fake.insertions == 0
    assert fake.commits == 0


def test_provisioner_rejects_short_secret_file_read_without_partial_value_leakage(
    monkeypatch: pytest.MonkeyPatch,
    service_helper: tuple[Any, _Frappe],
) -> None:
    helper, fake = service_helper
    reads = iter((b"PartialSecretSentinel", b""))
    monkeypatch.setattr(helper.os, "read", lambda _descriptor, _maximum: next(reads))

    with pytest.raises(
        helper.IdentityResolverProvisioningError,
        match="credential file is invalid",
    ) as raised:
        helper.provision_identity_resolver(True)

    assert "PartialSecretSentinel" not in repr(raised.value)
    assert fake.insertions == 0
    assert fake.commits == 0


@pytest.mark.parametrize(
    "unsafe_path",
    (
        "/tmp/frappe_identity_resolver_api_key",
        "/run/secrets/nested/frappe_identity_resolver_api_key",
        "/run/secrets/../frappe_identity_resolver_api_key",
        "/run/secrets/.hidden",
        "relative-secret",
    ),
)
def test_provisioner_rejects_paths_outside_one_safe_secret_filename(
    monkeypatch: pytest.MonkeyPatch,
    service_helper: tuple[Any, _Frappe],
    unsafe_path: str,
) -> None:
    helper, fake = service_helper
    monkeypatch.setenv("GBOS_IDENTITY_RESOLVER_API_KEY_FILE", unsafe_path)

    with pytest.raises(
        helper.IdentityResolverProvisioningError,
        match="credential file is invalid",
    ) as raised:
        helper.provision_identity_resolver(True)

    assert unsafe_path not in repr(raised.value)
    assert fake.insertions == 0
    assert fake.commits == 0


def test_provisioner_rejects_legacy_inline_secret_environment_before_db_mutation(
    monkeypatch: pytest.MonkeyPatch,
    service_helper: tuple[Any, _Frappe],
) -> None:
    helper, fake = service_helper
    monkeypatch.setenv("GBOS_IDENTITY_RESOLVER_API_KEY", API_KEY)
    monkeypatch.setenv("GBOS_IDENTITY_RESOLVER_API_SECRET", API_SECRET)

    with pytest.raises(
        helper.IdentityResolverProvisioningError,
        match="legacy credential environment is not allowed",
    ) as raised:
        helper.provision_identity_resolver(True)

    assert API_KEY not in repr(raised.value)
    assert API_SECRET not in repr(raised.value)
    assert fake.user is None
    assert fake.insertions == 0
    assert fake.commits == 0


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
    assert fake.insertions == 0
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
    assert fake.insertions == 0
    assert fake.rollbacks == 1
