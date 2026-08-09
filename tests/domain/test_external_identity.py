from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Generator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]
DOCTYPE_DIR = (
    ROOT / "apps" / "esan_gbos" / "esan_gbos" / "gbos" / "doctype" / "gbos_external_identity"
)
CONTROLLER = DOCTYPE_DIR / "gbos_external_identity.py"
METADATA = DOCTYPE_DIR / "gbos_external_identity.json"


class _PermissionError(Exception):
    pass


class _Base:
    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)


@pytest.fixture
def external_identity_module() -> Generator[Any]:
    fake_frappe = ModuleType("frappe")
    fake_frappe.PermissionError = _PermissionError  # type: ignore[attr-defined]
    fake_base = ModuleType("esan_gbos.gbos.doctype.base")
    fake_base.GBOSDocument = _Base  # type: ignore[attr-defined]
    original_frappe = sys.modules.get("frappe")
    original_base = sys.modules.get("esan_gbos.gbos.doctype.base")
    sys.modules["frappe"] = fake_frappe
    sys.modules["esan_gbos.gbos.doctype.base"] = fake_base
    spec = importlib.util.spec_from_file_location("_external_identity_delete_test", CONTROLLER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    if original_frappe is None:
        sys.modules.pop("frappe", None)
    else:
        sys.modules["frappe"] = original_frappe
    if original_base is None:
        sys.modules.pop("esan_gbos.gbos.doctype.base", None)
    else:
        sys.modules["esan_gbos.gbos.doctype.base"] = original_base


def test_every_external_identity_docperm_denies_delete() -> None:
    metadata = json.loads(METADATA.read_text(encoding="utf-8"))

    assert metadata["permissions"]
    assert all(row.get("delete", 0) == 0 for row in metadata["permissions"])


@pytest.mark.parametrize(
    "review_status",
    ("AI Draft", "Pending", "Approved", "Rejected", "Superseded"),
)
@pytest.mark.parametrize("business_status", ("Active", "Revoked", "Archived"))
def test_every_mapping_lifecycle_hard_denies_physical_delete_without_echo(
    external_identity_module: Any,
    review_status: str,
    business_status: str,
) -> None:
    subject = "extid:v1:email:SensitiveOpaqueSubject"
    target = "sensitive-target@example.invalid"
    identity = external_identity_module.GBOSExternalIdentity(
        external_subject=subject,
        user=target,
        party_profile=None,
        review_status=review_status,
        business_status=business_status,
    )

    with pytest.raises(_PermissionError) as error:
        identity.on_trash()

    assert subject not in repr(error.value)
    assert target not in repr(error.value)


def test_delete_guard_does_not_introduce_endpoint_or_permission_bypass() -> None:
    source = CONTROLLER.read_text(encoding="utf-8")

    assert "ignore_permissions" not in source
    assert "allow_guest" not in source
