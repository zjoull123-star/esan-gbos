from __future__ import annotations

from datetime import UTC, datetime

import pytest

SITE = "site.local"
PURPOSE = "sales_follow_up"
NOW = datetime(2026, 8, 13, 1, 2, 3, tzinfo=UTC)
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
OPAQUE_FROM = "extid:v1:email:" + "A" * 43
OPAQUE_TO = "extid:v1:email:" + "B" * 43


@pytest.fixture
def scope():
    from services.email_gateway.models import TenantScope

    return TenantScope(site_id=SITE, processing_purpose=PURPOSE)


@pytest.fixture
def mailbox():
    from services.email_gateway.models import Mailbox

    return Mailbox(
        mailbox_ref="MBX-01",
        site_id=SITE,
        address_display="primary@company.invalid",
        provider="fake",
        provider_account_ref="provider-account-01",
        observer_connector_instance_ref="observer-email-01",
        entry_role="primary",
        business_purpose=PURPOSE,
        default_team_ref="TEM-01",
        account_owner_user_ref="owner@company.invalid",
        priority=10,
        inbound_enabled=True,
        outbound_enabled=False,
        credential_ref="email-primary",
        status="active",
        config_revision=1,
        observer_config_projection_receipt=None,
    )


@pytest.fixture
def publication():
    from services.email_gateway.models import EmailMessagePublication, PublicationParticipant

    return EmailMessagePublication(
        publication_ref="PUB-01",
        site_id=SITE,
        processing_purpose=PURPOSE,
        mailbox_ref="MBX-01",
        mailbox_config_revision=1,
        observer_connector_instance_ref="observer-email-01",
        observer_delivery_ref="DEL-01",
        received_at=NOW,
        participants=(
            PublicationParticipant(role="from", identity_ref=OPAQUE_FROM),
            PublicationParticipant(role="to", identity_ref=OPAQUE_TO),
        ),
        subject_projection="Restricted subject",
        subject_digest=None,
        message_id_digest=DIGEST_A,
        in_reply_to_digest=None,
        references_digests=(),
        evidence_refs=("EVD-RAW-01",),
        publication_revision=1,
        idempotency_key="publication-01",
        payload_digest=DIGEST_B,
    )
