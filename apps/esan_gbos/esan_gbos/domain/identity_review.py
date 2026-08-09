"""Governed materialization of model association suggestions for human review.

This module is deliberately a domain service rather than a whitelisted endpoint.  Callers
must pass a closed request object; model-supplied target references are retained only as
hashed provenance, while separately selected candidates receive exact Frappe validation.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, cast

import frappe

from esan_gbos.api.v1.audit import run_idempotent
from esan_gbos.domain.review_dto import canonical_payload_hash
from esan_gbos.gbos.doctype.gbos_external_identity.gbos_external_identity import (
    validate_external_subject,
)
from esan_gbos.gbos.doctype.gbos_review_case.gbos_review_case import (
    build_case_payload,
    build_subject_snapshot,
)

_MATERIALIZE_FIELDS = frozenset(
    {
        "team",
        "identity_provider",
        "external_subject_ref",
        "observation_id",
        "suggestion_key",
        "association_type",
        "model_suggested_target_ref",
        "selected_candidate_type",
        "selected_candidate_ref",
        "evidence_refs",
        "policy_version",
        "idempotency_key",
        "request_id",
    }
)
_SUBMIT_FIELDS = frozenset(
    {
        "name",
        "team",
        "observation_id",
        "suggestion_key",
        "association_type",
        "model_suggested_target_ref",
        "selected_candidate_type",
        "selected_candidate_ref",
        "assigned_reviewer",
        "expected_revision",
        "evidence_refs",
        "policy_version",
        "idempotency_key",
        "request_id",
    }
)
_ASSOCIATION_TYPES = frozenset({"user", "party", "contact"})
_CANDIDATE_TYPES = frozenset({"User", "Party", "Contact"})
_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@~-]*$")
_OBSERVATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,47}$")
_SUGGESTION_KEY = re.compile(r"^suggestion:v1:[a-f0-9]{64}$")


class IdentityReviewError(ValueError):
    """A fail-closed, non-sensitive refusal from the identity review service."""


def materialize_association_suggestion(request: Mapping[str, Any]) -> dict[str, Any]:
    """Create one non-authoritative External Identity draft from a closed suggestion."""
    payload = _materialize_payload(request)
    _require_actor_team(str(payload["team"]))

    def execute() -> dict[str, Any]:
        target = _resolve_candidate(
            team=str(payload["team"]),
            candidate_type=str(payload["selected_candidate_type"]),
            candidate_ref=str(payload["selected_candidate_ref"]),
        )
        origin_reference = _origin_reference(payload)
        duplicate_subject = frappe.db.exists(
            "GBOS External Identity",
            {
                "identity_provider": payload["identity_provider"],
                "external_subject": payload["external_subject_ref"],
            },
        )
        duplicate_candidate = frappe.db.exists(
            "GBOS External Identity",
            {"origin": "AI", "origin_reference": origin_reference},
        )
        if duplicate_subject or duplicate_candidate:
            raise IdentityReviewError("identity candidate already exists")

        values: dict[str, Any] = {
            "doctype": "GBOS External Identity",
            "team": payload["team"],
            "identity_provider": payload["identity_provider"],
            "external_subject": payload["external_subject_ref"],
            "identity_type": target["identity_type"],
            "user": target["user"],
            "party_profile": target["party_profile"],
            "origin": "AI",
            "origin_reference": origin_reference,
            "business_status": "Active",
            "review_status": "AI Draft",
            "last_request_id": payload["request_id"],
        }
        try:
            mapping = frappe.get_doc(values).insert(ignore_permissions=True)
        except frappe.DuplicateEntryError:
            raise IdentityReviewError("identity candidate already exists") from None
        return _mapping_receipt(mapping, str(payload["request_id"]))

    result, _replayed, _original_request_id = run_idempotent(
        "identity_review.materialize",
        str(payload["idempotency_key"]),
        payload,
        execute,
        api_version="domain",
    )
    return cast("dict[str, Any]", result)


def submit_for_review(request: Mapping[str, Any]) -> dict[str, Any]:
    """Pin and submit one AI External Identity draft to one qualified reviewer."""
    payload = _submit_payload(request)
    _require_actor_team(str(payload["team"]))

    def execute() -> dict[str, Any]:
        mapping = _locked_mapping(str(payload["name"]))
        _validate_draft_binding(mapping, payload)
        _validate_reviewer(
            team=str(payload["team"]),
            reviewer=str(payload["assigned_reviewer"]),
        )
        if frappe.db.exists(
            "GBOS Review Case",
            {
                "subject_doctype": "GBOS External Identity",
                "subject_name": mapping.name,
            },
        ):
            raise IdentityReviewError("identity draft was already submitted")

        mapping.flags.gbos_ai_draft_command = True
        mapping.review_status = "Pending"
        mapping.last_request_id = payload["request_id"]
        mapping.save(ignore_permissions=True)

        snapshot = build_subject_snapshot(mapping)
        snapshot_hash = canonical_payload_hash(snapshot)
        case_values: dict[str, Any] = {
            "doctype": "GBOS Review Case",
            "title": "Identity association review",
            "team": payload["team"],
            "assigned_reviewer": payload["assigned_reviewer"],
            "subject_doctype": "GBOS External Identity",
            "subject_name": mapping.name,
            "subject_revision": int(mapping.revision),
            "subject_payload_sha256": snapshot_hash,
            "subject_snapshot": _json(snapshot),
            "case_payload_sha256": "",
            "evidence_refs": _json(payload["evidence_refs"]),
            "policy_version": payload["policy_version"],
            "origin": "AI",
            "origin_reference": mapping.origin_reference,
            "business_status": "Pending",
            "review_status": "AI Draft",
            "last_request_id": payload["request_id"],
        }
        provisional_case = frappe.get_doc(case_values)
        provisional_case.case_payload_sha256 = canonical_payload_hash(
            build_case_payload(provisional_case)
        )
        try:
            review_case = provisional_case.insert(ignore_permissions=True)
        except frappe.DuplicateEntryError:
            raise IdentityReviewError("identity draft was already submitted") from None
        review_case.flags.gbos_review_command = True
        review_case.flags.gbos_ai_draft_command = True
        review_case.review_status = "Pending"
        review_case.last_request_id = payload["request_id"]
        review_case.save(ignore_permissions=True)
        return {
            "doctype": "GBOS Review Case",
            "name": review_case.name,
            "review_status": review_case.review_status,
            "revision": int(review_case.revision),
            "subject_name": mapping.name,
            "subject_revision": int(mapping.revision),
            "request_id": payload["request_id"],
        }

    result, _replayed, _original_request_id = run_idempotent(
        "identity_review.submit",
        str(payload["idempotency_key"]),
        payload,
        execute,
        api_version="domain",
    )
    return cast("dict[str, Any]", result)


def _materialize_payload(request: Mapping[str, Any]) -> dict[str, Any]:
    payload = _closed_object(request, _MATERIALIZE_FIELDS)
    normalized = _common_proposal_fields(payload)
    normalized["identity_provider"] = _text(
        payload["identity_provider"], "identity provider", maximum=32
    )
    try:
        validate_external_subject(
            normalized["identity_provider"],
            normalized["external_subject_ref"],
        )
    except ValueError as error:
        raise IdentityReviewError(str(error)) from None
    return normalized


def _submit_payload(request: Mapping[str, Any]) -> dict[str, Any]:
    payload = _closed_object(request, _SUBMIT_FIELDS)
    normalized = _common_proposal_fields(payload)
    normalized["name"] = _text(payload["name"], "mapping name", maximum=140)
    normalized["assigned_reviewer"] = _text(
        payload["assigned_reviewer"], "assigned reviewer", maximum=140
    )
    revision = payload["expected_revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise IdentityReviewError("expected revision must be a positive integer")
    normalized["expected_revision"] = revision
    return normalized


def _common_proposal_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    team = _text(payload["team"], "team", maximum=140)
    observation_id = _text(payload["observation_id"], "observation", maximum=48)
    suggestion_key = _text(payload["suggestion_key"], "suggestion key", maximum=78)
    association_type = payload["association_type"]
    if not isinstance(association_type, str) or association_type not in _ASSOCIATION_TYPES:
        raise IdentityReviewError("association type is not allowed")
    model_target_ref = _text(
        payload["model_suggested_target_ref"],
        "model target reference",
        maximum=256,
    )
    candidate_type = payload["selected_candidate_type"]
    if not isinstance(candidate_type, str) or candidate_type not in _CANDIDATE_TYPES:
        raise IdentityReviewError("selected candidate type is not allowed")
    candidate_ref = _text(
        payload["selected_candidate_ref"],
        "selected candidate reference",
        maximum=256,
    )
    if _SAFE_REF.fullmatch(model_target_ref) is None:
        raise IdentityReviewError("model target reference is invalid")
    if _SAFE_REF.fullmatch(candidate_ref) is None:
        raise IdentityReviewError("selected candidate reference is invalid")
    if _OBSERVATION.fullmatch(observation_id) is None:
        raise IdentityReviewError("observation reference is invalid")
    if _SUGGESTION_KEY.fullmatch(suggestion_key) is None:
        raise IdentityReviewError("suggestion key is invalid")
    return {
        **dict(payload),
        "team": team,
        "external_subject_ref": _text(
            payload.get("external_subject_ref", ""),
            "external subject reference",
            maximum=160,
        )
        if "external_subject_ref" in payload
        else None,
        "observation_id": observation_id,
        "suggestion_key": suggestion_key,
        "association_type": association_type,
        "model_suggested_target_ref": model_target_ref,
        "selected_candidate_type": candidate_type,
        "selected_candidate_ref": candidate_ref,
        "evidence_refs": _evidence_refs(payload["evidence_refs"]),
        "policy_version": _text(payload["policy_version"], "policy version", maximum=140),
        "idempotency_key": _idempotency_key(payload["idempotency_key"]),
        "request_id": _text(payload["request_id"], "request ID", maximum=256),
    }


def _closed_object(
    request: Mapping[str, Any],
    fields: frozenset[str],
) -> dict[str, Any]:
    if not isinstance(request, Mapping) or set(request) != fields:
        raise IdentityReviewError("identity review request fields are invalid")
    return dict(request)


def _text(value: object, fieldname: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise IdentityReviewError(f"{fieldname} is invalid")
    normalized = value.strip()
    if not normalized or normalized != value or len(normalized) > maximum:
        raise IdentityReviewError(f"{fieldname} is invalid")
    return normalized


def _idempotency_key(value: object) -> str:
    key = _text(value, "idempotency key", maximum=256)
    if len(key) < 8:
        raise IdentityReviewError("idempotency key is invalid")
    return key


def _evidence_refs(value: object) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > 100:
        raise IdentityReviewError("evidence references are invalid")
    refs = [_text(item, "evidence reference", maximum=500) for item in value]
    if len(refs) != len(set(refs)):
        raise IdentityReviewError("evidence references are invalid")
    return refs


def _require_actor_team(team: str) -> None:
    actor = str(frappe.session.user)
    roles = set(frappe.get_roles(actor))
    if "GBOS Admin" in roles:
        return
    if not frappe.db.exists(
        "GBOS Team Member",
        {"parent": team, "user": actor, "enabled": 1},
    ):
        raise frappe.PermissionError


def _resolve_candidate(*, team: str, candidate_type: str, candidate_ref: str) -> dict[str, Any]:
    if candidate_type == "User":
        if int(
            frappe.db.get_value("User", candidate_ref, "enabled") or 0
        ) != 1 or not frappe.db.exists(
            "GBOS Team Member",
            {"parent": team, "user": candidate_ref, "enabled": 1},
        ):
            raise IdentityReviewError("identity candidate is not eligible")
        return {"identity_type": "User", "user": candidate_ref, "party_profile": None}
    if candidate_type == "Party":
        if frappe.db.get_value("GBOS Party Profile", candidate_ref, "team") != team:
            raise IdentityReviewError("identity candidate is not eligible")
        return {"identity_type": "Party", "user": None, "party_profile": candidate_ref}
    if candidate_type == "Contact":
        matches = frappe.get_all(
            "GBOS Party Profile",
            filters={"team": team, "contact": candidate_ref},
            fields=["name"],
            limit_page_length=2,
        )
        if len(matches) != 1:
            raise IdentityReviewError("identity candidate is ambiguous or unavailable")
        return {
            "identity_type": "Party",
            "user": None,
            "party_profile": str(matches[0]["name"]),
        }
    raise IdentityReviewError("selected candidate type is not allowed")


def _origin_reference(payload: Mapping[str, Any]) -> str:
    provenance = {
        "team": payload["team"],
        "observation_id": payload["observation_id"],
        "suggestion_key": payload["suggestion_key"],
        "association_type": payload["association_type"],
        "model_suggested_target_ref": payload["model_suggested_target_ref"],
        "selected_candidate_type": payload["selected_candidate_type"],
        "selected_candidate_ref": payload["selected_candidate_ref"],
        "evidence_refs": payload["evidence_refs"],
        "policy_version": payload["policy_version"],
    }
    return f"association:v1:{canonical_payload_hash(provenance)}"


def _locked_mapping(name: str) -> Any:
    try:
        return frappe.get_doc("GBOS External Identity", name, for_update=True)
    except Exception:
        raise IdentityReviewError("identity draft is unavailable") from None


def _validate_draft_binding(mapping: Any, payload: Mapping[str, Any]) -> None:
    if (
        mapping.get("origin") != "AI"
        or mapping.get("review_status") != "AI Draft"
        or mapping.get("team") != payload["team"]
        or int(mapping.get("revision") or 0) != payload["expected_revision"]
        or mapping.get("origin_reference") != _origin_reference(payload)
    ):
        raise IdentityReviewError("identity draft is stale or already submitted")
    try:
        validate_external_subject(mapping.get("identity_provider"), mapping.get("external_subject"))
    except ValueError:
        raise IdentityReviewError("identity draft is invalid") from None
    target = _resolve_candidate(
        team=str(payload["team"]),
        candidate_type=str(payload["selected_candidate_type"]),
        candidate_ref=str(payload["selected_candidate_ref"]),
    )
    if (
        mapping.get("identity_type") != target["identity_type"]
        or mapping.get("user") != target["user"]
        or mapping.get("party_profile") != target["party_profile"]
    ):
        raise IdentityReviewError("identity draft target is stale")


def _validate_reviewer(*, team: str, reviewer: str) -> None:
    if (
        int(frappe.db.get_value("User", reviewer, "enabled") or 0) != 1
        or "Reviewer" not in set(frappe.get_roles(reviewer))
        or not frappe.db.exists(
            "GBOS Team Member",
            {"parent": team, "user": reviewer, "enabled": 1},
        )
    ):
        raise IdentityReviewError("assigned reviewer is not eligible")


def _mapping_receipt(mapping: Any, request_id: str) -> dict[str, Any]:
    return {
        "doctype": "GBOS External Identity",
        "name": mapping.name,
        "review_status": mapping.review_status,
        "revision": int(mapping.revision),
        "request_id": request_id,
    }


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


__all__ = [
    "IdentityReviewError",
    "materialize_association_suggestion",
    "submit_for_review",
]
