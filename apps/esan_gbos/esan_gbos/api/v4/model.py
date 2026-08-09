from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import frappe
from frappe.utils import nowdate

from esan_gbos.api.v1.common import BFFError, bff_endpoint, require_roles
from esan_gbos.api.v4.gateway import call_local, v4_success
from esan_gbos.domain.v4_dto import V4DTOValidationError, map_model_usage, validate_period

MODEL_USAGE_ROLES = frozenset({"Integration Admin", "GBOS Admin"})


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@bff_endpoint("GET")
def get_usage(period: str | None = None) -> dict[str, Any]:
    require_roles(MODEL_USAGE_ROLES)
    period_value = period.strip() if isinstance(period, str) and period.strip() else nowdate()[:7]
    try:
        selected_period = validate_period(period_value)
    except V4DTOValidationError as error:
        raise BFFError("invalid_query", str(error)) from error
    data = call_local(
        "Agent",
        method="GET",
        path=f"/internal/v1/model/usage?{urlencode({'period': selected_period})}",
        purpose="model_usage_governance",
    )
    value = data.get("usage", data)
    try:
        usage = map_model_usage(value)
    except (TypeError, V4DTOValidationError) as error:
        raise BFFError("internal_error", "Agent usage response is invalid", status=503) from error
    return v4_success(usage)
