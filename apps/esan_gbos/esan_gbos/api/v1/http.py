from __future__ import annotations

from typing import Any

import frappe

from esan_gbos.api.v1.common import request_id
from esan_gbos.domain.errors import build_error_envelope

_BFF_PATH_PREFIXES = (
    "/api/method/esan_gbos.api.v1.",
    "/api/method/esan_gbos.api.v2.",
    "/api/method/esan_gbos.api.v3.",
    "/api/method/esan_gbos.api.v4.",
)


def normalize_bff_pre_dispatch_error(response: Any, request: Any) -> None:
    """Normalize Frappe errors raised before a BFF endpoint can run.

    Frappe validates CSRF while constructing the authenticated request, before
    whitelisted method dispatch. The BFF decorator therefore cannot catch this
    one error itself. Only the known CSRF exception on the versioned GBOS API
    surface is rewritten; every other Frappe response remains untouched.
    """

    if not str(getattr(request, "path", "")).startswith(_BFF_PATH_PREFIXES):
        return
    response.headers["Cache-Control"] = "no-store"
    response_state = getattr(frappe.local, "response", {})
    if response_state.get("exc_type") != "CSRFTokenError":
        return

    payload = {
        "message": build_error_envelope(
            code="csrf_failed",
            request_id=request_id(),
        )
    }
    response.set_data(frappe.as_json(payload))
    response.status_code = 400
    response.content_type = "application/json"
