from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .audit import AuditEvent, AuditSink, NullAuditSink, account_set_fingerprint
from .models import (
    AdapterResponse,
    AdapterStatus,
    AuthContext,
    ControlMetrics,
    QueryPlan,
    ValidatedRequest,
    VerificationSnapshot,
    VerificationStatus,
)
from .policy import FrozenKingdeePolicy, RequestRejected
from .transport import Transport, TransportResult


class KingdeeAdapter:
    """Independent read-only Kingdee/MCP policy enforcement boundary."""

    def __init__(
        self,
        *,
        policy: FrozenKingdeePolicy,
        transport: Transport,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self._policy = policy
        self._transport = transport
        self._audit = audit_sink or NullAuditSink()

    def invoke(
        self,
        tool_name: str,
        request: Mapping[str, Any],
        *,
        auth: AuthContext,
    ) -> AdapterResponse:
        try:
            validated = self._policy.validate_request(request, auth=auth)
            plan = self._policy.plan_for(tool_name, validated.logical_object)
        except RequestRejected:
            self._record_rejection(tool_name)
            raise
        transport_result = self._transport.execute(plan=plan, request=validated)
        transport_result = self._validate_transport_result(
            plan=plan,
            request=validated,
            result=transport_result,
        )
        response = self._response(
            plan=plan,
            request=validated,
            result=transport_result,
        )
        self._audit.record(
            AuditEvent(
                event_type="kingdee_read",
                request_id=validated.request_id,
                site_id=validated.site_id,
                account_set_fingerprint=account_set_fingerprint(validated.account_set_ref),
                processing_purpose=validated.processing_purpose,
                tool_name=tool_name,
                logical_object=validated.logical_object,
                status=response.status.value,
                reason_code=response.reason_code,
                returned_rows=len(response.rows),
                synthetic=response.synthetic,
                network_calls=response.controls.network_calls,
                writer_tools_discovered=0,
                mutation_attempts=0,
            )
        )
        return response

    dispatch = invoke
    handle_request = invoke

    def _record_rejection(self, tool_name: object) -> None:
        safe_tool_name = (
            tool_name
            if isinstance(tool_name, str) and tool_name in self._policy.tools
            else "rejected-tool"
        )
        self._audit.record(
            AuditEvent(
                event_type="kingdee_read_rejected",
                request_id="rejected-request",
                site_id="unknown",
                account_set_fingerprint="sha256:unavailable",
                processing_purpose="unknown",
                tool_name=safe_tool_name,
                logical_object="unknown",
                status="denied",
                reason_code="request_rejected",
                returned_rows=0,
                synthetic=self._transport.synthetic,
                network_calls=0,
                writer_tools_discovered=0,
                mutation_attempts=0,
            )
        )

    def _response(
        self,
        *,
        plan: QueryPlan,
        request: ValidatedRequest,
        result: TransportResult,
    ) -> AdapterResponse:
        tool_name = plan.tool_name
        status = AdapterStatus.AVAILABLE if result.available_status else AdapterStatus.UNAVAILABLE
        is_metadata = tool_name == "metadata.get"
        startup = (
            VerificationStatus.VERIFIED
            if result.startup_available
            else VerificationStatus.UNAVAILABLE
        )
        metadata = (
            VerificationStatus.VERIFIED
            if result.metadata_available
            else VerificationStatus.UNAVAILABLE
        )
        if is_metadata:
            business = VerificationStatus.NOT_ATTEMPTED
        else:
            business = (
                VerificationStatus.VERIFIED
                if result.available_status
                else VerificationStatus.UNAVAILABLE
            )
        rows = result.rows if result.available_status else ()
        returned_rows = len(rows)
        page = {
            "limit": request.limit,
            "offset": request.offset,
            "returned_rows": returned_rows,
            "has_more": result.available_status
            and not is_metadata
            and returned_rows == request.limit,
        }
        metadata_payload: Mapping[str, Any] = {}
        if result.available_status and is_metadata:
            metadata_payload = {
                "logical_object": plan.logical_object,
                "source_form": plan.source_form,
                "fields": [
                    {
                        "logical_name": logical_name,
                        "source_field": source_field,
                        "data_type": data_type,
                        "verification_status": (
                            "synthetic_only" if self._transport.synthetic else "live_verified"
                        ),
                    }
                    for logical_name, source_field, data_type in zip(
                        plan.fields,
                        plan.source_fields,
                        plan.field_types,
                        strict=True,
                    )
                ],
            }
        elif result.available_status:
            metadata_payload = {
                "source": (
                    "gate5_deterministic_synthetic"
                    if self._transport.synthetic
                    else "kingdee_live_read"
                )
            }
        return AdapterResponse(
            status=status,
            request_id=request.request_id,
            site_id=request.site_id,
            logical_object=request.logical_object,
            tool_name=tool_name,
            synthetic=self._transport.synthetic,
            rows=rows,
            metadata=metadata_payload,
            page=page,
            verification=VerificationSnapshot(
                startup=startup,
                authentication=VerificationStatus.VERIFIED,
                metadata=metadata,
                business=business,
            ),
            controls=ControlMetrics(
                network_calls=result.network_calls,
                writer_tools_discovered=0,
                mutation_attempts=0,
                synthetic_fallbacks=0,
            ),
            reason_code=result.reason_code,
        )

    def _validate_transport_result(
        self,
        *,
        plan: QueryPlan,
        request: ValidatedRequest,
        result: TransportResult,
    ) -> TransportResult:
        if not result.available_status:
            return result
        if plan.tool_name == "metadata.get":
            valid = not result.rows
        else:
            valid = len(result.rows) <= request.limit and all(
                self._valid_row(plan, row) for row in result.rows
            )
        if valid:
            return result
        return TransportResult.unavailable(
            reason_code="transport_result_rejected",
            network_calls=result.network_calls,
            startup_available=result.startup_available,
            metadata_available=result.metadata_available,
        )

    def _valid_row(self, plan: QueryPlan, row: Mapping[str, Any]) -> bool:
        required_keys = {"record_ref", "values"}
        if self._transport.synthetic:
            required_keys.add("synthetic")
        if set(row) != required_keys:
            return False
        if self._transport.synthetic and row.get("synthetic") is not True:
            return False
        record_ref = row.get("record_ref")
        values = row.get("values")
        if not isinstance(record_ref, str) or not 1 <= len(record_ref) <= 256:
            return False
        if not isinstance(values, Mapping) or set(values) != set(plan.fields):
            return False
        return all(
            _value_matches_type(values[field], data_type)
            for field, data_type in zip(plan.fields, plan.field_types, strict=True)
        )


def _value_matches_type(value: object, data_type: str) -> bool:
    if data_type in {"string", "date"}:
        return isinstance(value, str)
    if data_type == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    return isinstance(value, str | int | float | bool) or value is None
