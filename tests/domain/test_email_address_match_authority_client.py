from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace
from typing import Any

import pytest


class RecordingClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def request(self, **kwargs: Any) -> dict[str, object]:
        self.calls.append(kwargs)
        return {
            "site_id": "gbos.localhost",
            "meta": {
                "request_id": "REQ-ADDRESS-MATCH-01",
                "schema_version": "1.0",
            },
            "data": {
                "attestation_ref": "EMA-01KZQEC7B9A41Q2ZCDPFGQ7V5K",
                "attestation": {
                    "opaque_address_ref": "extid:v1:email:" + "e" * 43,
                    "candidate_target_ref": "USR-01KZQEC7B9A41Q2ZCDPFGQ7V5K",
                    "candidate_target_type": "User",
                    "evidence_ref": "EVR-01KZQEC7B9A41Q2ZCDPFGQ7V5K",
                    "normalization_version": "email-address-v1",
                    "matched": True,
                    "observed_at": "2026-08-14T01:00:00Z",
                    "expires_at": "2026-08-14T01:05:00Z",
                    "digest": "sha256:" + "d" * 64,
                },
            },
        }


@pytest.fixture
def authority_module() -> tuple[Any, SimpleNamespace, RecordingClient]:
    recording = RecordingClient()
    fake_frappe = SimpleNamespace(
        local=SimpleNamespace(site="gbos.localhost"),
    )
    fake_gateway = SimpleNamespace(
        configured_observer_email_material_client=lambda: recording,
    )
    original_frappe = sys.modules.get("frappe")
    original_gateway = sys.modules.get("esan_gbos.api.v5.gateway")
    sys.modules["frappe"] = fake_frappe
    sys.modules["esan_gbos.api.v5.gateway"] = fake_gateway
    sys.modules.pop("esan_gbos.api.internal.email_address_match_authority_client", None)
    module = importlib.import_module("esan_gbos.api.internal.email_address_match_authority_client")
    yield module, fake_frappe, recording
    sys.modules.pop("esan_gbos.api.internal.email_address_match_authority_client", None)
    if original_gateway is None:
        sys.modules.pop("esan_gbos.api.v5.gateway", None)
    else:
        sys.modules["esan_gbos.api.v5.gateway"] = original_gateway
    if original_frappe is None:
        sys.modules.pop("frappe", None)
    else:
        sys.modules["frappe"] = original_frappe


def _request(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "request_id": "REQ-ADDRESS-MATCH-01",
        "site_id": "gbos.localhost",
        "processing_purpose": "email_address_identity_confirmation",
        "caller_ref": "frappe-identity-command",
        "evidence_ref": "EVR-01KZQEC7B9A41Q2ZCDPFGQ7V5K",
        "address_role": "from",
        "role_index": 0,
        "opaque_address_ref": "extid:v1:email:" + "e" * 43,
        "candidate_target_ref": "USR-01KZQEC7B9A41Q2ZCDPFGQ7V5K",
        "candidate_target_type": "User",
        "candidate_address": "private-candidate@example.invalid",
    }
    value.update(changes)
    return value


def test_injected_client_uses_exact_observer_local_authority_boundary(
    authority_module: tuple[Any, SimpleNamespace, RecordingClient],
) -> None:
    module, fake_frappe, recording = authority_module

    client = module.inject_email_address_match_authority_client()
    response = client.attest(_request())

    assert fake_frappe.local.gbos_email_address_match_authority_client is client
    assert set(response) == {"attestation_ref", "attestation"}
    assert len(recording.calls) == 1
    call = recording.calls[0]
    assert call == {
        "method": "POST",
        "path": "/internal/v1/email-address-match/attest",
        "site_id": "gbos.localhost",
        "purpose": "email_address_identity_confirmation",
        "request_id": "REQ-ADDRESS-MATCH-01",
        "payload": _request(),
    }
    assert "private-candidate@example.invalid" not in repr(client)


@pytest.mark.parametrize(
    "changes",
    (
        {"site_id": "other.localhost"},
        {"processing_purpose": "email_draft_material"},
        {"caller_ref": "other-caller"},
        {"candidate_address": "x" * 255},
        {"raw_address": "extra-private@example.invalid"},
    ),
)
def test_client_rejects_scope_purpose_caller_size_or_shape_before_io_without_leak(
    authority_module: tuple[Any, SimpleNamespace, RecordingClient],
    changes: dict[str, object],
) -> None:
    module, _fake_frappe, recording = authority_module
    client = module.inject_email_address_match_authority_client()
    request = _request(**changes)

    with pytest.raises(Exception) as raised:
        client.attest(request)

    assert recording.calls == []
    assert "private-candidate@example.invalid" not in repr(raised.value)
    assert "extra-private@example.invalid" not in repr(raised.value)


def test_client_rejects_unclosed_or_oversized_response_without_leaking_candidate(
    authority_module: tuple[Any, SimpleNamespace, RecordingClient],
) -> None:
    module, _fake_frappe, recording = authority_module
    client = module.inject_email_address_match_authority_client()
    recording.request = lambda **_kwargs: {  # type: ignore[method-assign]
        "data": {"attestation_ref": "bad", "candidate_address": "reflected-private"}
    }

    with pytest.raises(Exception) as raised:
        client.attest(_request())

    assert "reflected-private" not in repr(raised.value)


@pytest.mark.parametrize(
    "mutate",
    (
        lambda response: response.pop("meta"),
        lambda response: response["meta"].update({"request_id": "wrong-request"}),
        lambda response: response["meta"].update({"extra": "unclosed"}),
        lambda response: response.update({"extra": "unclosed"}),
    ),
)
def test_client_requires_exact_response_envelope_and_request_binding(
    authority_module: tuple[Any, SimpleNamespace, RecordingClient],
    mutate: Any,
) -> None:
    module, _fake_frappe, recording = authority_module
    original_request = recording.request

    def malformed(**kwargs: Any) -> dict[str, object]:
        response = original_request(**kwargs)
        mutate(response)
        return response

    recording.request = malformed  # type: ignore[method-assign]
    client = module.inject_email_address_match_authority_client()

    with pytest.raises(module.EmailAddressMatchAuthorityClientError) as raised:
        client.attest(_request())

    assert raised.value.code == "authority_response_invalid"


def test_client_rejects_response_site_drift(
    authority_module: tuple[Any, SimpleNamespace, RecordingClient],
) -> None:
    module, _fake_frappe, recording = authority_module
    client = module.inject_email_address_match_authority_client()
    original_request = recording.request

    def mismatched(**kwargs: Any) -> dict[str, object]:
        response = original_request(**kwargs)
        response["site_id"] = "other.localhost"
        return response

    recording.request = mismatched  # type: ignore[method-assign]

    with pytest.raises(Exception, match="authority_response_invalid"):
        client.attest(_request())
