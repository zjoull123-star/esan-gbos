"""Governed materialization of model association suggestions for human review.

This module is deliberately a domain service rather than a whitelisted endpoint.  Callers
must pass a closed request object; model-supplied target references are retained only as
hashed provenance, while separately selected candidates receive exact Frappe validation.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

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
_REMATERIALIZE_FIELDS = frozenset(
    {
        *_MATERIALIZE_FIELDS,
        "name",
        "expected_revision",
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
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_MAPPING_REF = re.compile(r"^EID-([0-9A-HJKMNP-TV-Z]{26})$")
_EMAIL_EVIDENCE_REF = re.compile(r"^EVR-[0-9A-HJKMNP-TV-Z]{26}$")
_EMAIL_ATTESTATION_REF = re.compile(r"^EMA-[0-9A-HJKMNP-TV-Z]{26}$")
_HUMAN_SUBMIT_FIELDS = frozenset(
    {
        "team",
        "address_ref",
        "target_type",
        "target_ref",
        "purpose",
        "evidence_ref",
        "expected_revision",
        "idempotency_key",
        "request_id",
    }
)
_HUMAN_APPROVE_FIELDS = frozenset(
    {
        "review_case_ref",
        "expected_review_case_revision",
        "expected_mapping_revision",
        "purpose",
        "evidence_ref",
        "idempotency_key",
        "request_id",
    }
)
_HUMAN_PURPOSES = {
    "employee_mapping": "User",
    "customer_mapping": "Party",
}
_ADDRESS_MATCH_PURPOSE = "email_address_identity_confirmation"
_ADDRESS_MATCH_CALLER_REF = "frappe-identity-command"


class EmailAddressMatchAuthorityClient(Protocol):
    """Request-scoped boundary; Task 5 supplies the local HTTP implementation."""

    def attest(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...


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
    return result


def rematerialize_rejected_association_suggestion(
    request: Mapping[str, Any],
) -> dict[str, Any]:
    """Correct one rejected AI mapping in place without rewriting review history."""
    payload = _rematerialize_payload(request)
    _require_actor_team(str(payload["team"]))

    def execute() -> dict[str, Any]:
        mapping = _locked_mapping(str(payload["name"]))
        if (
            mapping.get("origin") != "AI"
            or mapping.get("review_status") != "Rejected"
            or mapping.get("business_status") != "Active"
            or mapping.get("team") != payload["team"]
            or mapping.get("identity_provider") != payload["identity_provider"]
            or mapping.get("external_subject") != payload["external_subject_ref"]
            or int(mapping.get("revision") or 0) != payload["expected_revision"]
        ):
            raise IdentityReviewError("rejected identity mapping is stale or unavailable")
        try:
            validate_external_subject(
                mapping.get("identity_provider"), mapping.get("external_subject")
            )
        except ValueError:
            raise IdentityReviewError("rejected identity mapping is invalid") from None
        target = _resolve_candidate(
            team=str(payload["team"]),
            candidate_type=str(payload["selected_candidate_type"]),
            candidate_ref=str(payload["selected_candidate_ref"]),
        )
        mapping.flags.gbos_ai_reopen_command = True
        mapping.identity_type = target["identity_type"]
        mapping.user = target["user"]
        mapping.party_profile = target["party_profile"]
        mapping.origin_reference = _origin_reference(payload)
        mapping.review_status = "AI Draft"
        mapping.last_request_id = payload["request_id"]
        mapping.save(ignore_permissions=True)
        return _mapping_receipt(mapping, str(payload["request_id"]))

    result, _replayed, _original_request_id = run_idempotent(
        "identity_review.rematerialize",
        str(payload["idempotency_key"]),
        payload,
        execute,
        api_version="domain",
    )
    return result


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
        pending_case = frappe.db.exists(
            "GBOS Review Case",
            {
                "subject_doctype": "GBOS External Identity",
                "subject_name": mapping.name,
                "business_status": "Pending",
                "review_status": "Pending",
            },
        )
        same_revision_case = frappe.db.exists(
            "GBOS Review Case",
            {
                "subject_doctype": "GBOS External Identity",
                "subject_name": mapping.name,
                "subject_revision": int(mapping.revision),
            },
        )
        if pending_case or same_revision_case:
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
    return result


def submit_human_identity_for_review(request: Mapping[str, Any]) -> dict[str, Any]:
    """Create a purpose-bound pending mapping without requiring an AI suggestion."""
    payload = _human_submit_payload(request)
    _authorize_human_identity_actor(
        team=payload["team"], purpose=payload["purpose"], target_type=payload["target_type"]
    )

    def execute() -> dict[str, Any]:
        target = _resolve_candidate(
            team=payload["team"],
            candidate_type=payload["target_type"],
            candidate_ref=payload["target_ref"],
        )
        if frappe.db.exists(
            "GBOS External Identity",
            {"identity_provider": "email", "external_subject": payload["address_ref"]},
        ):
            raise IdentityReviewError("identity candidate already exists")
        provenance = _human_provenance(payload)
        mapping = frappe.get_doc(
            {
                "doctype": "GBOS External Identity",
                "team": payload["team"],
                "identity_provider": "email",
                "external_subject": payload["address_ref"],
                "identity_type": target["identity_type"],
                "user": target["user"],
                "party_profile": target["party_profile"],
                "origin": "Manual",
                "origin_reference": provenance,
                "business_status": "Active",
                "review_status": "Pending",
                "last_request_id": payload["request_id"],
            }
        )
        mapping.flags.gbos_human_identity_command = True
        try:
            mapping.insert(ignore_permissions=True)
        except frappe.DuplicateEntryError:
            raise IdentityReviewError("identity candidate already exists") from None
        attestation_ref, attestation = _request_human_attestation(
            mapping=mapping,
            evidence_ref=payload["evidence_ref"],
            request_id=payload["request_id"],
        )
        _validate_human_attestation(attestation, mapping, payload["evidence_ref"])
        attestation_digest = str(attestation["digest"])
        snapshot = build_subject_snapshot(mapping)
        evidence_refs = [
            payload["evidence_ref"],
            attestation_ref,
            attestation_digest,
        ]
        case_values: dict[str, Any] = {
            "doctype": "GBOS Review Case",
            "title": "Human email identity review",
            "team": payload["team"],
            "assigned_reviewer": str(frappe.session.user),
            "subject_doctype": "GBOS External Identity",
            "subject_name": mapping.name,
            "subject_revision": int(mapping.revision),
            "subject_payload_sha256": canonical_payload_hash(snapshot),
            "subject_snapshot": _json(snapshot),
            "case_payload_sha256": "",
            "evidence_refs": _json(evidence_refs),
            "policy_version": payload["purpose"],
            "origin": "Manual",
            "origin_reference": provenance,
            "business_status": "Pending",
            "review_status": "Pending",
            "last_request_id": payload["request_id"],
        }
        review_case = frappe.get_doc(case_values)
        review_case.case_payload_sha256 = canonical_payload_hash(build_case_payload(review_case))
        review_case.flags.gbos_human_identity_command = True
        review_case.insert(ignore_permissions=True)
        return {
            "mapping_ref": mapping.name,
            "mapping_revision": int(mapping.revision),
            "review_case_ref": review_case.name,
            "review_case_revision": int(review_case.revision),
            "request_id": payload["request_id"],
        }

    result, _replayed, _original_request_id = run_idempotent(
        "identity_review.human_submit",
        payload["idempotency_key"],
        payload,
        execute,
        api_version="domain",
    )
    return result


def approve_human_identity_review(request: Mapping[str, Any]) -> dict[str, Any]:
    """Approve a human mapping only with a current exact Observer attestation."""
    payload = _human_approve_payload(request)

    def execute() -> dict[str, Any]:
        try:
            case = frappe.get_doc("GBOS Review Case", payload["review_case_ref"], for_update=True)
            mapping = frappe.get_doc("GBOS External Identity", case.subject_name, for_update=True)
        except Exception:
            raise IdentityReviewError("human identity review is unavailable") from None
        _authorize_human_identity_actor(
            team=str(case.team),
            purpose=payload["purpose"],
            target_type=str(mapping.identity_type),
        )
        expected_evidence = [
            payload["evidence_ref"],
        ]
        stored_evidence = json.loads(case.evidence_refs)
        if (
            case.subject_doctype != "GBOS External Identity"
            or case.origin != "Manual"
            or not str(case.origin_reference).startswith("identity-human:v1:")
            or case.policy_version != payload["purpose"]
            or case.business_status != "Pending"
            or case.review_status != "Pending"
            or int(case.revision) != payload["expected_review_case_revision"]
            or int(case.subject_revision) != payload["expected_mapping_revision"]
            or int(mapping.revision) != payload["expected_mapping_revision"]
            or mapping.review_status != "Pending"
            or mapping.origin != "Manual"
            or mapping.origin_reference != case.origin_reference
            or not _stored_attestation_envelope(stored_evidence, expected_evidence)
        ):
            raise IdentityReviewError("human identity review is stale or unavailable")
        _resolve_candidate(
            team=str(mapping.team),
            candidate_type=str(mapping.identity_type),
            candidate_ref=str(mapping.user or mapping.party_profile),
        )
        _attestation_ref, current_attestation = _request_human_attestation(
            mapping=mapping,
            evidence_ref=payload["evidence_ref"],
            request_id=payload["request_id"],
        )
        _validate_human_attestation(current_attestation, mapping, payload["evidence_ref"])
        case.flags.gbos_review_command = True
        case.flags.gbos_human_identity_command = True
        case.flags.gbos_human_identity_approval = True
        case.business_status = "Approved"
        case.review_status = "Approved"
        case.decided_by = str(frappe.session.user)
        case.last_request_id = payload["request_id"]
        case.save(ignore_permissions=True)
        mapping = frappe.get_doc("GBOS External Identity", case.subject_name, for_update=True)
        if mapping.review_status == "Pending":
            mapping.flags.gbos_identity_review_decision = True
            mapping.review_status = "Approved"
            mapping.business_status = "Active"
            mapping.last_request_id = payload["request_id"]
            mapping.save(ignore_permissions=True)
        return {
            "status": "approved",
            "mapping_ref": mapping.name,
            "mapping_revision": int(mapping.revision),
            "review_case_ref": case.name,
            "review_case_revision": int(case.revision),
            "request_id": payload["request_id"],
        }

    result, _replayed, _original_request_id = run_idempotent(
        "identity_review.human_approve",
        payload["idempotency_key"],
        payload,
        execute,
        api_version="domain",
    )
    return result


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


def _rematerialize_payload(request: Mapping[str, Any]) -> dict[str, Any]:
    payload = _closed_object(request, _REMATERIALIZE_FIELDS)
    normalized = _common_proposal_fields(payload)
    normalized["name"] = _text(payload["name"], "mapping name", maximum=140)
    normalized["identity_provider"] = _text(
        payload["identity_provider"], "identity provider", maximum=32
    )
    try:
        validate_external_subject(
            normalized["identity_provider"], normalized["external_subject_ref"]
        )
    except ValueError as error:
        raise IdentityReviewError(str(error)) from None
    revision = payload["expected_revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise IdentityReviewError("expected revision must be a positive integer")
    normalized["expected_revision"] = revision
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


def _human_submit_payload(request: Mapping[str, Any]) -> dict[str, Any]:
    payload = _closed_object(request, _HUMAN_SUBMIT_FIELDS)
    purpose = _human_purpose(payload["purpose"])
    target_type = _text(payload["target_type"], "target type", maximum=16)
    if _HUMAN_PURPOSES[purpose] != target_type:
        raise IdentityReviewError("identity purpose and target type do not match")
    address_ref = _text(payload["address_ref"], "address reference", maximum=160)
    try:
        validate_external_subject("email", address_ref)
    except ValueError as error:
        raise IdentityReviewError(str(error)) from None
    expected_revision = payload["expected_revision"]
    if isinstance(expected_revision, bool) or expected_revision != 0:
        raise IdentityReviewError("expected revision must be zero for a new mapping")
    evidence_ref = _safe_reference(payload["evidence_ref"], "evidence reference")
    if _EMAIL_EVIDENCE_REF.fullmatch(evidence_ref) is None:
        raise IdentityReviewError("evidence reference is invalid")
    return {
        "team": _text(payload["team"], "team", maximum=140),
        "address_ref": address_ref,
        "target_type": target_type,
        "target_ref": _safe_reference(payload["target_ref"], "target reference"),
        "purpose": purpose,
        "evidence_ref": evidence_ref,
        "expected_revision": 0,
        "idempotency_key": _idempotency_key(payload["idempotency_key"]),
        "request_id": _text(payload["request_id"], "request ID", maximum=256),
    }


def _human_approve_payload(request: Mapping[str, Any]) -> dict[str, Any]:
    payload = _closed_object(request, _HUMAN_APPROVE_FIELDS)
    evidence_ref = _safe_reference(payload["evidence_ref"], "evidence reference")
    if _EMAIL_EVIDENCE_REF.fullmatch(evidence_ref) is None:
        raise IdentityReviewError("evidence reference is invalid")
    result = {
        "review_case_ref": _safe_reference(payload["review_case_ref"], "review case"),
        "expected_review_case_revision": _revision(payload["expected_review_case_revision"]),
        "expected_mapping_revision": _revision(payload["expected_mapping_revision"]),
        "purpose": _human_purpose(payload["purpose"]),
        "evidence_ref": evidence_ref,
        "idempotency_key": _idempotency_key(payload["idempotency_key"]),
        "request_id": _text(payload["request_id"], "request ID", maximum=256),
    }
    return result


def _human_purpose(value: object) -> str:
    purpose = _text(value, "purpose", maximum=32)
    if purpose not in _HUMAN_PURPOSES:
        raise IdentityReviewError("identity purpose is not allowed")
    return purpose


def _revision(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise IdentityReviewError("expected revision must be a positive integer")
    return value


def _safe_reference(value: object, fieldname: str) -> str:
    reference = _text(value, fieldname, maximum=256)
    if _SAFE_REF.fullmatch(reference) is None:
        raise IdentityReviewError(f"{fieldname} is invalid")
    return reference


def _digest(value: object) -> str:
    digest = _text(value, "digest", maximum=71)
    if _DIGEST.fullmatch(digest) is None:
        raise IdentityReviewError("digest is invalid")
    return digest


def _authorize_human_identity_actor(*, team: str, purpose: str, target_type: str) -> None:
    actor = str(frappe.session.user)
    roles = set(frappe.get_roles(actor))
    if purpose == "employee_mapping" and target_type == "User":
        if roles & {"GBOS Admin", "Integration Admin"}:
            return
        raise frappe.PermissionError
    if purpose == "customer_mapping" and target_type == "Party":
        if roles & {"Sales Manager", "Reviewer"} and frappe.db.exists(
            "GBOS Team Member", {"parent": team, "user": actor, "enabled": 1}
        ):
            return
        raise frappe.PermissionError
    raise frappe.PermissionError


def _human_provenance(payload: Mapping[str, Any]) -> str:
    body = {
        "team": payload["team"],
        "address_ref": payload["address_ref"],
        "target_type": payload["target_type"],
        "target_ref": payload["target_ref"],
        "purpose": payload["purpose"],
        "evidence_ref": payload["evidence_ref"],
        "expected_revision": payload["expected_revision"],
        "request_id": payload["request_id"],
        "idempotency_key": payload["idempotency_key"],
    }
    return f"identity-human:v1:{canonical_payload_hash(body)}"


def _validate_human_attestation(
    attestation: Mapping[str, Any], mapping: Any, evidence_ref: str
) -> None:
    if not isinstance(attestation, Mapping):
        raise IdentityReviewError("address-match attestation is invalid")
    expected_fields = {
        "opaque_address_ref",
        "candidate_target_ref",
        "candidate_target_type",
        "evidence_ref",
        "normalization_version",
        "matched",
        "observed_at",
        "expires_at",
        "digest",
    }
    if (
        set(attestation) != expected_fields
        or attestation.get("opaque_address_ref") != mapping.external_subject
        or attestation.get("candidate_target_ref") != _candidate_target_ref(mapping)
        or attestation.get("candidate_target_type") != mapping.identity_type
        or attestation.get("evidence_ref") != evidence_ref
        or attestation.get("normalization_version") != "email-address-v1"
        or attestation.get("matched") is not True
        or _DIGEST.fullmatch(str(attestation.get("digest") or "")) is None
    ):
        raise IdentityReviewError("address-match attestation is invalid")
    try:
        observed = datetime.fromisoformat(str(attestation["observed_at"]).replace("Z", "+00:00"))
        expires = datetime.fromisoformat(str(attestation["expires_at"]).replace("Z", "+00:00"))
    except ValueError:
        raise IdentityReviewError("address-match attestation is invalid") from None
    now = datetime.now(UTC)
    target_modified = _current_target_modified(mapping)
    if (
        observed.tzinfo is None
        or expires.tzinfo is None
        or observed > now
        or observed < target_modified
        or expires <= observed
        or expires <= now
        or (expires - observed).total_seconds() > 900
    ):
        raise IdentityReviewError("address-match attestation is expired or invalid")


def _current_target_modified(mapping: Any) -> datetime:
    if mapping.identity_type == "User":
        modified = frappe.db.get_value("User", mapping.user, "modified")
        if not modified:
            raise IdentityReviewError("address-match attestation target is unavailable")
    elif mapping.identity_type == "Party":
        contact = frappe.db.get_value("GBOS Party Profile", mapping.party_profile, "contact")
        modified = frappe.db.get_value("Contact", contact, "modified") if contact else None
        if not contact or not modified:
            raise IdentityReviewError("address-match attestation target is unavailable")
    else:
        raise IdentityReviewError("address-match attestation target is unavailable")
    try:
        parsed = datetime.fromisoformat(str(modified).replace("Z", "+00:00"))
    except ValueError:
        raise IdentityReviewError("address-match attestation target is unavailable") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _candidate_target_ref(mapping: Any) -> str:
    match = _MAPPING_REF.fullmatch(str(mapping.name))
    target_type = str(mapping.identity_type)
    if match is None or target_type not in {"User", "Party"}:
        raise IdentityReviewError("address-match attestation target is unavailable")
    prefix = "USR" if target_type == "User" else "PTY"
    return f"{prefix}-{match.group(1)}"


def _stored_attestation_envelope(stored: object, prefix: list[str]) -> bool:
    return bool(
        isinstance(stored, list)
        and len(stored) == 3
        and stored[:1] == prefix
        and isinstance(stored[1], str)
        and _EMAIL_ATTESTATION_REF.fullmatch(stored[1]) is not None
        and isinstance(stored[2], str)
        and _DIGEST.fullmatch(stored[2]) is not None
    )


def _request_human_attestation(
    *, mapping: Any, evidence_ref: str, request_id: str
) -> tuple[str, Mapping[str, Any]]:
    client = getattr(frappe.local, "gbos_email_address_match_authority_client", None)
    if client is None:
        try:
            from esan_gbos.api.internal.email_address_match_authority_client import (
                inject_email_address_match_authority_client,
            )

            client = inject_email_address_match_authority_client()
        except Exception:
            raise IdentityReviewError("address-match authority is unavailable") from None
    attest = getattr(client, "attest", None)
    site_id = str(getattr(frappe.local, "site", "") or "").strip()
    if not site_id or not callable(attest):
        raise IdentityReviewError("address-match authority is unavailable")
    request = {
        "request_id": request_id,
        "site_id": site_id,
        "processing_purpose": _ADDRESS_MATCH_PURPOSE,
        "caller_ref": _ADDRESS_MATCH_CALLER_REF,
        "evidence_ref": evidence_ref,
        "address_role": "from",
        "role_index": 0,
        "opaque_address_ref": str(mapping.external_subject),
        "candidate_target_ref": _candidate_target_ref(mapping),
        "candidate_target_type": str(mapping.identity_type),
        "candidate_address": _current_target_address(mapping),
    }
    try:
        response = attest(request)
    except Exception:
        raise IdentityReviewError("address-match authority is unavailable") from None
    if not isinstance(response, Mapping) or set(response) != {"attestation_ref", "attestation"}:
        raise IdentityReviewError("address-match authority returned an invalid response")
    attestation_ref = _safe_reference(response["attestation_ref"], "attestation reference")
    if _EMAIL_ATTESTATION_REF.fullmatch(attestation_ref) is None:
        raise IdentityReviewError("address-match authority returned an invalid response")
    attestation = response["attestation"]
    if not isinstance(attestation, Mapping):
        raise IdentityReviewError("address-match authority returned an invalid response")
    return attestation_ref, attestation


def _current_target_address(mapping: Any) -> str:
    if mapping.identity_type == "User":
        address = frappe.db.get_value("User", mapping.user, "email")
    elif mapping.identity_type == "Party":
        contact = frappe.db.get_value("GBOS Party Profile", mapping.party_profile, "contact")
        address = frappe.db.get_value("Contact", contact, "email_id") if contact else None
    else:
        address = None
    if not isinstance(address, str) or not address:
        raise IdentityReviewError("address-match attestation target is unavailable")
    return address


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
    "approve_human_identity_review",
    "materialize_association_suggestion",
    "rematerialize_rejected_association_suggestion",
    "submit_human_identity_for_review",
    "submit_for_review",
]
