from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from datetime import datetime
from typing import Any


class V4DTOValidationError(ValueError):
    """A local or downstream value violates the frozen v4 wire contract."""


def validate_period(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value) is None:
        raise V4DTOValidationError("period must use YYYY-MM")
    return value


_CONNECTOR_REQUIRED = frozenset({"instance_id", "expected_revision", "idempotency_key"})
_COMMUNICATION_SUMMARY_FIELDS = (
    "observation_id",
    "channel",
    "occurred_at",
    "summary_zh",
    "original_language",
    "classification",
    "review_status",
    "team_ref",
    "party_ref",
    "evidence_count",
)
_USAGE_STATES = frozenset({"known", "partial", "unknown"})
_OPAQUE_IDENTITY_REF = re.compile(
    r"^extid:v1:(email|wecom|whatsapp|phone|manual_import):"
    r"([A-Za-z0-9_-]{43})$"
)


def _closed(
    payload: Mapping[str, Any],
    *,
    required: Collection[str],
    optional: Collection[str] = (),
) -> None:
    supplied = frozenset(payload)
    required_fields = frozenset(required)
    missing = required_fields - supplied
    unexpected = supplied - required_fields - frozenset(optional)
    if missing:
        raise V4DTOValidationError(f"missing required fields: {', '.join(sorted(missing))}")
    if unexpected:
        raise V4DTOValidationError(f"unexpected fields: {', '.join(sorted(unexpected))}")


def _text(value: object, field: str, *, maximum: int = 2000) -> str:
    if not isinstance(value, str):
        raise V4DTOValidationError(f"{field} must be a nonempty string")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or "\x00" in normalized:
        raise V4DTOValidationError(f"{field} must be a nonempty string")
    return normalized


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _integer(value: object, field: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise V4DTOValidationError(f"{field} must be an integer >= {minimum}")
    return value


def _number(value: object, field: str, *, strictly_positive: bool = False) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise V4DTOValidationError(f"{field} must be a number")
    parsed = float(value)
    if parsed < 0 or (strictly_positive and parsed <= 0):
        raise V4DTOValidationError(f"{field} is outside the allowed range")
    return parsed


def _enum(value: object, field: str, allowed: Collection[str]) -> str:
    normalized = _text(value, field)
    if normalized not in allowed:
        raise V4DTOValidationError(f"{field} is not an allowed value")
    return normalized


def _date_time(value: object, field: str) -> str:
    normalized = _text(value, field)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise V4DTOValidationError(f"{field} must be an ISO date-time") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise V4DTOValidationError(f"{field} must include a timezone")
    return normalized


def validate_connector_command(payload: dict[str, Any]) -> dict[str, Any]:
    _closed(payload, required=_CONNECTOR_REQUIRED)
    key = _text(payload["idempotency_key"], "idempotency_key", maximum=256)
    if len(key) < 8:
        raise V4DTOValidationError("idempotency_key must contain 8 to 256 characters")
    return {
        "instance_id": _text(payload["instance_id"], "instance_id"),
        "expected_revision": _integer(payload["expected_revision"], "expected_revision"),
        "idempotency_key": key,
    }


def validate_ai_draft_submit(payload: dict[str, Any]) -> dict[str, Any]:
    _closed(payload, required={"draft_id", "expected_revision", "idempotency_key"})
    key = _text(payload["idempotency_key"], "idempotency_key", maximum=256)
    if len(key) < 8:
        raise V4DTOValidationError("idempotency_key must contain 8 to 256 characters")
    return {
        "draft_id": _text(payload["draft_id"], "draft_id"),
        "expected_revision": _integer(payload["expected_revision"], "expected_revision"),
        "idempotency_key": key,
    }


def map_connector_status(payload: Mapping[str, Any]) -> dict[str, Any]:
    fields = {
        "instance_id",
        "channel",
        "status",
        "checkpoint_version",
        "backlog",
        "last_success_at",
        "safe_error_code",
        "freshness",
        "revision",
    }
    _closed(payload, required=fields)
    last_success = payload["last_success_at"]
    return {
        "instance_id": _text(payload["instance_id"], "instance_id"),
        "channel": _text(payload["channel"], "channel"),
        "status": _enum(
            payload["status"],
            "status",
            {"enabled", "paused", "error", "disabled"},
        ),
        "checkpoint_version": _integer(
            payload["checkpoint_version"],
            "checkpoint_version",
        ),
        "backlog": _integer(payload["backlog"], "backlog"),
        "last_success_at": (
            None if last_success is None else _date_time(last_success, "last_success_at")
        ),
        "safe_error_code": _optional_text(payload["safe_error_code"], "safe_error_code"),
        "freshness": _enum(payload["freshness"], "freshness", {"fresh", "stale", "unknown"}),
        "revision": _integer(payload["revision"], "revision"),
    }


def map_communication_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    _closed(payload, required=_COMMUNICATION_SUMMARY_FIELDS)
    return {
        "observation_id": _text(payload["observation_id"], "observation_id"),
        "channel": _text(payload["channel"], "channel"),
        "occurred_at": _date_time(payload["occurred_at"], "occurred_at"),
        "summary_zh": _text(payload["summary_zh"], "summary_zh"),
        "original_language": _text(payload["original_language"], "original_language"),
        "classification": _text(payload["classification"], "classification"),
        "review_status": _text(payload["review_status"], "review_status"),
        "team_ref": _optional_text(payload["team_ref"], "team_ref"),
        "party_ref": _optional_text(payload["party_ref"], "party_ref"),
        "evidence_count": _integer(payload["evidence_count"], "evidence_count"),
    }


def _map_model_metadata(payload: object) -> dict[str, str]:
    if not isinstance(payload, Mapping):
        raise V4DTOValidationError("model must be an object")
    _closed(payload, required={"name", "version"})
    if payload["name"] != "deepseek-v4-flash":
        raise V4DTOValidationError("model.name is not allowed")
    return {
        "name": "deepseek-v4-flash",
        "version": _text(payload["version"], "model.version"),
    }


def _map_evidence(payload: object) -> list[dict[str, str]]:
    if not isinstance(payload, list) or len(payload) > 100:
        raise V4DTOValidationError("evidence must be a list with at most 100 entries")
    result: list[dict[str, str]] = []
    for index, item in enumerate(payload):
        if not isinstance(item, Mapping):
            raise V4DTOValidationError(f"evidence[{index}] must be an object")
        _closed(item, required={"ref", "locator"})
        result.append(
            {
                "ref": _text(item["ref"], f"evidence[{index}].ref"),
                "locator": _text(item["locator"], f"evidence[{index}].locator"),
            }
        )
    return result


def _map_fact_proposals(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or len(payload) > 100:
        raise V4DTOValidationError("fact_proposals must be a bounded list")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(payload):
        if not isinstance(item, Mapping):
            raise V4DTOValidationError(f"fact_proposals[{index}] must be an object")
        _closed(item, required={"status", "confidence", "type", "value_display"})
        confidence = _number(item["confidence"], f"fact_proposals[{index}].confidence")
        if confidence > 1:
            raise V4DTOValidationError("fact proposal confidence must be <= 1")
        result.append(
            {
                "status": _text(item["status"], "status"),
                "confidence": confidence,
                "type": _text(item["type"], "type"),
                "value_display": _text(item["value_display"], "value_display"),
            }
        )
    return result


def _map_associations(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or len(payload) > 100:
        raise V4DTOValidationError("association_suggestions must be a bounded list")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(payload):
        if not isinstance(item, Mapping):
            raise V4DTOValidationError(f"association_suggestions[{index}] must be an object")
        _closed(
            item,
            required={"type", "target_ref", "confidence", "suggestion_key"},
        )
        confidence = _number(item["confidence"], f"association_suggestions[{index}].confidence")
        if confidence > 1:
            raise V4DTOValidationError("association confidence must be <= 1")
        suggestion_key = _text(
            item["suggestion_key"],
            f"association_suggestions[{index}].suggestion_key",
            maximum=78,
        )
        if re.fullmatch(r"suggestion:v1:[a-f0-9]{64}", suggestion_key) is None:
            raise V4DTOValidationError("association suggestion key is invalid")
        # target_ref is model provenance used only by the BFF submission path.  Validate it
        # as part of the closed Observer response, but never project it to a browser client.
        _text(item["target_ref"], f"association_suggestions[{index}].target_ref")
        result.append(
            {
                "type": _text(item["type"], "type"),
                "confidence": confidence,
                "suggestion_key": suggestion_key,
            }
        )
    return result


def _map_participant_identities(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or len(payload) > 100:
        raise V4DTOValidationError("participant_identities must be a bounded list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(payload):
        if not isinstance(item, Mapping):
            raise V4DTOValidationError(f"participant_identities[{index}] must be an object")
        base = {"identity_ref", "provider", "status"}
        mapping = {"mapping_ref", "mapping_revision", "target_type"}
        _closed(item, required=base, optional=mapping)
        identity_ref = _text(item["identity_ref"], f"participant_identities[{index}].identity_ref")
        provider = _enum(
            item["provider"],
            f"participant_identities[{index}].provider",
            {"email", "wecom", "whatsapp", "phone", "manual_import"},
        )
        match = _OPAQUE_IDENTITY_REF.fullmatch(identity_ref)
        if match is None or match.group(1) != provider or identity_ref in seen:
            raise V4DTOValidationError("participant identity reference is invalid")
        seen.add(identity_ref)
        status = _enum(
            item["status"],
            f"participant_identities[{index}].status",
            {"unresolved", "confirmed", "revoked"},
        )
        has_mapping = any(field in item for field in mapping)
        if has_mapping != all(field in item for field in mapping):
            raise V4DTOValidationError("participant identity mapping metadata is incomplete")
        value: dict[str, Any] = {
            "identity_ref": identity_ref,
            "provider": provider,
            "status": status,
        }
        if has_mapping:
            value.update(
                {
                    "mapping_ref": _text(
                        item["mapping_ref"],
                        f"participant_identities[{index}].mapping_ref",
                    ),
                    "mapping_revision": _integer(
                        item["mapping_revision"],
                        f"participant_identities[{index}].mapping_revision",
                        minimum=1,
                    ),
                    "target_type": _enum(
                        item["target_type"],
                        f"participant_identities[{index}].target_type",
                        {"User", "Party"},
                    ),
                }
            )
        return_status_has_mapping = status in {"confirmed", "revoked"}
        if return_status_has_mapping != has_mapping:
            raise V4DTOValidationError("participant identity status and mapping metadata disagree")
        result.append(value)
    return result


def map_communication_detail(payload: dict[str, Any]) -> dict[str, Any]:
    detail_fields = {
        "evidence",
        "fact_proposals",
        "association_suggestions",
        "participant_identities",
        "connector_account_user_ref",
        "model",
        "raw_access_allowed",
    }
    _closed(
        payload,
        required={*_COMMUNICATION_SUMMARY_FIELDS, *detail_fields},
        optional={"original_text"},
    )
    summary = map_communication_summary(
        {field: payload[field] for field in _COMMUNICATION_SUMMARY_FIELDS}
    )
    _optional_text(
        payload["connector_account_user_ref"],
        "connector_account_user_ref",
    )
    restricted = payload["classification"] == "Restricted"
    raw_allowed = payload["raw_access_allowed"] is True and not restricted
    result: dict[str, Any] = {
        **summary,
        "evidence": _map_evidence(payload["evidence"]),
        "fact_proposals": _map_fact_proposals(payload["fact_proposals"]),
        "association_suggestions": _map_associations(payload["association_suggestions"]),
        "participant_identities": _map_participant_identities(payload["participant_identities"]),
        "model": _map_model_metadata(payload["model"]),
        "raw_access_allowed": raw_allowed,
    }
    if raw_allowed and "original_text" in payload:
        result["original_text"] = _text(payload["original_text"], "original_text")
    return result


def map_model_usage(payload: dict[str, Any]) -> dict[str, Any]:
    required = {
        "model",
        "period",
        "tokens",
        "token_state",
        "cost",
        "soft_limit_usd",
        "hard_limit_usd",
        "state",
    }
    _closed(payload, required=required)
    if payload["model"] != "deepseek-v4-flash":
        raise V4DTOValidationError("model is not allowed")
    tokens = payload["tokens"]
    if tokens is not None:
        tokens = _integer(tokens, "tokens")
    cost = payload["cost"]
    if not isinstance(cost, Mapping):
        raise V4DTOValidationError("cost must be an object")
    _closed(cost, required={"currency", "amount", "state"})
    if cost["currency"] != "USD":
        raise V4DTOValidationError("cost.currency must be USD")
    amount = cost["amount"]
    if amount is not None:
        amount = _number(amount, "cost.amount")
    return {
        "model": "deepseek-v4-flash",
        "period": _text(payload["period"], "period"),
        "tokens": tokens,
        "token_state": _enum(payload["token_state"], "token_state", _USAGE_STATES),
        "cost": {
            "currency": "USD",
            "amount": amount,
            "state": _enum(cost["state"], "cost.state", _USAGE_STATES),
        },
        "soft_limit_usd": _number(payload["soft_limit_usd"], "soft_limit_usd"),
        "hard_limit_usd": _number(
            payload["hard_limit_usd"],
            "hard_limit_usd",
            strictly_positive=True,
        ),
        "state": _enum(
            payload["state"],
            "state",
            {"normal", "soft_limit", "hard_limit", "unknown"},
        ),
    }


def map_ai_draft(payload: Mapping[str, Any]) -> dict[str, Any]:
    _closed(
        payload,
        required={
            "draft_id",
            "kind",
            "status",
            "origin",
            "subject",
            "evidence",
            "model",
            "revision",
        },
    )
    if payload["origin"] != "AI":
        raise V4DTOValidationError("draft origin must be AI")
    return {
        "draft_id": _text(payload["draft_id"], "draft_id"),
        "kind": _enum(
            payload["kind"],
            "kind",
            {"Work Item", "Review Case", "CEO Informal Observation"},
        ),
        "status": _enum(payload["status"], "status", {"AI Draft", "Pending"}),
        "origin": "AI",
        "subject": _text(payload["subject"], "subject"),
        "evidence": _map_evidence(payload["evidence"]),
        "model": _map_model_metadata(payload["model"]),
        "revision": _integer(payload["revision"], "revision"),
    }
