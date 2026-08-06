from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Request

from .models import MetricQuery, ValidationError
from .service import MetricsService

_QUERY_FIELDS = frozenset({"window_start", "window_end"})


def create_metrics_app(
    *,
    service: MetricsService,
    clock: Callable[[], datetime],
) -> FastAPI:
    application = FastAPI(
        title="ESAN GBOS Gate 5 Governed Metrics",
        version="1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @application.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "gate": 5,
            "source_mode": service.source_mode.value,
            "synthetic": service.source_mode.value == "synthetic",
            "arbitrary_query": False,
        }

    @application.get("/v1/metrics/{metric_key}")
    def get_metric(
        metric_key: str,
        request: Request,
        window_start: datetime,
        window_end: datetime,
        x_site_id: Annotated[str | None, Header(alias="X-Site-ID")] = None,
        x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
    ) -> dict[str, Any]:
        if set(request.query_params) != _QUERY_FIELDS or any(
            len(request.query_params.getlist(field)) != 1 for field in _QUERY_FIELDS
        ):
            raise HTTPException(status_code=400, detail="unsupported query field")
        if not x_site_id or not x_request_id:
            raise HTTPException(status_code=400, detail="required governed header is missing")
        try:
            service.registry.require(metric_key)
        except ValidationError as exc:
            raise HTTPException(status_code=404, detail="metric not registered") from exc
        try:
            query = MetricQuery(
                site_id=x_site_id,
                metric_key=metric_key,
                window_start=window_start,
                window_end=window_end,
                queried_at=clock(),
                request_id=x_request_id,
            )
            return service.query(query)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return application
