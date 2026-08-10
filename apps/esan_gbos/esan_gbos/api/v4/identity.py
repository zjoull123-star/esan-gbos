"""Permission-safe v4 identity resolution reads and governed commands."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

import frappe

from esan_gbos.api.v1.audit import run_idempotent
from esan_gbos.api.v1.common import BFFError, bff_endpoint, request_id, require_roles
from esan_gbos.api.v4.communication import _scope
from esan_gbos.api.v4.gateway import call_local, v4_success
from esan_gbos.domain.identity_review import (
    IdentityReviewError,
    materialize_association_suggestion,
    rematerialize_rejected_association_suggestion,
)
from esan_gbos.domain.identity_review import (
    submit_for_review as submit_identity_draft,
)
from esan_gbos.domain.v4_dto import V4DTOValidationError, map_communication_detail

IDENTITY_READ_ROLES = frozenset(
    {"Sales User", "Sales Manager", "Integration Admin", "GBOS Admin", "CEO"}
)
IDENTITY_SUBMIT_ROLES = frozenset(
    {"Sales User", "Sales Manager", "Integration Admin", "GBOS Admin", "CEO"}
)
IDENTITY_REVOKE_ROLES = frozenset({"Integration Admin", "GBOS Admin"})
IDENTITY_REVIEW_ROLES = frozenset({"Reviewer", "GBOS Admin"})

_POLICY_VERSION = "identity-resolution-v1"
_CANDIDATE_TYPES = frozenset({"User", "Party", "Contact"})
_ADMIN_CANDIDATE_ROLES = frozenset({"Integration Admin", "GBOS Admin", "CEO"})
_SALES_CANDIDATE_TYPES = frozenset({"Party", "Contact"})
_MAPPING_FIELDS = [
    "name",
    "team",
    "identity_provider",
    "external_subject",
    "identity_type",
    "user",
    "party_profile",
    "business_status",
    "review_status",
    "revision",
]
_CASE_FIELDS = [
    "name",
    "title",
    "team",
    "assigned_reviewer",
    "subject_doctype",
    "subject_name",
    "subject_revision",
    "evidence_refs",
    "policy_version",
    "business_status",
    "review_status",
    "revision",
    "modified",
]


def _value(source: object, field: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(field, default)
    return getattr(source, field, default)


def _text(value: object, field: str, *, maximum: int = 256, query: bool = False) -> str:
    if not isinstance(value, str):
        raise BFFError("invalid_query" if query else "invalid_dto", f"{field} is invalid")
    normalized = value.strip()
    if not normalized or normalized != value or len(normalized) > maximum or "\x00" in value:
        raise BFFError("invalid_query" if query else "invalid_dto", f"{field} is invalid")
    return normalized


def _integer(
    value: int | str,
    field: str,
    *,
    minimum: int,
    maximum: int | None = None,
    query: bool = False,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise BFFError(
            "invalid_query" if query else "invalid_dto", f"{field} must be an integer"
        ) from error
    if isinstance(value, bool) or parsed < minimum or (maximum is not None and parsed > maximum):
        raise BFFError(
            "invalid_query" if query else "invalid_dto", f"{field} is outside the allowed range"
        )
    return parsed


def _idempotency_key(value: object) -> str:
    key = _text(value, "idempotency_key")
    if len(key) < 8:
        raise BFFError("invalid_dto", "idempotency_key is invalid")
    return key


def _fetch_communication(observation_id: object) -> dict[str, Any]:
    observation = _text(observation_id, "observation_id", maximum=48, query=True)
    data = call_local(
        "Observer",
        method="POST",
        path="/internal/v1/bff/communications/get",
        purpose="communication_projection",
        payload={**_scope(), "observation_id": observation},
    )
    value = data.get("communication")
    if not isinstance(value, dict):
        raise BFFError("not_found", "Communication was not found", status=404)
    try:
        map_communication_detail(value)
    except V4DTOValidationError as error:
        raise BFFError(
            "internal_error", "Observer communication detail is invalid", status=503
        ) from error
    team = value.get("team_ref")
    if not isinstance(team, str) or not team.strip():
        raise BFFError("scope_mismatch", "Communication team scope is unavailable", status=403)
    return value


def _participant(communication: Mapping[str, Any], identity_ref: object) -> dict[str, Any]:
    requested = _text(identity_ref, "identity_ref", maximum=160, query=True)
    matches = [
        item
        for item in communication.get("participant_identities", [])
        if isinstance(item, dict) and item.get("identity_ref") == requested
    ]
    if len(matches) != 1:
        raise BFFError(
            "identity_mismatch",
            "Participant identity is unavailable for this communication",
            status=409,
        )
    return matches[0]


def _mapping_rows(identity_ref: str) -> list[dict[str, Any]]:
    rows = frappe.get_all(
        "GBOS External Identity",
        filters={"external_subject": identity_ref},
        fields=_MAPPING_FIELDS,
        limit_page_length=3,
    )
    if len(rows) > 1:
        raise BFFError("internal_error", "Identity mapping is ambiguous", status=503)
    return [dict(row) for row in rows]


def _mapping_state(mapping: object) -> str:
    review = str(_value(mapping, "review_status") or "")
    business = str(_value(mapping, "business_status") or "")
    if review == "AI Draft" and business == "Active":
        return "proposed"
    if review == "Pending" and business == "Active":
        return "pending"
    if review == "Approved" and business == "Active":
        return "confirmed"
    if review == "Rejected" and business == "Active":
        return "rejected"
    if business in {"Revoked", "Archived"} or review == "Superseded":
        return "revoked"
    raise BFFError("internal_error", "Identity mapping state is invalid", status=503)


def _same_team_user(user_ref: str, team: str) -> dict[str, str] | None:
    membership = frappe.get_all(
        "GBOS Team Member",
        filters={"parent": team, "user": user_ref, "enabled": 1},
        fields=["user"],
        limit_page_length=2,
    )
    rows = frappe.get_all(
        "User",
        filters={"name": user_ref, "enabled": 1},
        fields=["name", "full_name"],
        limit_page_length=2,
    )
    if len(membership) != 1 or len(rows) != 1:
        return None
    label = str(rows[0].get("full_name") or "").strip() or "User"
    return {"candidate_type": "User", "candidate_ref": user_ref, "display_label": label}


def _same_team_party(party_ref: str, team: str) -> dict[str, str] | None:
    rows = frappe.get_all(
        "GBOS Party Profile",
        filters={"name": party_ref, "team": team},
        fields=["name", "party_name"],
        limit_page_length=2,
    )
    if len(rows) != 1:
        return None
    label = str(rows[0].get("party_name") or "").strip() or "Party"
    return {"candidate_type": "Party", "candidate_ref": party_ref, "display_label": label}


def _same_team_contact(contact_ref: str, team: str) -> dict[str, str] | None:
    parties = frappe.get_all(
        "GBOS Party Profile",
        filters={"team": team, "contact": contact_ref},
        fields=["name", "party_name", "contact"],
        limit_page_length=2,
    )
    contacts = frappe.get_all(
        "Contact",
        filters={"name": contact_ref},
        fields=["name", "full_name"],
        limit_page_length=2,
    )
    if len(parties) != 1 or len(contacts) != 1:
        return None
    label = (
        str(contacts[0].get("full_name") or "").strip()
        or str(parties[0].get("party_name") or "").strip()
        or "Contact"
    )
    return {"candidate_type": "Contact", "candidate_ref": contact_ref, "display_label": label}


def _candidate(candidate_type: str, candidate_ref: str, team: str) -> dict[str, str] | None:
    if candidate_type == "User":
        return _same_team_user(candidate_ref, team)
    if candidate_type == "Party":
        return _same_team_party(candidate_ref, team)
    if candidate_type == "Contact":
        return _same_team_contact(candidate_ref, team)
    return None


def _require_candidate_type_for_actor(candidate_type: str, *, query: bool) -> None:
    if candidate_type not in _CANDIDATE_TYPES:
        raise BFFError(
            "invalid_query" if query else "invalid_dto",
            "candidate_type is not allowed",
        )
    roles = set(frappe.get_roles())
    if roles & _ADMIN_CANDIDATE_ROLES:
        return
    if candidate_type in _SALES_CANDIDATE_TYPES:
        return
    raise BFFError(
        "candidate_type_forbidden",
        "Candidate type is not permitted for the current role",
        status=403,
    )


def _mapping_target(mapping: object, team: str) -> dict[str, str]:
    if str(_value(mapping, "team") or "") != team:
        raise BFFError(
            "scope_mismatch",
            "Identity mapping is outside the communication team",
            status=403,
        )
    identity_type = str(_value(mapping, "identity_type") or "")
    if identity_type == "User":
        target = _same_team_user(str(_value(mapping, "user") or ""), team)
    elif identity_type == "Party":
        target = _same_team_party(str(_value(mapping, "party_profile") or ""), team)
    else:
        target = None
    if target is None:
        raise BFFError("internal_error", "Identity mapping target is unavailable", status=503)
    return target


def _identity_state(
    communication: Mapping[str, Any], participant: Mapping[str, Any]
) -> dict[str, Any]:
    identity_ref = str(participant["identity_ref"])
    provider = str(participant["provider"])
    team = str(communication["team_ref"])
    rows = _mapping_rows(identity_ref)
    if not rows:
        if participant.get("mapping_ref") is not None:
            raise BFFError("internal_error", "Identity mapping is unavailable", status=503)
        return {"identity_ref": identity_ref, "provider": provider, "status": "unresolved"}
    mapping = rows[0]
    if (
        str(mapping.get("team") or "") != team
        or str(mapping.get("identity_provider") or "") != provider
        or str(mapping.get("external_subject") or "") != identity_ref
    ):
        raise BFFError("scope_mismatch", "Identity mapping scope does not match", status=403)
    observer_mapping_ref = participant.get("mapping_ref")
    if observer_mapping_ref is not None and observer_mapping_ref != mapping.get("name"):
        raise BFFError("internal_error", "Identity projection does not match authority", status=503)
    state = _mapping_state(mapping)
    if state == "rejected":
        if observer_mapping_ref is not None:
            raise BFFError(
                "internal_error",
                "Rejected identity mapping remains resolved by Observer",
                status=503,
            )
        return {
            "identity_ref": identity_ref,
            "provider": provider,
            "status": state,
            "mapping_ref": str(mapping["name"]),
            "mapping_revision": int(mapping["revision"]),
        }
    target = _mapping_target(mapping, team)
    return {
        "identity_ref": identity_ref,
        "provider": provider,
        "status": state,
        "mapping_ref": str(mapping["name"]),
        "mapping_revision": int(mapping["revision"]),
        "target_type": str(mapping["identity_type"]),
        "display_label": target["display_label"],
    }


def _connector_owner(communication: Mapping[str, Any]) -> dict[str, str] | None:
    owner = communication.get("connector_account_user_ref")
    if owner is None:
        return None
    candidate = _same_team_user(str(owner), str(communication["team_ref"]))
    if candidate is None:
        raise BFFError("internal_error", "Connector account owner is unavailable", status=503)
    return {"display_label": candidate["display_label"]}


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@bff_endpoint("GET")
def list_states(observation_id: str) -> dict[str, Any]:
    require_roles(IDENTITY_READ_ROLES)
    communication = _fetch_communication(observation_id)
    identities = [
        _identity_state(communication, participant)
        for participant in communication["participant_identities"]
    ]
    return v4_success(
        {"identities": identities, "connector_account_owner": _connector_owner(communication)}
    )


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@bff_endpoint("GET")
def get_state(observation_id: str, identity_ref: str) -> dict[str, Any]:
    require_roles(IDENTITY_READ_ROLES)
    communication = _fetch_communication(observation_id)
    participant = _participant(communication, identity_ref)
    return v4_success(
        {
            "identity": _identity_state(communication, participant),
            "connector_account_owner": _connector_owner(communication),
        }
    )


def _candidate_rows(candidate_type: str, team: str) -> list[dict[str, str]]:
    if candidate_type == "User":
        members = frappe.get_all(
            "GBOS Team Member",
            filters={"parent": team, "enabled": 1},
            fields=["user"],
            limit_page_length=500,
        )
        values = [
            candidate
            for member in members
            if (candidate := _same_team_user(str(member.get("user") or ""), team)) is not None
        ]
    elif candidate_type == "Party":
        parties = frappe.get_all(
            "GBOS Party Profile",
            filters={"team": team},
            fields=["name"],
            limit_page_length=500,
        )
        values = [
            candidate
            for party in parties
            if (candidate := _same_team_party(str(party.get("name") or ""), team)) is not None
        ]
    elif candidate_type == "Contact":
        parties = frappe.get_all(
            "GBOS Party Profile",
            filters={"team": team},
            fields=["contact"],
            limit_page_length=500,
        )
        contact_refs = sorted(
            {str(row.get("contact") or "") for row in parties if row.get("contact")}
        )
        values = [
            candidate
            for contact_ref in contact_refs
            if (candidate := _same_team_contact(contact_ref, team)) is not None
        ]
    else:
        raise BFFError("invalid_query", "candidate_type is not allowed")
    return sorted(values, key=lambda row: (row["display_label"].casefold(), row["candidate_ref"]))


def _reviewer_rows(team: str) -> list[dict[str, str]]:
    members = frappe.get_all(
        "GBOS Team Member",
        filters={"parent": team, "enabled": 1},
        fields=["user"],
        limit_page_length=500,
    )
    reviewers: list[dict[str, str]] = []
    for member in members:
        user_ref = str(member.get("user") or "")
        user = _same_team_user(user_ref, team)
        if user is not None and "Reviewer" in set(frappe.get_roles(user_ref)):
            reviewers.append({"reviewer_ref": user_ref, "display_label": user["display_label"]})
    return sorted(
        reviewers,
        key=lambda row: (row["display_label"].casefold(), row["reviewer_ref"]),
    )


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@bff_endpoint("GET")
def list_candidates(
    observation_id: str,
    identity_ref: str,
    candidate_type: str,
    search: str | None = None,
    page: int | str = 1,
    page_size: int | str = 20,
) -> dict[str, Any]:
    require_roles(IDENTITY_SUBMIT_ROLES)
    communication = _fetch_communication(observation_id)
    _participant(communication, identity_ref)
    kind = _text(candidate_type, "candidate_type", maximum=16, query=True)
    _require_candidate_type_for_actor(kind, query=True)
    query = "" if search in (None, "") else _text(search, "search", maximum=100, query=True)
    number = _integer(page, "page", minimum=1, maximum=1000, query=True)
    size = _integer(page_size, "page_size", minimum=1, maximum=50, query=True)
    rows = _candidate_rows(kind, str(communication["team_ref"]))
    if query:
        needle = query.casefold()
        rows = [row for row in rows if needle in row["display_label"].casefold()]
    start = (number - 1) * size
    return v4_success(
        {
            "candidates": rows[start : start + size],
            "eligible_reviewers": _reviewer_rows(str(communication["team_ref"])),
            "has_more": len(rows) > start + size,
        },
        page_size=size,
    )


def _review_filters() -> dict[str, Any]:
    filters: dict[str, Any] = {
        "subject_doctype": "GBOS External Identity",
        "business_status": "Pending",
        "review_status": "Pending",
    }
    if "GBOS Admin" not in set(frappe.get_roles()):
        filters["assigned_reviewer"] = frappe.session.user
    return filters


def _evidence_refs(value: object) -> list[str]:
    try:
        parsed = frappe.parse_json(value) if isinstance(value, str) else value
    except Exception:
        raise BFFError(
            "internal_error", "Identity review evidence is invalid", status=503
        ) from None
    if (
        not isinstance(parsed, list)
        or len(parsed) > 100
        or not all(isinstance(item, str) and 0 < len(item) <= 500 for item in parsed)
    ):
        raise BFFError("internal_error", "Identity review evidence is invalid", status=503)
    return list(parsed)


def _review_dto(case: object) -> dict[str, Any]:
    mapping_name = str(_value(case, "subject_name") or "")
    try:
        mapping = frappe.get_doc("GBOS External Identity", mapping_name)
    except Exception:
        raise BFFError(
            "internal_error", "Identity review mapping is unavailable", status=503
        ) from None
    team = str(_value(case, "team") or "")
    if (
        _value(case, "subject_doctype") != "GBOS External Identity"
        or str(_value(mapping, "team") or "") != team
        or str(_value(mapping, "name") or "") != mapping_name
        or int(_value(mapping, "revision") or 0) != int(_value(case, "subject_revision") or 0)
        or _value(mapping, "review_status") != "Pending"
        or _value(mapping, "business_status") != "Active"
    ):
        raise BFFError("internal_error", "Identity review pin is invalid", status=503)
    return {
        "review_case_ref": str(_value(case, "name")),
        "review_case_revision": int(_value(case, "revision") or 0),
        "status": "pending",
        "assigned_reviewer": str(_value(case, "assigned_reviewer")),
        "team_ref": team,
        "mapping_ref": mapping_name,
        "mapping_revision": int(_value(case, "subject_revision") or 0),
        "target": _mapping_target(mapping, team),
        "evidence_refs": _evidence_refs(_value(case, "evidence_refs")),
        "policy_version": str(_value(case, "policy_version") or ""),
    }


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@bff_endpoint("GET")
def list_pending_reviews(page: int | str = 1, page_size: int | str = 20) -> dict[str, Any]:
    require_roles(IDENTITY_REVIEW_ROLES)
    number = _integer(page, "page", minimum=1, maximum=1000, query=True)
    size = _integer(page_size, "page_size", minimum=1, maximum=50, query=True)
    rows = frappe.get_all(
        "GBOS Review Case",
        filters=_review_filters(),
        fields=_CASE_FIELDS,
        order_by="modified desc, name desc",
        limit_start=(number - 1) * size,
        limit_page_length=size + 1,
    )
    return v4_success(
        {"reviews": [_review_dto(row) for row in rows[:size]], "has_more": len(rows) > size},
        page_size=size,
    )


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@bff_endpoint("GET")
def get_pending_review(review_case_ref: str) -> dict[str, Any]:
    require_roles(IDENTITY_REVIEW_ROLES)
    name = _text(review_case_ref, "review_case_ref", maximum=140, query=True)
    rows = frappe.get_all(
        "GBOS Review Case",
        filters={**_review_filters(), "name": name},
        fields=_CASE_FIELDS,
        limit_page_length=2,
    )
    if len(rows) != 1:
        raise BFFError("not_found", "Identity review was not found", status=404)
    return v4_success({"review": _review_dto(rows[0])})


def _derived_key(outer_key: str, phase: str) -> str:
    digest = hashlib.sha256(f"identity-v4\0{phase}\0{outer_key}".encode()).hexdigest()
    return f"identity-v4-{phase}-{digest}"


def _eligible_reviewer(team: str, reviewer: str) -> bool:
    return any(row["reviewer_ref"] == reviewer for row in _reviewer_rows(team))


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@bff_endpoint("POST")
def submit_for_review(
    observation_id: str,
    identity_ref: str,
    suggestion_key: str,
    selected_candidate_type: str,
    selected_candidate_ref: str,
    assigned_reviewer: str,
    expected_state: str,
    expected_revision: int | str,
    idempotency_key: str,
) -> dict[str, Any]:
    require_roles(IDENTITY_SUBMIT_ROLES)
    payload: dict[str, Any] = {
        "observation_id": _text(observation_id, "observation_id", maximum=48),
        "identity_ref": _text(identity_ref, "identity_ref", maximum=160),
        "suggestion_key": _text(suggestion_key, "suggestion_key", maximum=78),
        "selected_candidate_type": _text(
            selected_candidate_type, "selected_candidate_type", maximum=16
        ),
        "selected_candidate_ref": _text(
            selected_candidate_ref, "selected_candidate_ref", maximum=256
        ),
        "assigned_reviewer": _text(assigned_reviewer, "assigned_reviewer", maximum=140),
        "expected_state": _text(expected_state, "expected_state", maximum=16),
        "expected_revision": _integer(expected_revision, "expected_revision", minimum=0),
        "idempotency_key": _idempotency_key(idempotency_key),
    }
    _require_candidate_type_for_actor(str(payload["selected_candidate_type"]), query=False)

    def execute() -> dict[str, Any]:
        expected_state = str(payload["expected_state"])
        expected_revision_value = int(payload["expected_revision"])
        if not (
            (expected_state == "unresolved" and expected_revision_value == 0)
            or (expected_state == "rejected" and expected_revision_value > 0)
        ):
            raise BFFError("revision_conflict", "Identity state changed", status=409)
        communication = _fetch_communication(payload["observation_id"])
        participant = _participant(communication, payload["identity_ref"])
        current = _identity_state(communication, participant)
        if current["status"] != expected_state or (
            expected_state == "rejected"
            and int(current.get("mapping_revision") or 0) != expected_revision_value
        ):
            raise BFFError("revision_conflict", "Identity state changed", status=409)
        suggestions = [
            item
            for item in communication["association_suggestions"]
            if isinstance(item, dict) and item.get("suggestion_key") == payload["suggestion_key"]
        ]
        if len(suggestions) != 1:
            raise BFFError(
                "suggestion_mismatch", "Association suggestion is unavailable", status=409
            )
        team = str(communication["team_ref"])
        candidate = _candidate(
            str(payload["selected_candidate_type"]),
            str(payload["selected_candidate_ref"]),
            team,
        )
        if candidate is None:
            raise BFFError("candidate_ineligible", "Selected candidate is not eligible", status=422)
        reviewer = str(payload["assigned_reviewer"])
        if not _eligible_reviewer(team, reviewer):
            raise BFFError("reviewer_ineligible", "Assigned reviewer is not eligible", status=422)
        current_request_id = request_id()
        evidence_refs = [str(item["ref"]) for item in communication["evidence"]]
        try:
            proposal = {
                "team": team,
                "identity_provider": participant["provider"],
                "external_subject_ref": participant["identity_ref"],
                "observation_id": communication["observation_id"],
                "suggestion_key": suggestions[0]["suggestion_key"],
                "association_type": suggestions[0]["type"],
                "model_suggested_target_ref": suggestions[0]["target_ref"],
                "selected_candidate_type": candidate["candidate_type"],
                "selected_candidate_ref": candidate["candidate_ref"],
                "evidence_refs": evidence_refs,
                "policy_version": _POLICY_VERSION,
                "request_id": current_request_id,
            }
            if expected_state == "unresolved":
                mapping = materialize_association_suggestion(
                    {
                        **proposal,
                        "idempotency_key": _derived_key(
                            str(payload["idempotency_key"]), "materialize"
                        ),
                    }
                )
            else:
                mapping = rematerialize_rejected_association_suggestion(
                    {
                        **proposal,
                        "name": current["mapping_ref"],
                        "expected_revision": expected_revision_value,
                        "idempotency_key": _derived_key(
                            str(payload["idempotency_key"]), "rematerialize"
                        ),
                    }
                )
            review = submit_identity_draft(
                {
                    "name": mapping["name"],
                    "team": team,
                    "observation_id": communication["observation_id"],
                    "suggestion_key": suggestions[0]["suggestion_key"],
                    "association_type": suggestions[0]["type"],
                    "model_suggested_target_ref": suggestions[0]["target_ref"],
                    "selected_candidate_type": candidate["candidate_type"],
                    "selected_candidate_ref": candidate["candidate_ref"],
                    "assigned_reviewer": reviewer,
                    "expected_revision": int(mapping["revision"]),
                    "evidence_refs": evidence_refs,
                    "policy_version": _POLICY_VERSION,
                    "idempotency_key": _derived_key(str(payload["idempotency_key"]), "submit"),
                    "request_id": current_request_id,
                }
            )
        except IdentityReviewError:
            raise BFFError(
                "validation_error", "Identity review could not be submitted", status=422
            ) from None
        if review.get("review_status") != "Pending" or review.get("subject_name") != mapping.get(
            "name"
        ):
            raise BFFError("internal_error", "Identity review receipt is invalid", status=503)
        return {
            "status": "pending",
            "mapping_ref": str(mapping["name"]),
            "mapping_revision": int(review["subject_revision"]),
            "review_case_ref": str(review["name"]),
            "review_case_revision": int(review["revision"]),
        }

    result, replayed, original_request_id = run_idempotent(
        "identity.submit_for_review",
        str(payload["idempotency_key"]),
        payload,
        execute,
        api_version="v4",
    )
    return v4_success(
        result,
        replayed=replayed,
        **({"original_request_id": original_request_id} if replayed else {}),
    )


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@bff_endpoint("POST")
def revoke(
    observation_id: str,
    identity_ref: str,
    mapping_ref: str,
    expected_revision: int | str,
    idempotency_key: str,
) -> dict[str, Any]:
    require_roles(IDENTITY_REVOKE_ROLES)
    payload = {
        "observation_id": _text(observation_id, "observation_id", maximum=48),
        "identity_ref": _text(identity_ref, "identity_ref", maximum=160),
        "mapping_ref": _text(mapping_ref, "mapping_ref", maximum=140),
        "expected_revision": _integer(expected_revision, "expected_revision", minimum=1),
        "idempotency_key": _idempotency_key(idempotency_key),
    }

    def execute() -> dict[str, Any]:
        communication = _fetch_communication(payload["observation_id"])
        participant = _participant(communication, payload["identity_ref"])
        try:
            mapping = frappe.get_doc(
                "GBOS External Identity", str(payload["mapping_ref"]), for_update=True
            )
        except Exception:
            raise BFFError("not_found", "Identity mapping was not found", status=404) from None
        if (
            str(_value(mapping, "team") or "") != str(communication["team_ref"])
            or str(_value(mapping, "identity_provider") or "") != str(participant["provider"])
            or str(_value(mapping, "external_subject") or "") != str(participant["identity_ref"])
        ):
            raise BFFError("scope_mismatch", "Identity mapping scope does not match", status=403)
        actual_revision = int(_value(mapping, "revision") or 0)
        if actual_revision != payload["expected_revision"]:
            raise BFFError("revision_conflict", "Identity mapping revision changed", status=409)
        if (
            _value(mapping, "review_status") != "Approved"
            or _value(mapping, "business_status") != "Active"
        ):
            raise BFFError(
                "invalid_transition",
                "Only an active approved mapping may be revoked",
                status=409,
            )
        mapping.flags.gbos_identity_status_command = True
        mapping.business_status = "Revoked"
        mapping.last_request_id = request_id()
        mapping.save(ignore_permissions=True)
        return {
            "status": "revoked",
            "mapping_ref": str(mapping.name),
            "mapping_revision": int(mapping.revision),
        }

    result, replayed, original_request_id = run_idempotent(
        "identity.revoke",
        str(payload["idempotency_key"]),
        payload,
        execute,
        api_version="v4",
    )
    return v4_success(
        result,
        replayed=replayed,
        **({"original_request_id": original_request_id} if replayed else {}),
    )


__all__ = [
    "get_pending_review",
    "get_state",
    "list_candidates",
    "list_pending_reviews",
    "list_states",
    "revoke",
    "submit_for_review",
]
