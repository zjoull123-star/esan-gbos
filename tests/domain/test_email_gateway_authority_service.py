from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Generator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

USER = "email-gateway-authority@localhost.invalid"
ROLE = "Email Gateway Authority Consumer"
AUTH_REF = "email-gateway-authority-v1"
API_KEY = "GatewayAuthorityKey_0123456789"
API_SECRET = "GatewayAuthoritySecret_0123456789"


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
        raise AssertionError

    def commit(self) -> None:
        self.runtime.commits += 1

    def rollback(self) -> None:
        self.runtime.rollbacks += 1


class _User:
    def __init__(self, runtime: _Frappe, values: dict[str, Any]) -> None:
        self.runtime = runtime
        self.values = dict(values)
        self.flags = SimpleNamespace()
        self.secret: str | None = None

    def get(self, fieldname: str, default: Any = None) -> Any:
        return self.values.get(fieldname, default)

    def get_password(self, fieldname: str, raise_exception: bool = True) -> str | None:
        del fieldname, raise_exception
        return self.secret

    def insert(self, *, ignore_permissions: bool) -> _User:
        assert ignore_permissions is True
        self.secret = str(self.values["api_secret"])
        self.values["api_secret"] = "********"
        self.values["user_type"] = "Website User"
        self.values["name"] = self.values["email"]
        self.runtime.user = self
        return self


class _Frappe(ModuleType):
    def __init__(self) -> None:
        super().__init__("frappe")
        self.local = SimpleNamespace(site="gbos.localhost")
        self.conf = {
            "gbos_email_gateway_authority_identities": {
                AUTH_REF: {
                    "user": USER,
                    "site_id": "gbos.localhost",
                    "processing_purposes": ["email_gateway_authority"],
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
            return _User(self, doctype)
        assert (doctype, name) == ("User", USER)
        assert self.user is not None
        return self.user


@pytest.fixture
def service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Generator[tuple[Any, _Frappe]]:
    secret_dir = tmp_path / "run" / "secrets"
    secret_dir.mkdir(parents=True)
    key_path = secret_dir / "email_gateway_authority_key"
    secret_path = secret_dir / "email_gateway_authority_secret"
    key_path.write_text(API_KEY + "\n", encoding="utf-8")
    secret_path.write_text(API_SECRET + "\n", encoding="utf-8")
    os.chmod(key_path, 0o400)
    os.chmod(secret_path, 0o400)
    monkeypatch.setenv("GBOS_EMAIL_GATEWAY_AUTHORITY_API_KEY_FILE", str(key_path))
    monkeypatch.setenv("GBOS_EMAIL_GATEWAY_AUTHORITY_API_SECRET_FILE", str(secret_path))
    monkeypatch.delenv("GBOS_EMAIL_GATEWAY_AUTHORITY_API_KEY", raising=False)
    monkeypatch.delenv("GBOS_EMAIL_GATEWAY_AUTHORITY_API_SECRET", raising=False)
    fake = _Frappe()
    original_frappe = sys.modules.get("frappe")
    original_module = sys.modules.pop("esan_gbos.email_gateway_authority_service", None)
    sys.modules["frappe"] = fake
    module = importlib.import_module("esan_gbos.email_gateway_authority_service")
    monkeypatch.setattr(module, "_SECRET_DIRECTORY", secret_dir)
    yield module, fake
    sys.modules.pop("esan_gbos.email_gateway_authority_service", None)
    if original_module is not None:
        sys.modules["esan_gbos.email_gateway_authority_service"] = original_module
    if original_frappe is None:
        sys.modules.pop("frappe", None)
    else:
        sys.modules["frappe"] = original_frappe


def test_provisioner_creates_exact_deskless_single_role_identity(
    service: tuple[Any, _Frappe],
) -> None:
    helper, fake = service

    receipt = helper.provision_email_gateway_authority(True)

    assert receipt == {
        "status": "created",
        "user": USER,
        "role": ROLE,
        "auth_ref": AUTH_REF,
        "site_id": "gbos.localhost",
    }
    assert fake.user is not None
    assert fake.user.get("user_type") == "Website User"
    assert {row["role"] for row in fake.user.get("roles")} == {ROLE}
    assert API_KEY not in repr(receipt)
    assert API_SECRET not in repr(receipt)


def test_provisioner_rejects_inline_secret_environment(
    monkeypatch: pytest.MonkeyPatch,
    service: tuple[Any, _Frappe],
) -> None:
    helper, fake = service
    monkeypatch.setenv("GBOS_EMAIL_GATEWAY_AUTHORITY_API_KEY", API_KEY)

    with pytest.raises(helper.EmailGatewayAuthorityProvisioningError, match="legacy"):
        helper.provision_email_gateway_authority(True)

    assert fake.user is None
    assert fake.rollbacks == 1


def test_provisioner_rejects_non_deskless_role(service: tuple[Any, _Frappe]) -> None:
    helper, fake = service
    fake.role_desk_access = 1

    with pytest.raises(helper.EmailGatewayAuthorityProvisioningError, match="service role"):
        helper.provision_email_gateway_authority(True)


def test_provisioner_requires_explicit_local_confirmation(service: tuple[Any, _Frappe]) -> None:
    helper, fake = service

    with pytest.raises(helper.EmailGatewayAuthorityProvisioningError, match="confirmation"):
        helper.provision_email_gateway_authority(False)

    assert fake.user is None
