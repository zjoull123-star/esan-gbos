"""Controlled Frappe context resolution for Agent materialization."""

from __future__ import annotations

from typing import Any

from .frappe_client import (
    FrappeClientError,
    FrappeJsonTransport,
    _FrappeHttpBoundary,
)
from .materialization import MaterializationContextRequest
from .proposals import MaterializationContext

_RESOLVE_PATH = "/api/method/esan_gbos.api.internal.materialization.resolve_context"


class HttpMaterializationContextResolver:
    """Resolve a revision-pinned GBOS subject over the local Frappe boundary."""

    __slots__ = ("_auth_ref", "_boundary")

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        api_secret: str,
        auth_ref: str,
        site_id: str,
        timeout_seconds: float = 3.0,
        transport: FrappeJsonTransport | None = None,
    ) -> None:
        self._boundary = _FrappeHttpBoundary(
            base_url=base_url,
            api_key=api_key,
            api_secret=api_secret,
            auth_ref=auth_ref,
            site_id=site_id,
            timeout_seconds=timeout_seconds,
            transport=transport,
        )
        self._auth_ref = auth_ref

    def __repr__(self) -> str:
        return (
            "HttpMaterializationContextResolver("
            f"site_id={self._boundary.site_id!r}, auth_ref={self._auth_ref!r}, "
            "credentials=<redacted>)"
        )

    def resolve(
        self,
        request: MaterializationContextRequest,
    ) -> MaterializationContext | None:
        if request.site_id != self._boundary.site_id:
            raise FrappeClientError("materialization context request has mismatched site")
        response = self._boundary.post(
            path=_RESOLVE_PATH,
            purpose=request.processing_purpose,
            request_id=request.task_id,
            payload={
                "site_id": request.site_id,
                "processing_purpose": request.processing_purpose,
                "request_id": request.task_id,
                "auth_ref": self._auth_ref,
                "task_id": request.task_id,
                "proposal_id": request.proposal_id,
                "subject_type": request.subject_type,
                "subject_ref": request.subject_ref,
                "subject_revision": request.subject_revision,
            },
        )
        if not _matches_request(response, request):
            raise FrappeClientError("Frappe returned mismatched materialization context")
        snapshot = response.get("subject_snapshot")
        team = response.get("team")
        reviewer = response.get("assigned_reviewer")
        digest = response.get("subject_payload_digest")
        if (
            not isinstance(snapshot, dict)
            or not isinstance(team, str)
            or not team
            or (reviewer is not None and not isinstance(reviewer, str))
            or not isinstance(digest, str)
        ):
            raise FrappeClientError("Frappe returned invalid materialization context")
        try:
            return MaterializationContext(
                team=team,
                assigned_reviewer=reviewer,
                subject_snapshot=snapshot,
                subject_payload_digest=digest,
            )
        except Exception:
            raise FrappeClientError("Frappe returned invalid materialization context") from None


def _matches_request(
    value: dict[str, Any],
    request: MaterializationContextRequest,
) -> bool:
    return (
        value.get("site_id") == request.site_id
        and value.get("request_id") == request.task_id
        and value.get("subject_type") == request.subject_type
        and value.get("subject_ref") == request.subject_ref
        and value.get("subject_revision") == request.subject_revision
    )


__all__ = ["HttpMaterializationContextResolver"]
