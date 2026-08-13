from __future__ import annotations

import importlib
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

TEAM_ONE = "TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV"
TEAM_TWO = "TEM-01ARZ3NDEKTSV4RRFFQ69G5FB0"


class FakeFrappe(ModuleType):
    def __init__(self) -> None:
        super().__init__("frappe")
        self.conf: dict[str, Any] = {}
        self.local = SimpleNamespace(site="gbos.localhost")
        self.session = SimpleNamespace(user="sales@example.invalid")
        self._roles = {"Sales User"}
        self._teams = [{"parent": TEAM_TWO}, {"parent": TEAM_ONE}]
        self.exists_calls: list[tuple[str, object]] = []
        self.db = SimpleNamespace(exists=self._exists)

    def get_roles(self) -> list[str]:
        return sorted(self._roles)

    def get_all(self, *_args: Any, **_kwargs: Any) -> list[dict[str, str]]:
        return self._teams

    def get_value(self, doctype: str, name: str, field: str) -> str | None:
        values = {
            ("GBOS Team", TEAM_ONE, "team_name"): "海湾销售组",
            ("GBOS Team", TEAM_TWO, "team_name"): "中国销售组",
            ("User", "owner@example.invalid", "full_name"): "邮箱负责人",
            ("User", "sales@example.invalid", "full_name"): "销售员",
        }
        return values.get((doctype, name, field))

    def _exists(self, doctype: str, filters: object) -> bool:
        self.exists_calls.append((doctype, filters))
        if doctype == "GBOS Team":
            return isinstance(filters, dict) and filters.get("name") in {TEAM_ONE, TEAM_TWO}
        if doctype == "User":
            return isinstance(filters, dict) and filters.get("name") in {
                "owner@example.invalid",
                "sales@example.invalid",
            }
        if doctype == "GBOS Team Member":
            return isinstance(filters, dict) and filters.get("parent") in {
                TEAM_ONE,
                TEAM_TWO,
            }
        return False

    def whitelist(self, **_kwargs: Any) -> Any:
        return lambda function: function


@pytest.fixture
def v5_modules(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, Any, FakeFrappe]:
    fake = FakeFrappe()
    common = ModuleType("esan_gbos.api.v1.common")

    class BFFError(Exception):
        def __init__(self, code: str, message: str, *, status: int = 400) -> None:
            super().__init__(message)
            self.code = code
            self.status = status

    def require_roles(allowed: set[str] | frozenset[str]) -> None:
        if not fake._roles & set(allowed):
            raise BFFError("permission_denied", "Role is not permitted", status=403)

    common.BFFError = BFFError
    common.bff_endpoint = lambda _method: lambda function: function
    common.require_roles = require_roles
    common.request_id = lambda: "REQ-v5-test"
    common.success = lambda data, schema_version, **meta: {
        "data": data,
        "meta": {"request_id": "REQ-v5-test", "schema_version": schema_version, **meta},
    }
    audit = ModuleType("esan_gbos.api.v1.audit")
    audit.run_idempotent = lambda _operation, _key, _payload, execute, **_kwargs: (
        execute(),
        False,
        None,
    )
    monkeypatch.setitem(sys.modules, "frappe", fake)
    monkeypatch.setitem(sys.modules, "esan_gbos.api.v1.common", common)
    monkeypatch.setitem(sys.modules, "esan_gbos.api.v1.audit", audit)
    for name in (
        "esan_gbos.api.v5.gateway",
        "esan_gbos.api.v5.email_admin",
        "esan_gbos.api.v5.email_inbox",
    ):
        sys.modules.pop(name, None)
    gateway = importlib.import_module("esan_gbos.api.v5.gateway")
    admin = importlib.import_module("esan_gbos.api.v5.email_admin")
    inbox = importlib.import_module("esan_gbos.api.v5.email_inbox")
    return gateway, admin, inbox, fake


def mailbox_payload() -> dict[str, Any]:
    return {
        "mailbox_ref": "MBX-01",
        "display_label": "海湾销售主入口",
        "provider_kind": "fake",
        "business_mode": "primary",
        "business_purpose": "sales_follow_up",
        "default_team_label": "海湾销售组",
        "account_owner_label": "邮箱负责人",
        "inbound_enabled": True,
        "outbound_enabled": False,
        "status": "active",
        "config_revision": 3,
    }


def gateway_mailbox_payload() -> dict[str, Any]:
    return {
        "mailbox_ref": "MBX-01",
        "display_label": "海湾销售主入口",
        "provider_kind": "fake",
        "business_mode": "primary",
        "business_purpose": "sales_follow_up",
        "default_team_ref": TEAM_ONE,
        "account_owner_user_ref": "owner@example.invalid",
        "inbound_enabled": True,
        "outbound_enabled": False,
        "status": "active",
        "config_revision": 3,
    }


def test_mailbox_reads_delegate_exact_actor_role_and_team_scope(
    v5_modules: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _gateway, admin, _inbox, fake = v5_modules
    fake._roles = {"Integration Admin"}
    calls: list[dict[str, Any]] = []

    def call_gateway(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"mailboxes": [gateway_mailbox_payload()]}

    monkeypatch.setattr(admin, "call_gateway", call_gateway)

    response = admin.list(cursor="CUR-01", page_size="20")

    assert response["data"]["mailboxes"] == [mailbox_payload()]
    assert calls == [
        {
            "method": "POST",
            "path": "/internal/v1/bff/mailboxes/list",
            "purpose": "email_mailbox_read",
            "payload": {
                "actor_ref": "sales@example.invalid",
                "actor_roles": ["Integration Admin"],
                "allowed_team_refs": [TEAM_ONE, TEAM_TWO],
                "cursor": "CUR-01",
                "page_size": 20,
            },
        }
    ]


def test_mailbox_admin_is_role_gated_and_writes_forward_revision_and_idempotency(
    v5_modules: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _gateway, admin, _inbox, fake = v5_modules
    fake._roles = {"Integration Admin"}
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        admin,
        "call_gateway",
        lambda **kwargs: calls.append(kwargs) or {"mailbox": gateway_mailbox_payload()},
    )

    result = admin.set_status(
        mailbox_ref="MBX-01",
        action="pause",
        expected_revision="3",
        idempotency_key="pause-mailbox-01",
    )

    assert result["data"]["mailbox"]["status"] == "active"
    assert calls[0]["path"] == "/internal/v1/bff/mailboxes/status"
    assert calls[0]["payload"] == {
        "actor_ref": "sales@example.invalid",
        "actor_roles": ["Integration Admin"],
        "allowed_team_refs": [TEAM_ONE, TEAM_TWO],
        "mailbox_ref": "MBX-01",
        "action": "pause",
        "expected_revision": 3,
        "idempotency_key": "pause-mailbox-01",
    }
    assert calls[0]["idempotency_key"] == "pause-mailbox-01"


def test_multiple_primary_mailboxes_are_preserved_without_local_uniqueness_filter(
    v5_modules: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _gateway, admin, _inbox, fake = v5_modules
    fake._roles = {"Integration Admin"}
    first = gateway_mailbox_payload()
    second = {
        **gateway_mailbox_payload(),
        "mailbox_ref": "MBX-02",
        "display_label": "中国销售主入口",
        "default_team_ref": TEAM_TWO,
    }
    monkeypatch.setattr(admin, "call_gateway", lambda **_kwargs: {"mailboxes": [first, second]})

    response = admin.list()

    assert [item["business_mode"] for item in response["data"]["mailboxes"]] == [
        "primary",
        "primary",
    ]


def test_mailbox_upsert_is_domain_complete_and_authority_checked(
    v5_modules: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _gateway, admin, _inbox, fake = v5_modules
    fake._roles = {"Integration Admin"}
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        admin,
        "call_gateway",
        lambda **kwargs: calls.append(kwargs) or {"mailbox": gateway_mailbox_payload()},
    )

    admin.upsert(
        display_label="海湾销售主入口",
        provider_kind="fake",
        business_mode="primary",
        business_purpose="sales_follow_up",
        provider_account_ref="provider-account-sales",
        observer_connector_instance_ref="OCI-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        default_team_ref=TEAM_ONE,
        account_owner_user_ref="owner@example.invalid",
        priority="10",
        credential_ref="secretref:v1/email-sales",
        inbound_enabled="false",
        outbound_enabled="false",
        expected_revision="0",
        idempotency_key="create-mailbox-01",
    )

    payload = calls[0]["payload"]
    assert payload["provider_account_ref"] == "provider-account-sales"
    assert payload["observer_connector_instance_ref"].startswith("OCI-")
    assert payload["default_team_ref"] == TEAM_ONE
    assert payload["account_owner_user_ref"] == "owner@example.invalid"
    assert payload["priority"] == 10
    assert payload["credential_ref"] == "secretref:v1/email-sales"
    assert payload["outbound_enabled"] is False
    assert (
        "GBOS Team",
        {
            "name": TEAM_ONE,
            "business_status": "Active",
            "review_status": "Approved",
        },
    ) in fake.exists_calls
    assert (
        "User",
        {
            "name": "owner@example.invalid",
            "enabled": 1,
            "user_type": "System User",
        },
    ) in fake.exists_calls


def test_ceo_and_gbos_admin_delegate_wildcard_but_teamless_sales_fails_closed(
    v5_modules: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _gateway, _admin, inbox, fake = v5_modules
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        inbox,
        "call_gateway",
        lambda **kwargs: calls.append(kwargs) or {"inbox_items": [], "next_cursor": None},
    )
    fake._roles = {"CEO"}
    inbox.list()
    assert calls[-1]["payload"]["allowed_team_refs"] == ["*"]

    fake._roles = {"Sales User"}
    fake._teams = []
    with pytest.raises(Exception, match="team scope"):
        inbox.list()


def test_inbox_is_read_only_safe_projection_and_delegates_scope(
    v5_modules: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _gateway, _admin, inbox, _fake = v5_modules
    safe = {
        "inbox_item_ref": "INB-01",
        "mailbox_label": "海湾销售主入口",
        "mailbox_role": "primary",
        "received_at": "2026-08-13T08:00:00Z",
        "state": "identity_pending",
        "safe_summary": "新的销售咨询",
        "team_ref": TEAM_ONE,
        "revision": 1,
    }
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        inbox,
        "call_gateway",
        lambda **kwargs: calls.append(kwargs) or {"inbox_items": [safe]},
    )

    response = inbox.list(state="identity_pending", page_size="25")

    assert response["data"]["inbox_items"] == [
        {
            **{key: value for key, value in safe.items() if key != "team_ref"},
            "team_label": "海湾销售组",
        }
    ]
    assert calls[0]["payload"]["actor_ref"] == "sales@example.invalid"
    assert calls[0]["payload"]["allowed_team_refs"] == [TEAM_ONE, TEAM_TWO]
    assert calls[0]["payload"]["page_size"] == 25
    assert not any(name in vars(inbox) for name in ("claim", "merge", "draft", "send"))


def test_standalone_integration_admin_cannot_read_business_inbox(
    v5_modules: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _gateway, _admin, inbox, fake = v5_modules
    fake._roles = {"Integration Admin"}
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(inbox, "call_gateway", lambda **kwargs: calls.append(kwargs) or {})

    with pytest.raises(Exception, match="Role is not permitted"):
        inbox.list()

    assert calls == []


def test_closed_dto_rejects_sensitive_or_extra_downstream_fields(
    v5_modules: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _gateway, _admin, inbox, _fake = v5_modules
    monkeypatch.setattr(
        inbox,
        "call_gateway",
        lambda **_kwargs: {
            "inbox_item": {
                "inbox_item_ref": "INB-01",
                "mailbox_label": "主入口",
                "mailbox_role": "primary",
                "received_at": "2026-08-13T08:00:00Z",
                "state": "unassigned",
                "safe_summary": "新的咨询",
                "team_ref": TEAM_ONE,
                "assignee_user_ref": None,
                "identity_state": "unknown",
                "revision": 1,
                "raw_body": "must never cross the BFF",
            }
        },
    )

    with pytest.raises(Exception, match="unexpected fields"):
        inbox.get("INB-01")


def test_connector_health_is_live_safe_read_for_admin_only(
    v5_modules: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _gateway, admin, _inbox, fake = v5_modules
    fake._roles = {"Integration Admin"}
    health = {
        "mailbox_ref": "MBX-01",
        "mailbox_label": "海湾销售主入口",
        "status": "healthy",
        "freshness": "fresh",
        "backlog": 0,
        "last_success_at": "2026-08-13T08:00:00Z",
        "safe_error_code": None,
    }
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        admin,
        "call_gateway",
        lambda **kwargs: calls.append(kwargs) or {"connector_health": [health]},
    )

    response = admin.get_connector_health()

    assert response["data"] == {"connector_health": [health]}
    assert calls[0]["path"] == "/internal/v1/bff/email-connectors/health"
    assert calls[0]["purpose"] == "email_connector_health_read"
