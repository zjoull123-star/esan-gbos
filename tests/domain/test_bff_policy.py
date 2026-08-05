from __future__ import annotations

from esan_gbos.domain.access_policy import (
    PARTY_360_ROLES,
    SAMPLE_READ_ROLES,
    SOURCING_READ_ROLES,
)
from esan_gbos.domain.errors import (
    ERROR_MESSAGES_ZH,
    build_error_envelope,
    build_success_envelope,
)


def test_procurement_roles_cannot_read_full_party_360() -> None:
    assert "Purchase Manager" not in PARTY_360_ROLES
    assert "Buyer" not in PARTY_360_ROLES


def test_product_role_uses_linked_sample_context_not_full_party_360() -> None:
    assert "Product/R&D" not in PARTY_360_ROLES


def test_auditor_cannot_read_full_party_360() -> None:
    assert "Privacy/Audit" not in PARTY_360_ROLES


def test_procurement_roles_cannot_read_sample_details() -> None:
    assert "Purchase Manager" not in SAMPLE_READ_ROLES
    assert "Buyer" not in SAMPLE_READ_ROLES


def test_sales_and_product_roles_cannot_browse_procurement_board() -> None:
    assert "Sales Manager" not in SOURCING_READ_ROLES
    assert "Sales User" not in SOURCING_READ_ROLES
    assert "Product/R&D" not in SOURCING_READ_ROLES


def test_error_contract_carries_request_id_inside_error() -> None:
    envelope = build_error_envelope(
        code="revision_conflict",
        request_id="REQ-12345678",
        details={"expected": 2, "current": 3},
    )

    assert envelope["error"]["request_id"] == "REQ-12345678"
    assert envelope["error"]["message"] == "数据已被更新，请刷新后重试。"
    assert set(envelope) == {"error"}


def test_error_contract_has_actionable_chinese_fallback() -> None:
    envelope = build_error_envelope(
        code="unknown_error",
        request_id="REQ-12345678",
    )

    assert envelope["error"]["message"] == "操作未完成，请稍后重试或联系管理员。"


def test_internal_error_has_safe_actionable_chinese_message() -> None:
    assert ERROR_MESSAGES_ZH["internal_error"] == "服务暂时不可用，请稍后重试并提供请求编号。"


def test_permission_error_uses_frozen_contract_code() -> None:
    envelope = build_error_envelope(
        code="permission_denied",
        request_id="REQ-12345678",
    )

    assert envelope["error"]["code"] == "permission_denied"
    assert envelope["error"]["message"] == "你没有执行此操作的权限，请联系团队管理员。"


def test_success_contract_always_has_schema_version() -> None:
    envelope = build_success_envelope(
        data={"name": "SAM-01"},
        request_id="REQ-12345678",
        meta={"replayed": False},
    )

    assert envelope == {
        "data": {"name": "SAM-01"},
        "meta": {
            "request_id": "REQ-12345678",
            "schema_version": "1.0",
            "replayed": False,
        },
    }
