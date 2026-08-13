from __future__ import annotations

import pytest


def test_send_outbox_is_inert_until_approved_command_stage(scope) -> None:
    from services.email_gateway.models import OutboundNotAuthorized
    from services.email_gateway.send_outbox import DisabledSendOutboxRepository

    repository = DisabledSendOutboxRepository(outbound_enabled=False)
    with pytest.raises(OutboundNotAuthorized, match="outbound_not_authorized"):
        repository.insert(scope, object())
    with pytest.raises(OutboundNotAuthorized, match="outbound_not_authorized"):
        DisabledSendOutboxRepository(outbound_enabled=True).insert(scope, object())


def test_gateway_core_has_no_outbound_transport_protocol() -> None:
    from services.email_gateway import protocols

    names = set(vars(protocols))
    assert not names.intersection({"SmtpClient", "ProviderSender", "EmailTransport", "send_email"})
