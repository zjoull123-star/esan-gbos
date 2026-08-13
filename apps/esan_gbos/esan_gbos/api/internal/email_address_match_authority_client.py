"""Closed production-local client for Observer email address-match authority."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Protocol, cast

import frappe

from esan_gbos.api.v5.gateway import configured_observer_email_material_client

_PATH = "/internal/v1/email-address-match/attest"
_PURPOSE = "email_address_identity_confirmation"
_CALLER_REF = "frappe-identity-command"
_REQUEST_FIELDS = frozenset(
    {
        "request_id",
        "site_id",
        "processing_purpose",
        "caller_ref",
        "evidence_ref",
        "address_role",
        "role_index",
        "opaque_address_ref",
        "candidate_target_ref",
        "candidate_target_type",
        "candidate_address",
    }
)
_ATTESTATION_FIELDS = frozenset(
    {
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
)
_BOUND_TEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")
_SITE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,139}$")
_EVIDENCE = re.compile(r"^EVR-[0-9A-HJKMNP-TV-Z]{26}$")
_OPAQUE_ADDRESS = re.compile(r"^extid:v1:email:[A-Za-z0-9_-]{43}$")
_TARGET = re.compile(r"^(USR|PTY)-[0-9A-HJKMNP-TV-Z]{26}$")
_ATTESTATION = re.compile(r"^EMA-[0-9A-HJKMNP-TV-Z]{26}$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_MAX_RESPONSE_BYTES = 8_192


class EmailAddressMatchAuthorityClientError(ValueError):
    """Safe local-authority rejection that never renders candidate material."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r})"


class _LocalClient(Protocol):
    def request(self, **kwargs: Any) -> dict[str, Any]: ...


class ObserverEmailAddressMatchAuthorityClient:
    def __init__(self, client: object) -> None:
        if not callable(getattr(client, "request", None)):
            raise EmailAddressMatchAuthorityClientError("authority_client_unavailable")
        self._client = cast(_LocalClient, client)

    def attest(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        payload = _request(request)
        response = self._client.request(
            method="POST",
            path=_PATH,
            site_id=payload["site_id"],
            purpose=_PURPOSE,
            request_id=payload["request_id"],
            payload=payload,
        )
        return _response(
            response,
            expected_site_id=payload["site_id"],
            expected_request_id=payload["request_id"],
        )

    def __repr__(self) -> str:
        return "ObserverEmailAddressMatchAuthorityClient(client=<redacted>)"


def inject_email_address_match_authority_client() -> ObserverEmailAddressMatchAuthorityClient:
    existing = getattr(frappe.local, "gbos_email_address_match_authority_client", None)
    if isinstance(existing, ObserverEmailAddressMatchAuthorityClient):
        return existing
    if existing is not None:
        raise EmailAddressMatchAuthorityClientError("authority_client_conflict")
    client = ObserverEmailAddressMatchAuthorityClient(configured_observer_email_material_client())
    frappe.local.gbos_email_address_match_authority_client = client
    return client


def _request(value: object) -> dict[str, Any]:
    site_id = str(getattr(frappe.local, "site", "") or "").strip()
    if not isinstance(value, Mapping) or set(value) != _REQUEST_FIELDS:
        raise EmailAddressMatchAuthorityClientError("authority_request_invalid")
    request_id = value.get("request_id")
    address_role = value.get("address_role")
    role_index = value.get("role_index")
    target_type = value.get("candidate_target_type")
    target_ref = value.get("candidate_target_ref")
    candidate = value.get("candidate_address")
    if (
        not isinstance(request_id, str)
        or _BOUND_TEXT.fullmatch(request_id) is None
        or _SITE.fullmatch(site_id) is None
        or value.get("site_id") != site_id
        or value.get("processing_purpose") != _PURPOSE
        or value.get("caller_ref") != _CALLER_REF
        or not isinstance(value.get("evidence_ref"), str)
        or _EVIDENCE.fullmatch(str(value["evidence_ref"])) is None
        or address_role not in {"from", "to", "cc", "bcc"}
        or isinstance(role_index, bool)
        or not isinstance(role_index, int)
        or not 0 <= role_index <= 999
        or not isinstance(value.get("opaque_address_ref"), str)
        or _OPAQUE_ADDRESS.fullmatch(str(value["opaque_address_ref"])) is None
        or not isinstance(target_ref, str)
        or _TARGET.fullmatch(target_ref) is None
        or target_type not in {"User", "Party"}
        or not target_ref.startswith(("USR" if target_type == "User" else "PTY") + "-")
        or not isinstance(candidate, str)
        or not 1 <= len(candidate) <= 254
        or candidate != candidate.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in candidate)
    ):
        raise EmailAddressMatchAuthorityClientError("authority_request_invalid")
    return dict(value)


def _response(
    value: object,
    *,
    expected_site_id: str,
    expected_request_id: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"data", "site_id", "meta"}:
        raise EmailAddressMatchAuthorityClientError("authority_response_invalid")
    meta = value.get("meta")
    if (
        value.get("site_id") != expected_site_id
        or not isinstance(meta, Mapping)
        or set(meta) != {"request_id", "schema_version"}
        or meta.get("request_id") != expected_request_id
        or meta.get("schema_version") != "1.0"
    ):
        raise EmailAddressMatchAuthorityClientError("authority_response_invalid")
    data = value.get("data")
    if not isinstance(data, Mapping) or set(data) != {"attestation_ref", "attestation"}:
        raise EmailAddressMatchAuthorityClientError("authority_response_invalid")
    attestation_ref = data.get("attestation_ref")
    attestation = data.get("attestation")
    if (
        not isinstance(attestation_ref, str)
        or _ATTESTATION.fullmatch(attestation_ref) is None
        or not isinstance(attestation, Mapping)
        or set(attestation) != _ATTESTATION_FIELDS
        or _OPAQUE_ADDRESS.fullmatch(str(attestation.get("opaque_address_ref") or "")) is None
        or _TARGET.fullmatch(str(attestation.get("candidate_target_ref") or "")) is None
        or attestation.get("candidate_target_type") not in {"User", "Party"}
        or _EVIDENCE.fullmatch(str(attestation.get("evidence_ref") or "")) is None
        or attestation.get("normalization_version") != "email-address-v1"
        or not isinstance(attestation.get("matched"), bool)
        or _DIGEST.fullmatch(str(attestation.get("digest") or "")) is None
        or any(
            not isinstance(attestation.get(field), str)
            or not 20 <= len(str(attestation[field])) <= 35
            for field in ("observed_at", "expires_at")
        )
    ):
        raise EmailAddressMatchAuthorityClientError("authority_response_invalid")
    prefix = "USR" if attestation["candidate_target_type"] == "User" else "PTY"
    if not str(attestation["candidate_target_ref"]).startswith(prefix + "-"):
        raise EmailAddressMatchAuthorityClientError("authority_response_invalid")
    try:
        encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except TypeError, ValueError:
        raise EmailAddressMatchAuthorityClientError("authority_response_invalid") from None
    if len(encoded) > _MAX_RESPONSE_BYTES:
        raise EmailAddressMatchAuthorityClientError("authority_response_invalid")
    return dict(data)


__all__ = [
    "EmailAddressMatchAuthorityClientError",
    "ObserverEmailAddressMatchAuthorityClient",
    "inject_email_address_match_authority_client",
]
