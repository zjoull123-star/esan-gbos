"""One closed projection for every external-identity authority consumer."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


class ExternalIdentityProjectionError(ValueError):
    """The persisted authority row cannot be safely projected."""


def _value(source: object, fieldname: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(fieldname, default)
    getter = getattr(source, "get", None)
    if callable(getter):
        return getter(fieldname, default)
    return getattr(source, fieldname, default)


def owner_eligibility_revision(
    party: object,
    state: Mapping[str, Any],
) -> str:
    payload = {
        "schema_version": "owner-eligibility-v1",
        "party_ref": str(_value(party, "name") or ""),
        "party_revision": int(_value(party, "revision") or 0),
        "team_ref": str(_value(party, "team") or ""),
        "team_revision": int(state.get("team_revision") or 0),
        "owner_user_ref": str(_value(party, "owner_user") or ""),
        "owner_enabled": int(state.get("owner_enabled") or 0),
        "owner_user_type": str(state.get("owner_user_type") or ""),
        "membership_ref": str(state.get("membership_ref") or ""),
        "membership_parent": str(state.get("membership_parent") or ""),
        "membership_user": str(state.get("membership_user") or ""),
        "membership_enabled": int(state.get("membership_enabled") or 0),
        "membership_modified": str(state.get("membership_modified") or ""),
    }
    serialized = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return f"sha256:{hashlib.sha256(serialized.encode()).hexdigest()}"


def build_external_identity_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    mapping_ref = row.get("mapping_ref")
    mapping_revision = row.get("mapping_revision")
    team_ref = row.get("team_ref")
    target_type = row.get("target_type")
    business_status = row.get("business_status")
    user_ref = row.get("user_ref")
    party_ref = row.get("party_ref")
    target_shape_valid = bool(
        (target_type == "User" and isinstance(user_ref, str) and user_ref and not party_ref)
        or (target_type == "Party" and isinstance(party_ref, str) and party_ref and not user_ref)
    )
    if (
        not isinstance(mapping_ref, str)
        or not mapping_ref
        or isinstance(mapping_revision, bool)
        or not isinstance(mapping_revision, int)
        or mapping_revision < 1
        or not isinstance(team_ref, str)
        or not team_ref
        or target_type not in {"User", "Party"}
        or not target_shape_valid
        or row.get("review_status") != "Approved"
        or business_status not in {"Active", "Revoked"}
    ):
        raise ExternalIdentityProjectionError("external identity authority is invalid")
    return {
        "mapping_ref": mapping_ref,
        "mapping_revision": mapping_revision,
        "status": "confirmed" if business_status == "Active" else "revoked",
        "target_type": target_type,
        "team_ref": team_ref,
    }


__all__ = [
    "ExternalIdentityProjectionError",
    "build_external_identity_projection",
    "owner_eligibility_revision",
]
