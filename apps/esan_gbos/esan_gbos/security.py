from __future__ import annotations

from typing import Any

_GBOS_PWA_CSP = "; ".join(
    (
        "default-src 'self'",
        "base-uri 'self'",
        "object-src 'none'",
        "frame-ancestors 'self'",
        "form-action 'self'",
        "img-src 'self' data: blob:",
        "font-src 'self' data:",
        "style-src 'self' 'unsafe-inline'",
        "script-src 'self'",
        "connect-src 'self' ws: wss:",
        "worker-src 'self' blob:",
        "manifest-src 'self'",
        "media-src 'self' blob:",
    )
)


def add_gbos_pwa_security_headers(response: Any, request: Any) -> None:
    """Apply a strict, PWA-only browser policy without changing upstream apps."""

    request_path = str(getattr(request, "path", ""))
    if not (request_path == "/gbos" or request_path.startswith("/gbos/")):
        return

    headers = getattr(response, "headers", None)
    if headers is None:
        return

    headers["Content-Security-Policy"] = _GBOS_PWA_CSP
    headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    headers["Cache-Control"] = "no-store,no-cache,must-revalidate,max-age=0"
    headers["Referrer-Policy"] = "same-origin"
    headers["X-Content-Type-Options"] = "nosniff"
