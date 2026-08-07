from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from .invocations import ModelInvocationRecord
from .models import (
    AgentTaskMetadata,
    ValidationError,
    canonical_payload_digest,
    freeze_json,
    thaw_json,
)

if TYPE_CHECKING:
    from .agents import AgentExecutionResult

_ALLOWED_ACTION_TYPES = {
    "internal.ai_draft.propose",
    "internal.work_item.propose",
    "internal.review_case.propose",
    "internal.work_item.transition.propose",
}
_FORBIDDEN_KEYS = {
    "raw_context",
    "message_body",
    "message_text",
    "email",
    "phone",
    "telephone",
    "person_name",
    "customer_name",
    "contact_name",
    "supplier_name",
    "organization_name",
    "prompt",
    "prompt_text",
    "response",
    "response_text",
    "tokenized_context",
    "draft_mutation",
    "approved_command",
    "external_send",
    "outbound",
    "quotation",
    "formal_price",
    "price",
    "formal_discount",
    "discount",
    "delivery_commitment",
    "delivery_promise",
    "order",
    "won",
    "lost",
    "selected_supplier",
    "supplier_final_selection",
    "official_kpi",
    "metric_value",
}
_FORBIDDEN_TEXT = (
    "external.message.send",
    "outbound email",
    "formal quotation",
    "formal price",
    "formal discount",
    "delivery commitment",
    "delivery promise",
    "create order",
    "deal won",
    "deal lost",
    "final supplier",
    "select final supplier",
    "draftmutation",
    "approvedcommand",
    "official kpi",
    "正式报价",
    "正式价格",
    "正式折扣",
    "承诺交期",
    "创建订单",
    "赢单",
    "输单",
    "最终供应商",
    "正式kpi",
)
_DIRECT_PII_PATTERN = re.compile(
    r"(?:[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})|"
    r"(?:\+\d[\d ()-]{7,}\d)|"
    r"(?:\b\d{3}[\s()]\d[\d ()-]{5,}\d\b)|"
    r"(?:\b1[3-9]\d{9}\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True, repr=False)
class ActionProposalRecord:
    proposal_id: str
    site_id: str
    idempotency_key: str
    task_id: str
    task_attempt: int
    action_type: str
    status: Literal["proposed"]
    origin: Literal["AI"]
    review_status: Literal["AI Draft"]
    subject_type: str
    subject_ref: str
    subject_revision: int
    evidence_refs: tuple[str, ...]
    fact_version_refs: tuple[tuple[str, int], ...]
    invocation_ids: tuple[str, ...]
    payload_digest: str
    bundle_digest: str
    document: Mapping[str, Any] = field(repr=False)
    created_at: datetime

    def __post_init__(self) -> None:
        if self.action_type not in _ALLOWED_ACTION_TYPES:
            raise ValidationError("proposal action type is not materializable")
        if self.status != "proposed" or self.origin != "AI" or self.review_status != "AI Draft":
            raise ValidationError("proposal must remain an AI Draft proposal")
        if self.task_attempt < 1 or self.subject_revision < 0:
            raise ValidationError("proposal task and subject revisions are invalid")
        if not self.evidence_refs or len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValidationError("proposal evidence refs must be non-empty and unique")
        if len(self.invocation_ids) != len(set(self.invocation_ids)):
            raise ValidationError("proposal invocation ids must be unique")
        if not re.fullmatch(r"[a-f0-9]{64}", self.payload_digest):
            raise ValidationError("proposal payload digest is invalid")
        if not re.fullmatch(r"[a-f0-9]{64}", self.bundle_digest):
            raise ValidationError("proposal bundle digest is invalid")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValidationError("proposal created_at must be timezone-aware")
        detached = thaw_json(freeze_json(self.document))
        _reject_unsafe_content(detached)
        object.__setattr__(self, "document", freeze_json(detached))

    def __repr__(self) -> str:
        return (
            f"ActionProposalRecord(proposal_id={self.proposal_id!r}, "
            f"site_id={self.site_id!r}, task_id={self.task_id!r}, "
            f"task_attempt={self.task_attempt}, bundle_digest={self.bundle_digest!r})"
        )

    @classmethod
    def from_execution(
        cls,
        task: AgentTaskMetadata,
        result: AgentExecutionResult,
    ) -> ActionProposalRecord:
        document = thaw_json(freeze_json(result.action_proposal))
        if not isinstance(document, dict):
            raise ValidationError("action proposal must be a JSON object")
        if (
            document.get("site_id") != task.site_id
            or document.get("target_ref") != task.subject_ref
            or document.get("status") != "proposed"
        ):
            raise ValidationError("action proposal is not bound to the claimed task")
        action_type = document.get("action_type")
        proposal_id = document.get("action_proposal_id")
        payload = document.get("payload")
        evidence_refs = document.get("evidence_refs")
        fact_refs = document.get("fact_version_refs")
        payload_digest = document.get("payload_digest")
        created_at_value = document.get("created_at")
        target_revision = document.get("target_revision")
        if (
            not isinstance(action_type, str)
            or not isinstance(proposal_id, str)
            or not isinstance(payload, dict)
            or not isinstance(evidence_refs, list)
            or not all(isinstance(value, str) for value in evidence_refs)
            or not isinstance(fact_refs, list)
            or not isinstance(payload_digest, str)
            or not isinstance(created_at_value, str)
            or not isinstance(target_revision, int)
        ):
            raise ValidationError("action proposal shape is invalid")
        if canonical_payload_digest(payload) != payload_digest:
            raise ValidationError("action proposal payload digest does not match payload")
        parsed_fact_refs: list[tuple[str, int]] = []
        for value in fact_refs:
            if (
                not isinstance(value, dict)
                or set(value) != {"fact_id", "fact_version"}
                or not isinstance(value["fact_id"], str)
                or not isinstance(value["fact_version"], int)
            ):
                raise ValidationError("action proposal fact refs are invalid")
            parsed_fact_refs.append((value["fact_id"], value["fact_version"]))
        created_at = datetime.fromisoformat(created_at_value.replace("Z", "+00:00"))
        invocation_ids = tuple(record.invocation_id for record in result.invocations)
        bundle_document = {
            "task_id": task.task_id,
            "task_attempt": task.attempt,
            "proposal": document,
            "invocations": [_invocation_fingerprint(record) for record in result.invocations],
        }
        bundle_digest = canonical_payload_digest(bundle_document)
        return cls(
            proposal_id=proposal_id,
            site_id=task.site_id,
            idempotency_key=f"proposal:{task.task_id}:{task.attempt}",
            task_id=task.task_id,
            task_attempt=task.attempt,
            action_type=action_type,
            status="proposed",
            origin="AI",
            review_status="AI Draft",
            subject_type=task.subject_type,
            subject_ref=task.subject_ref,
            subject_revision=target_revision,
            evidence_refs=tuple(evidence_refs),
            fact_version_refs=tuple(parsed_fact_refs),
            invocation_ids=invocation_ids,
            payload_digest=payload_digest,
            bundle_digest=bundle_digest,
            document=document,
            created_at=created_at,
        )


@dataclass(frozen=True, slots=True)
class MaterializationOutboxRecord:
    materialization_id: str
    proposal_id: str
    site_id: str
    task_id: str
    task_attempt: int
    status: Literal["pending"]
    origin: Literal["AI"]
    review_status: Literal["AI Draft"]
    created_at: datetime

    @classmethod
    def from_proposal(
        cls,
        proposal: ActionProposalRecord,
        *,
        created_at: datetime,
    ) -> MaterializationOutboxRecord:
        digest = hashlib.sha256(f"{proposal.site_id}:{proposal.proposal_id}".encode()).hexdigest()[
            :32
        ]
        return cls(
            materialization_id=f"materialization-{digest}",
            proposal_id=proposal.proposal_id,
            site_id=proposal.site_id,
            task_id=proposal.task_id,
            task_attempt=proposal.task_attempt,
            status="pending",
            origin="AI",
            review_status="AI Draft",
            created_at=created_at,
        )


@dataclass(frozen=True, slots=True, repr=False, kw_only=True)
class MaterializationEnvelope:
    origin: str
    review_status: str
    action_type: str
    proposal: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True, slots=True)
class MaterializationIntent:
    operation: Literal["create", "submit"]
    doctype: Literal["GBOS Work Item", "GBOS Review Case"]
    values: Mapping[str, Any]


class TrustedMaterializer:
    """Pure trusted boundary that can only shape reversible internal AI drafts."""

    def materialize(self, envelope: MaterializationEnvelope) -> MaterializationIntent:
        if envelope.origin != "AI" or envelope.review_status != "AI Draft":
            raise ValidationError("materializer only accepts AI Draft proposals")
        if envelope.action_type not in _ALLOWED_ACTION_TYPES:
            raise ValidationError("materializer rejects non-internal proposal actions")
        document = thaw_json(freeze_json(envelope.proposal))
        _reject_unsafe_content(document)
        payload = document.get("payload") if isinstance(document, dict) else None
        if not isinstance(payload, dict):
            raise ValidationError("materialization proposal payload is required")
        if envelope.action_type == "internal.work_item.transition.propose":
            expected = {
                "target_doctype",
                "target_ref",
                "from_review_status",
                "to_review_status",
            }
            if (
                set(payload) != expected
                or payload["target_doctype"] not in {"GBOS Work Item", "GBOS Review Case"}
                or payload["from_review_status"] != "AI Draft"
                or payload["to_review_status"] != "Pending"
            ):
                raise ValidationError("only AI Draft to Pending submission is allowed")
            return MaterializationIntent(
                operation="submit",
                doctype=payload["target_doctype"],
                values={
                    "name": payload["target_ref"],
                    "origin": "AI",
                    "review_status": "Pending",
                },
            )
        if envelope.action_type == "internal.work_item.propose":
            doctype: Literal["GBOS Work Item", "GBOS Review Case"] = "GBOS Work Item"
            values = {**payload, "origin": "AI", "review_status": "AI Draft"}
        else:
            doctype = "GBOS Review Case"
            values = {**payload, "origin": "AI", "review_status": "AI Draft"}
        if envelope.action_type == "internal.ai_draft.propose":
            if payload.get("is_official_metric") is not False:
                raise ValidationError("CEO observations cannot become formal metrics")
            values["observation_kind"] = "informal_communication_observation"
            values["source_basis"] = "communications"
            values["is_official_metric"] = False
        return MaterializationIntent(operation="create", doctype=doctype, values=values)


def _reject_unsafe_content(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", str(key)).casefold()
            normalized = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
            if normalized in _FORBIDDEN_KEYS:
                raise ValidationError("proposal contains forbidden authoritative content")
            _reject_unsafe_content(nested)
        return
    if isinstance(value, list | tuple):
        for nested in value:
            _reject_unsafe_content(nested)
        return
    if isinstance(value, str):
        compact = re.sub(r"[\s_-]+", "", value.casefold())
        if any(
            marker.casefold() in value.casefold()
            or re.sub(r"[\s_-]+", "", marker.casefold()) in compact
            for marker in _FORBIDDEN_TEXT
        ):
            raise ValidationError("proposal contains forbidden authoritative language")
        if _DIRECT_PII_PATTERN.search(value):
            raise ValidationError("proposal cannot store direct identity content")


def _invocation_fingerprint(record: ModelInvocationRecord) -> dict[str, Any]:
    cost_amount: Decimal | None = record.cost.amount
    return {
        "invocation_id": record.invocation_id,
        "site_id": record.site_id,
        "provider": record.provider,
        "requested_model": record.requested_model,
        "observed_model": record.observed_model,
        "prompt_version": record.prompt_version,
        "output_schema_version": record.output_schema_version,
        "policy_version": record.policy_version,
        "tokenizer_version": record.tokenizer_version,
        "request_id": record.request_id,
        "response_id": record.response_id,
        "started_at": record.started_at.isoformat(),
        "completed_at": (None if record.completed_at is None else record.completed_at.isoformat()),
        "latency_ms": record.latency_ms,
        "status": record.status,
        "token_usage": record.token_usage.to_wire(),
        "cost": {
            "status": record.cost.status,
            "amount": None if cost_amount is None else str(cost_amount),
            "currency": record.cost.currency,
        },
        "network_call_count": record.network_call_count,
        "tool_call_count": record.tool_call_count,
        "external_send_count": record.external_send_count,
        "references": record.references.to_wire(),
        "idempotency_key": record.idempotency_key,
        "attempt": record.attempt,
        "retry_count": record.retry_count,
        "finish_code": record.finish_code,
        "error_code": record.error_code,
        "budget_status": record.budget_status,
        "price_catalog_version": record.price_catalog_version,
        "output_digest": record.output_digest,
    }


def proposal_document_json(record: ActionProposalRecord) -> str:
    return json.dumps(thaw_json(record.document), ensure_ascii=False, sort_keys=True)
