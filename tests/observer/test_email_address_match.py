from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from observer.email_address_match import (
    EMAIL_ADDRESS_MATCH_PURPOSE,
    AddressMatchRejected,
    AddressMatchRequest,
    EmailAddressMatchService,
)
from observer.models import TenantScope

NOW = datetime(2026, 8, 13, 12, tzinfo=UTC)
SCOPE = TenantScope("alpha.example", EMAIL_ADDRESS_MATCH_PURPOSE)
EVIDENCE_REF = "EVR-01KZQEC7B9A41Q2ZCDPFGQ7V5K"
ROOT = Path(__file__).parents[2]


def _eml() -> bytes:
    message = EmailMessage()
    message["From"] = "Private Person <Private@Example.INVALID>"
    message["To"] = "recipient@example.invalid"
    message.set_content("private body")
    return message.as_bytes()


class RestrictedReader:
    def read_authorized(self, scope, evidence_ref, *, caller_ref, purpose):
        assert scope == SCOPE
        assert evidence_ref == EVIDENCE_REF
        assert caller_ref == "frappe-identity-command"
        assert purpose == "email_address_identity_confirmation"
        return _eml()


def _service() -> EmailAddressMatchService:
    return EmailAddressMatchService(
        evidence_reader=RestrictedReader(),
        signing_key=b"s" * 32,
        allowed_caller_ref="frappe-identity-command",
        clock=lambda: NOW,
        ttl_seconds=300,
    )


def _request(**changes: object) -> AddressMatchRequest:
    values = {
        "request_id": "request-001",
        "site_id": SCOPE.site_id,
        "processing_purpose": SCOPE.processing_purpose,
        "caller_ref": "frappe-identity-command",
        "evidence_ref": EVIDENCE_REF,
        "address_role": "from",
        "role_index": 0,
        "opaque_address_ref": "extid:v1:email:" + "e" * 43,
        "candidate_target_ref": "USR-01KZQEC7B9A41Q2ZCDPFGQ7V5K",
        "candidate_target_type": "User",
        "candidate_address": "private@example.invalid",
    }
    values.update(changes)
    return AddressMatchRequest(**values)  # type: ignore[arg-type]


def test_transient_match_returns_closed_expiring_attestation_without_raw_address() -> None:
    service = _service()
    response = service.attest(_request())
    attestation = response.attestation

    assert attestation.matched is True
    assert attestation.expires_at == NOW + timedelta(seconds=300)
    assert attestation.evidence_ref == EVIDENCE_REF
    assert attestation.digest.startswith("sha256:")
    assert set(attestation.to_wire()) == {
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
    assert "private@example.invalid" not in repr((service, attestation))
    assert set(response.to_wire()) == {"attestation_ref", "attestation"}
    assert response.to_wire()["attestation"] == attestation.to_wire()

    schema = json.loads(
        (
            ROOT
            / "contracts"
            / "email_gateway"
            / "email-address-match-attestation-v1.0.schema.json"
        ).read_text()
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(attestation.to_wire())


@pytest.mark.parametrize(
    "changes",
    [
        {"site_id": "other.example"},
        {"caller_ref": "gateway"},
        {"address_role": "cc"},
        {"role_index": 99},
    ],
)
def test_wrong_site_caller_purpose_role_or_evidence_is_rejected_safely(changes) -> None:
    with pytest.raises(AddressMatchRejected) as exc:
        _service().attest(_request(**changes))
    assert "private@example.invalid" not in repr(exc.value)


def test_request_replay_drift_and_expired_attestation_are_rejected() -> None:
    service = _service()
    response = service.attest(_request())
    attestation = response.attestation
    assert service.attest(_request()) == response

    with pytest.raises(AddressMatchRejected, match="request_replay_drift"):
        service.attest(_request(candidate_target_ref="USR-01KZQEC7B9A41Q2ZCDPFGQ7V5M"))

    with pytest.raises(AddressMatchRejected, match="attestation_expired"):
        service.require_current(attestation, now=attestation.expires_at)


def test_address_match_request_and_repr_freeze_protected_boundary() -> None:
    request = _request()
    assert set(request.to_wire()) == {
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
    with pytest.raises(TypeError):
        _request(match_purpose="routing")

    response = _service().attest(request)
    rendered = repr((request, response, response.attestation))
    for protected in (
        request.candidate_target_ref,
        request.opaque_address_ref,
        request.evidence_ref,
        request.candidate_address,
    ):
        assert protected not in rendered


def test_request_rejects_any_non_frozen_processing_purpose() -> None:
    with pytest.raises(ValueError, match="processing purpose"):
        _request(processing_purpose="entity_resolution")
