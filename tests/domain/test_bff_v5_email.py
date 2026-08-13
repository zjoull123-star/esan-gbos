from __future__ import annotations

import hashlib
import importlib
import json
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

TEAM_ONE = "TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV"
TEAM_TWO = "TEM-01ARZ3NDEKTSV4RRFFQ69G5FB0"
RAW_MAILBOX_ADDRESS = "mailbox-raw-sentinel@example.invalid"
OPAQUE_MAILBOX_ADDRESS = "extid:v1:email:" + "M" * 43
SLA_MAILBOX_REF = "MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV"
SLA_POLICY_REF = "SLA-01ARZ3NDEKTSV4RRFFQ69G5FAV"


def command_authority(command: str = "claim") -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "command": command,
        "actor_ref_digest": "sha256:" + "d" * 64,
        "actor_roles": ["Sales User"],
        "actor_team_refs": [TEAM_ONE, TEAM_TWO],
        "actor_eligibility_revision": "sha256:" + "a" * 64,
        "inbox_item_ref": "INB-01",
        "expected_inbox_revision": 1,
        "target_user_ref_digest": None,
        "target_team_refs": [],
        "target_eligibility_revision": None,
        "business_ref": None,
        "business_team_ref": None,
        "business_revision": None,
    }


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


@pytest.fixture
def email_send_module(
    v5_modules: tuple[Any, Any, Any, FakeFrappe], monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, FakeFrappe]:
    _gateway, _admin, _inbox, fake = v5_modules
    approval_module = ModuleType(
        "esan_gbos.gbos.doctype.gbos_email_send_approval.gbos_email_send_approval"
    )
    approval_module.approval_snapshot = lambda _doc: {}
    review_case_module = ModuleType("esan_gbos.gbos.doctype.gbos_review_case.gbos_review_case")
    review_case_module.build_case_payload = lambda _doc: {}
    review_case_module.build_subject_snapshot = lambda _doc: {}
    monkeypatch.setitem(sys.modules, approval_module.__name__, approval_module)
    monkeypatch.setitem(sys.modules, review_case_module.__name__, review_case_module)
    sys.modules.pop("esan_gbos.api.v5.email_send", None)
    module = importlib.import_module("esan_gbos.api.v5.email_send")
    return module, fake


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


def mailbox_upsert_kwargs(**overrides: Any) -> dict[str, Any]:
    return {
        "canonical_mailbox_address": RAW_MAILBOX_ADDRESS,
        "display_label": "海湾销售主入口",
        "provider_kind": "fake",
        "business_mode": "primary",
        "business_purpose": "sales_follow_up",
        "provider_account_ref": "provider-account-sales",
        "observer_connector_instance_ref": "OCI-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "default_team_ref": TEAM_ONE,
        "account_owner_user_ref": "owner@example.invalid",
        "priority": "10",
        "credential_ref": "secretref:v1/email-sales",
        "inbound_enabled": "false",
        "outbound_enabled": "false",
        "expected_revision": "0",
        "idempotency_key": "create-mailbox-01",
        **overrides,
    }


def sla_policy_payload(*, include_mailbox: bool = False, **overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "policy_ref": SLA_POLICY_REF,
        "revision": 4,
        "first_response_duration_seconds": 3600,
        "effective_at": "2026-08-14T09:30:00+08:00",
    }
    if include_mailbox:
        value["mailbox_ref"] = SLA_MAILBOX_REF
    return {**value, **overrides}


def sla_policy_upsert_kwargs(**overrides: Any) -> dict[str, Any]:
    return {
        "mailbox_ref": SLA_MAILBOX_REF,
        "first_response_duration_seconds": "3600",
        "effective_at": "2026-08-14T09:30:00+08:00",
        "expected_revision": "3",
        "idempotency_key": "sla-policy-upsert-01",
        **overrides,
    }


def test_sla_policy_list_is_closed_scoped_and_normalizes_utc(
    v5_modules: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _gateway, admin, _inbox, fake = v5_modules
    fake._roles = {"Integration Admin"}
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        admin,
        "call_gateway",
        lambda **kwargs: (
            calls.append(kwargs)
            or {
                "mailbox_ref": SLA_MAILBOX_REF,
                "sla_policies": [sla_policy_payload()],
                "next_cursor": "opaque-next-02",
            }
        ),
    )

    response = admin.list_sla_policies(
        mailbox_ref=SLA_MAILBOX_REF,
        cursor="opaque-current-01",
        page_size="100",
    )

    assert response["data"] == {
        "mailbox_ref": SLA_MAILBOX_REF,
        "sla_policies": [
            {
                "policy_ref": SLA_POLICY_REF,
                "revision": 4,
                "first_response_duration_seconds": 3600,
                "effective_at": "2026-08-14T01:30:00Z",
            }
        ],
        "next_cursor": "opaque-next-02",
    }
    assert calls == [
        {
            "method": "POST",
            "path": "/internal/v1/bff/email-admin/sla-policies/list",
            "purpose": "email_admin_read",
            "payload": {
                "actor_ref": "sales@example.invalid",
                "actor_roles": ["Integration Admin"],
                "allowed_team_refs": [TEAM_ONE, TEAM_TWO],
                "mailbox_ref": SLA_MAILBOX_REF,
                "cursor": "opaque-current-01",
                "page_size": 100,
            },
        }
    ]


def test_sla_policy_list_applies_transport_page_default_only(
    v5_modules: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _gateway, admin, _inbox, fake = v5_modules
    fake._roles = {"GBOS Admin"}
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        admin,
        "call_gateway",
        lambda **kwargs: (
            calls.append(kwargs)
            or {"mailbox_ref": SLA_MAILBOX_REF, "sla_policies": [], "next_cursor": None}
        ),
    )

    response = admin.list_sla_policies(mailbox_ref=SLA_MAILBOX_REF)

    assert response["data"] == {
        "mailbox_ref": SLA_MAILBOX_REF,
        "sla_policies": [],
        "next_cursor": None,
    }
    assert calls[0]["payload"]["page_size"] == 25
    assert "cursor" not in calls[0]["payload"]


def test_sla_policy_upsert_is_idempotent_closed_scoped_and_normalizes_utc(
    v5_modules: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _gateway, admin, _inbox, fake = v5_modules
    fake._roles = {"Integration Admin"}
    gateway_calls: list[dict[str, Any]] = []
    idempotency_calls: list[tuple[str, str, dict[str, Any], str]] = []

    monkeypatch.setattr(
        admin,
        "call_gateway",
        lambda **kwargs: (
            gateway_calls.append(kwargs) or {"sla_policy": sla_policy_payload(include_mailbox=True)}
        ),
    )

    def run_idempotent(
        operation: str,
        key: str,
        payload: dict[str, Any],
        execute: Any,
        *,
        api_version: str,
    ) -> tuple[dict[str, Any], bool, str]:
        idempotency_calls.append((operation, key, payload, api_version))
        return execute(), True, "REQ-original"

    monkeypatch.setattr(admin, "run_idempotent", run_idempotent)

    response = admin.upsert_sla_policy(**sla_policy_upsert_kwargs())

    expected_payload = {
        "actor_ref": "sales@example.invalid",
        "actor_roles": ["Integration Admin"],
        "allowed_team_refs": [TEAM_ONE, TEAM_TWO],
        "mailbox_ref": SLA_MAILBOX_REF,
        "first_response_duration_seconds": 3600,
        "effective_at": "2026-08-14T01:30:00Z",
        "expected_revision": 3,
        "idempotency_key": "sla-policy-upsert-01",
    }
    assert gateway_calls == [
        {
            "method": "POST",
            "path": "/internal/v1/bff/email-admin/sla-policies/upsert",
            "purpose": "email_admin_command",
            "payload": expected_payload,
            "idempotency_key": "sla-policy-upsert-01",
        }
    ]
    assert idempotency_calls == [
        (
            "email_admin.upsert_sla_policy",
            "sla-policy-upsert-01",
            expected_payload,
            "v5",
        )
    ]
    assert response["data"] == {
        "sla_policy": {
            "mailbox_ref": SLA_MAILBOX_REF,
            "policy_ref": SLA_POLICY_REF,
            "revision": 4,
            "first_response_duration_seconds": 3600,
            "effective_at": "2026-08-14T01:30:00Z",
        }
    }
    assert response["meta"]["replayed"] is True
    assert response["meta"]["original_request_id"] == "REQ-original"


def test_sla_policy_utc_normalization_preserves_rfc3339_fraction(
    v5_modules: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _gateway, admin, _inbox, fake = v5_modules
    fake._roles = {"Integration Admin"}
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        admin,
        "call_gateway",
        lambda **kwargs: (
            calls.append(kwargs)
            or {
                "sla_policy": sla_policy_payload(
                    include_mailbox=True,
                    effective_at="2026-08-14T09:30:00.123456789+08:00",
                )
            }
        ),
    )

    response = admin.upsert_sla_policy(
        **sla_policy_upsert_kwargs(effective_at="2026-08-14T09:30:00.123456789+08:00")
    )

    assert calls[0]["payload"]["effective_at"] == "2026-08-14T01:30:00.123456789Z"
    assert response["data"]["sla_policy"]["effective_at"] == ("2026-08-14T01:30:00.123456789Z")


@pytest.mark.parametrize("operation", ["list", "upsert"])
def test_sla_policy_role_denial_happens_before_gateway(
    v5_modules: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    _gateway, admin, _inbox, fake = v5_modules
    fake._roles = {"Sales Manager"}
    gateway_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(admin, "call_gateway", lambda **kwargs: gateway_calls.append(kwargs))

    with pytest.raises(admin.BFFError) as raised:
        if operation == "list":
            admin.list_sla_policies(mailbox_ref=SLA_MAILBOX_REF)
        else:
            admin.upsert_sla_policy(**sla_policy_upsert_kwargs())

    assert raised.value.code == "permission_denied"
    assert raised.value.status == 403
    assert gateway_calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mailbox_ref", "MBX-01"),
        ("mailbox_ref", SLA_MAILBOX_REF + "\n"),
        ("cursor", ""),
        ("cursor", "cursor\nvalue"),
        ("cursor", "x" * 513),
        ("page_size", 0),
        ("page_size", 101),
        ("page_size", True),
        ("page_size", 1.5),
        ("page_size", "1.5"),
        ("page_size", "9" * 5000),
    ],
)
def test_sla_policy_list_rejects_malformed_inputs_before_gateway(
    v5_modules: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    _gateway, admin, _inbox, fake = v5_modules
    fake._roles = {"Integration Admin"}
    gateway_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(admin, "call_gateway", lambda **kwargs: gateway_calls.append(kwargs))
    values: dict[str, object] = {"mailbox_ref": SLA_MAILBOX_REF}
    values[field] = value

    with pytest.raises(admin.BFFError):
        admin.list_sla_policies(**values)

    assert gateway_calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mailbox_ref", "MBX-invalid"),
        ("first_response_duration_seconds", 59),
        ("first_response_duration_seconds", 604801),
        ("first_response_duration_seconds", True),
        ("first_response_duration_seconds", 60.0),
        ("first_response_duration_seconds", "60.0"),
        ("effective_at", "2026-08-14T09:30:00"),
        ("effective_at", "not-a-timestamp"),
        ("effective_at", "2026-08-14 09:30:00+08:00"),
        ("effective_at", "2026-08-14T09:30:00+0800"),
        ("effective_at", "2026-08-14T09:30:00-00:00"),
        ("effective_at", "2026-08-14T09:30:00.1234567890+08:00"),
        ("effective_at", "x" * 36),
        ("expected_revision", -1),
        ("expected_revision", True),
        ("idempotency_key", "short"),
        ("idempotency_key", "key\rvalue"),
        ("idempotency_key", "x" * 257),
    ],
)
def test_sla_policy_upsert_rejects_malformed_inputs_before_gateway(
    v5_modules: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    _gateway, admin, _inbox, fake = v5_modules
    fake._roles = {"Integration Admin"}
    gateway_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(admin, "call_gateway", lambda **kwargs: gateway_calls.append(kwargs))

    with pytest.raises(admin.BFFError):
        admin.upsert_sla_policy(**sla_policy_upsert_kwargs(**{field: value}))

    assert gateway_calls == []


def test_sla_policy_upsert_requires_duration_and_rejects_public_authority_fields(
    v5_modules: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _gateway, admin, _inbox, fake = v5_modules
    fake._roles = {"Integration Admin"}
    gateway_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(admin, "call_gateway", lambda **kwargs: gateway_calls.append(kwargs))
    missing_duration = sla_policy_upsert_kwargs()
    missing_duration.pop("first_response_duration_seconds")

    with pytest.raises(TypeError):
        admin.upsert_sla_policy(**missing_duration)
    for forbidden in ("policy_ref", "site_id", "team_ref", "purpose", "actor_ref"):
        with pytest.raises(TypeError):
            admin.upsert_sla_policy(**sla_policy_upsert_kwargs(**{forbidden: "forbidden"}))

    assert gateway_calls == []


@pytest.mark.parametrize(
    "response",
    [
        {
            "mailbox_ref": SLA_MAILBOX_REF,
            "sla_policies": [],
            "next_cursor": None,
            "extra": "drift",
        },
        {"mailbox_ref": "MBX-01ARZ3NDEKTSV4RRFFQ69G5FAW", "sla_policies": [], "next_cursor": None},
        {
            "mailbox_ref": SLA_MAILBOX_REF,
            "sla_policies": [sla_policy_payload(extra="drift")],
            "next_cursor": None,
        },
        {
            "mailbox_ref": SLA_MAILBOX_REF,
            "sla_policies": [sla_policy_payload(policy_ref=" " + SLA_POLICY_REF)],
            "next_cursor": None,
        },
        {
            "mailbox_ref": SLA_MAILBOX_REF,
            "sla_policies": [sla_policy_payload(effective_at="2026-08-14T01:30:00")],
            "next_cursor": None,
        },
        {
            "mailbox_ref": SLA_MAILBOX_REF,
            "sla_policies": [sla_policy_payload(first_response_duration_seconds=True)],
            "next_cursor": None,
        },
        {"mailbox_ref": SLA_MAILBOX_REF, "sla_policies": [], "next_cursor": "x" * 513},
        {
            "mailbox_ref": SLA_MAILBOX_REF,
            "sla_policies": [sla_policy_payload() for _index in range(101)],
            "next_cursor": None,
        },
    ],
)
def test_sla_policy_list_rejects_closed_response_drift(
    v5_modules: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
    response: dict[str, Any],
) -> None:
    _gateway, admin, _inbox, fake = v5_modules
    fake._roles = {"Integration Admin"}
    monkeypatch.setattr(admin, "call_gateway", lambda **_kwargs: response)

    with pytest.raises(admin.BFFError) as raised:
        admin.list_sla_policies(mailbox_ref=SLA_MAILBOX_REF)

    assert raised.value.status == 503


@pytest.mark.parametrize(
    "response",
    [
        {"sla_policy": sla_policy_payload(include_mailbox=True), "extra": "drift"},
        {"sla_policy": sla_policy_payload(include_mailbox=True, extra="drift")},
        {
            "sla_policy": sla_policy_payload(
                include_mailbox=True,
                mailbox_ref="MBX-01ARZ3NDEKTSV4RRFFQ69G5FAW",
            )
        },
        {"sla_policy": sla_policy_payload(include_mailbox=True, policy_ref="SLA-invalid")},
        {"sla_policy": sla_policy_payload(include_mailbox=True, revision=0)},
    ],
)
def test_sla_policy_upsert_rejects_closed_response_drift(
    v5_modules: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
    response: dict[str, Any],
) -> None:
    _gateway, admin, _inbox, fake = v5_modules
    fake._roles = {"Integration Admin"}
    monkeypatch.setattr(admin, "call_gateway", lambda **_kwargs: response)

    with pytest.raises(admin.BFFError) as raised:
        admin.upsert_sla_policy(**sla_policy_upsert_kwargs())

    assert raised.value.status == 503


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

    response = admin.list_mailboxes(cursor="CUR-01", page_size="20")

    assert response["data"]["mailboxes"] == [mailbox_payload()]
    assert calls == [
        {
            "method": "POST",
            "path": "/internal/v1/bff/email-admin/mailboxes/list",
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

    result = admin.set_mailbox_status(
        mailbox_ref="MBX-01",
        action="pause",
        expected_revision="3",
        idempotency_key="pause-mailbox-01",
    )

    assert result["data"]["mailbox"]["status"] == "active"
    assert calls[0]["path"] == "/internal/v1/bff/email-admin/mailboxes/status"
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

    response = admin.list_mailboxes()

    assert [item["business_mode"] for item in response["data"]["mailboxes"]] == [
        "primary",
        "primary",
    ]


def test_mailbox_upsert_is_domain_complete_and_authority_checked(
    v5_modules: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _gateway, admin, _inbox, fake = v5_modules
    fake._roles = {"Integration Admin"}
    events: list[tuple[str, dict[str, Any]]] = []
    idempotency_payloads: list[dict[str, Any]] = []

    def observer_call(**kwargs: Any) -> dict[str, str]:
        assert fake.exists_calls
        events.append(("observer", kwargs))
        return {
            "opaque_address_ref": OPAQUE_MAILBOX_ADDRESS,
            "normalization_version": "email-v1",
        }

    def gateway_call(**kwargs: Any) -> dict[str, Any]:
        events.append(("gateway", kwargs))
        return {"mailbox": gateway_mailbox_payload()}

    def run_idempotent(
        _operation: str,
        _key: str,
        payload: dict[str, Any],
        execute: Any,
        **_kwargs: Any,
    ) -> tuple[dict[str, Any], bool, None]:
        idempotency_payloads.append(payload)
        return execute(), False, None

    monkeypatch.setattr(admin, "call_observer", observer_call)
    monkeypatch.setattr(admin, "call_gateway", gateway_call)
    monkeypatch.setattr(admin, "run_idempotent", run_idempotent)

    response = admin.upsert_mailbox(**mailbox_upsert_kwargs())

    assert [event[0] for event in events] == ["observer", "gateway"]
    assert events[0][1] == {
        "path": "/internal/v1/bff/email-mailbox-identity/derive",
        "purpose": "email_mailbox_identity",
        "payload": {
            "canonical_mailbox_address": RAW_MAILBOX_ADDRESS,
            "idempotency_key": "create-mailbox-01",
        },
        "idempotency_key": "create-mailbox-01",
    }
    payload = events[1][1]["payload"]
    assert payload["provider_account_ref"] == "provider-account-sales"
    assert payload["observer_connector_instance_ref"].startswith("OCI-")
    assert payload["default_team_ref"] == TEAM_ONE
    assert payload["account_owner_user_ref"] == "owner@example.invalid"
    assert payload["priority"] == 10
    assert payload["credential_ref"] == "secretref:v1/email-sales"
    assert payload["outbound_enabled"] is False
    assert payload["mailbox_address_identity_ref"] == OPAQUE_MAILBOX_ADDRESS
    assert "canonical_mailbox_address" not in payload
    assert idempotency_payloads == [payload]
    assert RAW_MAILBOX_ADDRESS not in repr(idempotency_payloads)
    assert RAW_MAILBOX_ADDRESS not in repr(response)
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


def test_mailbox_command_validator_requires_only_the_opaque_address_identity() -> None:
    from esan_gbos.domain.v5_email_dto import (
        V5EmailDTOValidationError,
        validate_mailbox_upsert,
    )

    command = mailbox_upsert_kwargs()
    command.pop("canonical_mailbox_address")
    command["mailbox_address_identity_ref"] = OPAQUE_MAILBOX_ADDRESS
    command["priority"] = 10
    command["inbound_enabled"] = False
    command["outbound_enabled"] = False
    command["expected_revision"] = 0

    assert validate_mailbox_upsert(command)["mailbox_address_identity_ref"] == (
        OPAQUE_MAILBOX_ADDRESS
    )
    with pytest.raises(V5EmailDTOValidationError, match="mailbox_address_identity_ref"):
        validate_mailbox_upsert(
            {key: value for key, value in command.items() if key != "mailbox_address_identity_ref"}
        )
    with pytest.raises(V5EmailDTOValidationError, match="canonical_mailbox_address"):
        validate_mailbox_upsert({**command, "canonical_mailbox_address": RAW_MAILBOX_ADDRESS})


def test_mailbox_observer_failure_blocks_gateway_and_does_not_echo_raw_address(
    v5_modules: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _gateway, admin, _inbox, fake = v5_modules
    fake._roles = {"Integration Admin"}
    gateway_calls: list[dict[str, Any]] = []

    def fail_observer(**_kwargs: Any) -> dict[str, Any]:
        raise admin.BFFError(
            "internal_error",
            "Observer email material service is unavailable",
            status=503,
        )

    monkeypatch.setattr(admin, "call_observer", fail_observer)
    monkeypatch.setattr(admin, "call_gateway", lambda **kwargs: gateway_calls.append(kwargs))

    with pytest.raises(admin.BFFError) as raised:
        admin.upsert_mailbox(**mailbox_upsert_kwargs())

    assert gateway_calls == []
    assert RAW_MAILBOX_ADDRESS not in repr(raised.value)
    assert RAW_MAILBOX_ADDRESS not in caplog.text


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("canonical_mailbox_address", "x" * 255),
        ("canonical_mailbox_address", "mailbox\n@example.invalid"),
        ("canonical_mailbox_address", 123),
        ("default_team_ref", "TEM-invalid"),
        ("default_team_ref", TEAM_ONE + "\n"),
        ("account_owner_user_ref", "owner\x7f@example.invalid"),
        ("account_owner_user_ref", "x" * 141),
        ("idempotency_key", "key\rvalue"),
        ("idempotency_key", "x" * 257),
    ],
)
def test_mailbox_upsert_rejects_unsafe_transient_inputs_before_authority_or_services(
    v5_modules: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    _gateway, admin, _inbox, fake = v5_modules
    fake._roles = {"Integration Admin"}
    observer_calls: list[dict[str, Any]] = []
    gateway_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(admin, "call_observer", lambda **kwargs: observer_calls.append(kwargs))
    monkeypatch.setattr(admin, "call_gateway", lambda **kwargs: gateway_calls.append(kwargs))

    with pytest.raises(admin.BFFError) as raised:
        admin.upsert_mailbox(**mailbox_upsert_kwargs(**{field: value}))

    assert fake.exists_calls == []
    assert observer_calls == []
    assert gateway_calls == []
    assert repr(value) not in repr(raised.value)


@pytest.mark.parametrize(
    "observer_response",
    [
        {"opaque_address_ref": OPAQUE_MAILBOX_ADDRESS},
        {
            "opaque_address_ref": OPAQUE_MAILBOX_ADDRESS,
            "normalization_version": "email-v1",
            "extra": "invented",
        },
        {
            "opaque_address_ref": "extid:v1:email:" + "A" * 42,
            "normalization_version": "email-v1",
        },
        {
            "opaque_address_ref": OPAQUE_MAILBOX_ADDRESS,
            "normalization_version": "email-address-v1",
        },
    ],
)
def test_mailbox_upsert_rejects_untrusted_observer_identity_responses_without_gateway(
    v5_modules: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
    observer_response: dict[str, str],
) -> None:
    _gateway, admin, _inbox, fake = v5_modules
    fake._roles = {"Integration Admin"}
    gateway_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(admin, "call_observer", lambda **_kwargs: observer_response)
    monkeypatch.setattr(admin, "call_gateway", lambda **kwargs: gateway_calls.append(kwargs))

    with pytest.raises(admin.BFFError) as raised:
        admin.upsert_mailbox(**mailbox_upsert_kwargs())

    assert gateway_calls == []
    assert raised.value.status == 503
    assert RAW_MAILBOX_ADDRESS not in repr(raised.value)


def test_mailbox_idempotency_replays_same_derived_identity_and_conflicts_on_drift(
    v5_modules: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _gateway, admin, _inbox, fake = v5_modules
    fake._roles = {"Integration Admin"}
    derived_refs = iter(
        [OPAQUE_MAILBOX_ADDRESS, OPAQUE_MAILBOX_ADDRESS, "extid:v1:email:" + "N" * 43]
    )
    gateway_calls: list[dict[str, Any]] = []
    stored: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}

    monkeypatch.setattr(
        admin,
        "call_observer",
        lambda **_kwargs: {
            "opaque_address_ref": next(derived_refs),
            "normalization_version": "email-v1",
        },
    )
    monkeypatch.setattr(
        admin,
        "call_gateway",
        lambda **kwargs: gateway_calls.append(kwargs) or {"mailbox": gateway_mailbox_payload()},
    )

    def run_idempotent(
        _operation: str,
        key: str,
        payload: dict[str, Any],
        execute: Any,
        **_kwargs: Any,
    ) -> tuple[dict[str, Any], bool, str | None]:
        assert RAW_MAILBOX_ADDRESS not in repr(payload)
        previous = stored.get(key)
        if previous is not None:
            previous_payload, result = previous
            if previous_payload != payload:
                raise admin.BFFError(
                    "idempotency_conflict",
                    "Mailbox command idempotency conflict",
                    status=409,
                )
            return result, True, "REQ-original"
        result = execute()
        stored[key] = (payload, result)
        return result, False, None

    monkeypatch.setattr(admin, "run_idempotent", run_idempotent)

    first = admin.upsert_mailbox(**mailbox_upsert_kwargs())
    replay = admin.upsert_mailbox(**mailbox_upsert_kwargs())
    with pytest.raises(admin.BFFError) as raised:
        admin.upsert_mailbox(**mailbox_upsert_kwargs())

    assert len(gateway_calls) == 1
    assert first["meta"]["replayed"] is False
    assert replay["meta"]["replayed"] is True
    assert raised.value.code == "idempotency_conflict"
    assert RAW_MAILBOX_ADDRESS not in repr(first) + repr(replay) + repr(raised.value)


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
    assert {
        "list",
        "get",
        "claim",
        "reassign",
        "transition",
        "merge",
        "split",
        "link_business",
        "save_draft",
        "reveal",
    } <= set(vars(inbox))
    assert "send" not in vars(inbox)


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

    response = admin.connector_health()

    assert response["data"] == {"connector_health": [health]}
    assert calls[0]["path"] == "/internal/v1/bff/email-admin/connector-health/get"
    assert calls[0]["purpose"] == "email_connector_health_read"


def test_inbox_claim_delegates_exact_current_actor_scope_revision_and_idempotency(
    v5_modules: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _gateway, _admin, inbox, _fake = v5_modules
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        inbox,
        "derive_inbox_command_authority",
        lambda **_kwargs: command_authority(),
    )
    monkeypatch.setattr(
        inbox,
        "call_gateway",
        lambda **kwargs: (
            calls.append(kwargs)
            or {
                "inbox_item": {
                    "inbox_item_ref": "INB-01",
                    "state": "assigned",
                    "revision": 2,
                }
            }
        ),
    )

    result = inbox.claim("INB-01", expected_revision="1", idempotency_key="claim-0001")

    assert result["data"]["inbox_item"]["revision"] == 2
    assert calls == [
        {
            "method": "POST",
            "path": "/internal/v1/bff/email-inbox/claim",
            "purpose": "email_inbox_command",
            "payload": {
                "actor_ref": "sales@example.invalid",
                "actor_roles": ["Sales User"],
                "allowed_team_refs": [TEAM_ONE, TEAM_TWO],
                "inbox_item_ref": "INB-01",
                "expected_revision": 1,
                "idempotency_key": "claim-0001",
                "authority_receipt": command_authority(),
            },
            "idempotency_key": "claim-0001",
        }
    ]


def test_reassign_derives_closed_authority_server_side_and_forwards_no_boolean(
    v5_modules: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _gateway, _admin, inbox, _fake = v5_modules
    calls: list[dict[str, Any]] = []
    receipt = {
        "schema_version": "1.0",
        "command": "reassign",
        "actor_ref_digest": "sha256:" + "d" * 64,
        "actor_roles": ["Sales Manager"],
        "actor_team_refs": [TEAM_ONE],
        "actor_eligibility_revision": "sha256:" + "a" * 64,
        "inbox_item_ref": "INB-01",
        "expected_inbox_revision": 1,
        "target_user_ref_digest": "sha256:" + "e" * 64,
        "target_team_refs": [TEAM_ONE],
        "target_eligibility_revision": "sha256:" + "b" * 64,
        "business_ref": None,
        "business_team_ref": None,
        "business_revision": None,
    }
    monkeypatch.setattr(inbox, "derive_inbox_command_authority", lambda **_kwargs: receipt)
    monkeypatch.setattr(
        inbox,
        "call_gateway",
        lambda **kwargs: (
            calls.append(kwargs)
            or {"inbox_item": {"inbox_item_ref": "INB-01", "state": "assigned", "revision": 2}}
        ),
    )

    inbox.reassign(
        "INB-01",
        expected_revision="1",
        idempotency_key="reassign-0001",
        assignee_user_ref="other@example.invalid",
    )

    payload = calls[0]["payload"]
    assert payload["authority_receipt"] == receipt
    assert "assignee_enabled" not in payload
    assert "assignee_team_ref" not in payload


def test_inbox_authority_conflict_returns_bounded_409(
    v5_modules: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _gateway, _admin, inbox, _fake = v5_modules
    authority = importlib.import_module("esan_gbos.api.internal.email_gateway_authority")
    monkeypatch.setattr(
        authority,
        "derive_inbox_command_authority",
        lambda **_kwargs: (_ for _ in ()).throw(
            authority.InboxCommandAuthorityConflict("authority_conflict")
        ),
    )

    with pytest.raises(inbox.BFFError) as caught:
        inbox.claim("INB-01", expected_revision="1", idempotency_key="claim-conflict")
    assert caught.value.status == 409
    assert caught.value.code == "authority_conflict"


def test_inbox_command_projection_replaces_raw_actor_and_team_refs_with_labels(
    v5_modules: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _gateway, _admin, inbox, _fake = v5_modules
    monkeypatch.setattr(
        inbox,
        "derive_inbox_command_authority",
        lambda **_kwargs: command_authority(),
    )
    monkeypatch.setattr(
        inbox,
        "call_gateway",
        lambda **_kwargs: {
            "inbox_item": {
                "inbox_item_ref": "INB-01",
                "state": "assigned",
                "team_ref": TEAM_ONE,
                "assignee_user_ref": "sales@example.invalid",
                "conversation_ref": "CNV-01",
                "business_links": ["CRM-LEAD-01"],
                "revision": 2,
            }
        },
    )

    result = inbox.claim("INB-01", expected_revision="1", idempotency_key="claim-0001")

    projection = result["data"]["inbox_item"]
    assert projection["team_label"] == "海湾销售组"
    assert projection["assignee_label"] == "销售员"
    assert "team_ref" not in projection
    assert "assignee_user_ref" not in projection


def test_save_draft_gets_fresh_gateway_receipt_then_observer_cas_then_commits_projection(
    v5_modules: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _gateway, _admin, inbox, _fake = v5_modules
    gateway_calls: list[dict[str, Any]] = []
    observer_calls: list[dict[str, Any]] = []
    provisional_draft_ref = "DRF-ui-provisional-01"
    canonical_draft_ref = "DRF-01ARZ3NDEKTSV4RRFFQ69G5FAV"
    receipt = {
        "receipt_ref": "DAR-01",
        "site_id": "gbos.localhost",
        "purpose": "email_draft_material",
        "inbox_item_ref": "INB-01",
        "draft_ref": canonical_draft_ref,
        "draft_revision": 1,
        "actor_ref": "sales@example.invalid",
        "team_ref": TEAM_ONE,
        "request_digest": "sha256:" + hashlib.sha256(b"Hello customer").hexdigest(),
        "issued_at": "2026-08-13T08:00:00Z",
        "expires_at": "2026-08-13T08:05:00Z",
    }

    def gateway_call(**kwargs: Any) -> dict[str, Any]:
        gateway_calls.append(kwargs)
        if kwargs["payload"]["phase"] == "authorize":
            return {"draft_authorization": receipt}
        return {"draft": {"draft_ref": canonical_draft_ref, "revision": 1, "state": "editable"}}

    monkeypatch.setattr(inbox, "call_gateway", gateway_call)
    monkeypatch.setattr(
        inbox,
        "call_observer",
        lambda **kwargs: (
            observer_calls.append(kwargs)
            or {
                "evidence_ref": "EVR-DRAFT-01",
                "digest": receipt["request_digest"],
                "revision": 1,
            }
        ),
    )

    result = inbox.save_draft(
        "INB-01",
        draft_ref=provisional_draft_ref,
        expected_revision="0",
        content="Hello customer",
        idempotency_key="draft-save-01",
    )

    assert result["data"]["draft"] == {
        "draft_ref": canonical_draft_ref,
        "revision": 1,
        "state": "editable",
    }
    assert [call["payload"]["phase"] for call in gateway_calls] == ["authorize", "commit"]
    assert observer_calls[0]["path"] == "/internal/v1/bff/email-draft-material/save"
    assert observer_calls[0]["payload"]["content"] == "Hello customer"
    assert observer_calls[0]["payload"]["authorization"]["draft_ref"] == canonical_draft_ref
    assert observer_calls[0]["payload"]["content_digest"] == receipt["request_digest"]
    assert observer_calls[0]["payload"]["idempotency_key"] == "draft-save-01"
    assert gateway_calls[1]["payload"]["evidence_ref"] == "EVR-DRAFT-01"
    assert gateway_calls[0]["payload"]["draft_ref"] == provisional_draft_ref
    assert gateway_calls[1]["payload"]["draft_ref"] == provisional_draft_ref
    roles = {"sender": "mailbox_owner", "recipients": ["original_sender"]}
    roles_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(roles, sort_keys=True, separators=(",", ":")).encode("ascii")
        ).hexdigest()
    )
    assert gateway_calls[0]["payload"]["participant_roles_digest"] == roles_digest
    assert gateway_calls[1]["payload"]["participant_roles_digest"] == roles_digest
    assert "content" not in gateway_calls[1]["payload"]


def test_email_send_submission_derives_protected_snapshot_only_from_server_authorities(
    email_send_module: tuple[Any, FakeFrappe], monkeypatch: pytest.MonkeyPatch
) -> None:
    email_send, _fake = email_send_module
    actor = "sales@example.invalid"
    refs = {
        "team": TEAM_ONE,
        "mailbox": "MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "inbox": "INB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "conversation": "CNV-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "draft": "DRF-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "party": "PTY-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "mapping": "EID-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "evidence": "EVR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "client": "CLI-01ARZ3NDEKTSV4RRFFQ69G5FAV",
    }
    gateway_snapshot = {
        "site_id": "gbos.localhost",
        "processing_purpose": "sales_follow_up",
        "team_ref": refs["team"],
        "assignee_user_name": actor,
        "mailbox_ref": refs["mailbox"],
        "mailbox_config_revision": 3,
        "inbox_item_ref": refs["inbox"],
        "inbox_item_revision": 4,
        "conversation_ref": refs["conversation"],
        "conversation_revision": 2,
        "party_ref": refs["party"],
        "owner_user_name": actor,
        "reply_draft_ref": refs["draft"],
        "reply_draft_revision": 3,
        "reply_draft_digest": "sha256:" + "4" * 64,
    }
    sender = "extid:v1:email:" + "a" * 43
    recipient = "extid:v1:email:" + "b" * 43
    gateway_calls: list[dict[str, Any]] = []
    observer_calls: list[dict[str, Any]] = []
    authorize_extra: dict[str, Any] = {}

    def gateway_call(**kwargs: Any) -> dict[str, Any]:
        gateway_calls.append(kwargs)
        if kwargs["payload"]["phase"] == "authorize":
            return {
                "send_authority": {
                    "gateway_snapshot": gateway_snapshot,
                    "draft_authorization": {"receipt_ref": "DAR-opaque"},
                    "draft_evidence_ref": "obs:v1:opaque-draft",
                    **authorize_extra,
                }
            }
        return {
            "send_authority": {
                "gateway_snapshot": gateway_snapshot,
                "participants": [
                    {"address_role": "sender", "opaque_address_ref": sender},
                    {
                        "address_role": "to",
                        "opaque_address_ref": recipient,
                        "identity_mapping_ref": refs["mapping"],
                        "identity_mapping_revision": 7,
                    },
                ],
            }
        }

    def observer_call(**kwargs: Any) -> dict[str, Any]:
        observer_calls.append(kwargs)
        return {
            "evidence_ref": refs["evidence"],
            "digest": "sha256:" + "9" * 64,
            "role_binding": email_send.EMAIL_SEND_PARTICIPANT_ROLES_DIGEST,
            "participants": [
                {"address_role": "sender", "opaque_address_ref": sender},
                {"address_role": "to", "opaque_address_ref": recipient},
            ],
        }

    monkeypatch.setattr(email_send, "call_gateway", gateway_call)
    monkeypatch.setattr(email_send, "call_observer", observer_call)
    monkeypatch.setattr(email_send, "make_gbos_name", lambda _prefix: refs["client"])
    monkeypatch.setattr(
        email_send,
        "_current_frappe_party_authority",
        lambda **_kwargs: {
            "party_revision": 5,
            "team_revision": 6,
            "owner_eligibility_revision": "sha256:" + "8" * 64,
        },
    )

    result = email_send._derive_submission_snapshot(
        {
            "inbox_item_ref": refs["inbox"],
            "draft_ref": refs["draft"],
            "expected_revision": 4,
            "expected_draft_revision": 3,
            "idempotency_key": "submit-email-review-01",
        },
        actor=actor,
        issued_at=email_send.datetime(2026, 8, 13, 10, tzinfo=email_send.UTC),
    )

    assert result["assignee_user_ref"] == result["owner_user_ref"]
    assert actor not in repr(result)
    assert result["participants"][1]["identity_mapping_revision"] == 7
    assert result["final_mime_evidence_ref"] == refs["evidence"]
    assert observer_calls[0]["payload"]["participant_roles"] == {
        "sender": "mailbox_owner",
        "recipients": ["original_sender"],
    }
    assert gateway_calls[0]["payload"]["expected_inbox_revision"] == 4
    assert gateway_calls[1]["payload"]["participant_projection"][1] == {
        "address_role": "to",
        "opaque_address_ref": recipient,
    }
    serialized_calls = repr(gateway_calls) + repr(observer_calls)
    assert "@esan" not in serialized_calls
    assert "body" not in serialized_calls

    observer_count = len(observer_calls)
    revalidated = email_send._derive_approval_live_snapshot(
        result,
        actor=actor,
        issued_at=email_send.datetime(2026, 8, 13, 10, 1, tzinfo=email_send.UTC),
    )
    assert revalidated == result
    assert len(observer_calls) == observer_count

    authorize_extra["unexpected"] = True
    with pytest.raises(email_send.BFFError, match="authority response"):
        email_send._derive_submission_snapshot(
            {
                "inbox_item_ref": refs["inbox"],
                "draft_ref": refs["draft"],
                "expected_revision": 4,
                "expected_draft_revision": 3,
                "idempotency_key": "submit-email-review-02",
            },
            actor=actor,
            issued_at=email_send.datetime(2026, 8, 13, 10, tzinfo=email_send.UTC),
        )
    assert len(observer_calls) == observer_count


def test_email_send_rechecks_current_party_team_owner_and_membership_authority(
    email_send_module: tuple[Any, FakeFrappe], monkeypatch: pytest.MonkeyPatch
) -> None:
    email_send, fake = email_send_module
    actor = "sales@example.invalid"
    row = {
        "mapping_ref": "EID-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "mapping_revision": 7,
        "team_ref": TEAM_ONE,
        "identity_type": "Party",
        "party_ref": "PTY-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "review_status": "Approved",
        "business_status": "Active",
        "party_revision": 5,
        "party_team_ref": TEAM_ONE,
        "party_status": "Active",
        "party_review_status": "Approved",
        "owner_user_ref": actor,
        "team_revision": 6,
        "team_status": "Active",
        "team_review_status": "Approved",
        "owner_enabled": 1,
        "owner_user_type": "System User",
        "membership_ref": "TMM-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "membership_parent": TEAM_ONE,
        "membership_user": actor,
        "membership_enabled": 1,
        "membership_modified": "2026-08-13T09:00:00Z",
    }
    queries: list[tuple[str, dict[str, Any]]] = []

    def current_authority(
        query: str, parameters: dict[str, Any], **_kwargs: Any
    ) -> list[dict[str, Any]]:
        queries.append((query, parameters))
        return [dict(row)]

    monkeypatch.setattr(fake.db, "sql", current_authority, raising=False)

    current = email_send._current_frappe_party_authority(
        mapping_ref=row["mapping_ref"],
        expected_mapping_revision=7,
        expected_team_ref=TEAM_ONE,
        expected_party_ref=row["party_ref"],
        actor=actor,
    )

    assert current["party_revision"] == 5
    assert current["team_revision"] == 6
    assert current["owner_eligibility_revision"].startswith("sha256:")
    assert "limit 3 for update" in " ".join(queries[0][0].lower().split())
    assert queries[0][1] == {"mapping_ref": row["mapping_ref"]}

    row["membership_enabled"] = 0
    with pytest.raises(email_send.BFFError, match="authority changed"):
        email_send._current_frappe_party_authority(
            mapping_ref=row["mapping_ref"],
            expected_mapping_revision=7,
            expected_team_ref=TEAM_ONE,
            expected_party_ref=row["party_ref"],
            actor=actor,
        )


def test_email_send_scalar_boundary_rejects_controls_and_overflow(
    email_send_module: tuple[Any, FakeFrappe],
) -> None:
    email_send, _fake = email_send_module

    with pytest.raises(email_send.BFFError, match="reference is invalid"):
        email_send._bounded_ref("opaque\x00reference", "reference")
    with pytest.raises(email_send.BFFError, match="positive integer"):
        email_send._positive_integer(2_147_483_648, "revision")


def test_reveal_delegates_once_to_gateway_which_owns_the_second_observer_call(
    v5_modules: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _gateway, _admin, inbox, fake = v5_modules
    fake._roles = {"Reviewer"}
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        inbox,
        "call_gateway",
        lambda **kwargs: (
            calls.append(("gateway", kwargs))
            or {
                "revealed": {
                    "content": "restricted body",
                    "media_type": "text/plain; charset=utf-8",
                }
            }
        ),
    )

    result = inbox.reveal("INB-01", "EVR-01")

    assert result["data"]["content"] == "restricted body"
    assert [name for name, _call in calls] == ["gateway"]
    assert calls[0][1]["path"] == "/internal/v1/bff/email-inbox/reveal"
    assert "?" not in calls[0][1]["path"]
    assert calls[0][1]["purpose"] == "email_evidence_reveal"
