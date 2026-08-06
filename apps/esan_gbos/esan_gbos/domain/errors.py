from __future__ import annotations

from typing import Any

ERROR_MESSAGES_ZH = {
    "method_not_allowed": "请求方式不正确，请刷新页面后重试。",
    "authentication_required": "登录已失效，请重新登录后继续。",
    "csrf_failed": "请求安全校验失败，请刷新页面后重试。",
    "permission_denied": "你没有执行此操作的权限，请联系团队管理员。",
    "not_found": "未找到该记录，可能已被删除或你无权查看。",
    "validation_error": "提交内容不符合要求，请检查后重试。",
    "invalid_dto": "提交字段不符合接口要求，请刷新页面后重试。",
    "invalid_query": "查询条件不受支持，请调整筛选条件。",
    "invalid_cursor": "列表位置已失效，请刷新列表。",
    "scope_mismatch": "关联记录不属于同一团队，请重新选择。",
    "revision_conflict": "数据已被更新，请刷新后重试。",
    "invalid_transition": "当前状态不允许执行此操作，请刷新后确认状态。",
    "idempotency_conflict": "该请求标识已用于其他操作，请重新提交。",
    "request_in_progress": "相同请求正在处理中，请勿重复提交。",
    "internal_error": "服务暂时不可用，请稍后重试并提供请求编号。",
}
DEFAULT_ERROR_MESSAGE_ZH = "操作未完成，请稍后重试或联系管理员。"


def build_error_envelope(
    *,
    code: str,
    request_id: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": ERROR_MESSAGES_ZH.get(code, DEFAULT_ERROR_MESSAGE_ZH),
            "request_id": request_id,
            "details": details or {},
        }
    }


def build_success_envelope(
    *,
    data: Any,
    request_id: str,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "data": data,
        "meta": {
            "request_id": request_id,
            "schema_version": "1.0",
            **(meta or {}),
        },
    }
