from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

import pytest
from observer.connectors.wecom_archive import (
    ArchiveDisposition,
    DecryptedMessage,
    EncryptedEnvelope,
    MediaDownloadChunk,
    MediaDownloadRequest,
    SdkFetchPage,
    SdkFetchStatus,
    WeComArchiveAdapter,
    WeComArchiveConfig,
    WeComArchiveError,
    preflight_official_sdk,
)

NOW = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)


class FakeOfficialSdk:
    def __init__(
        self,
        *,
        pages: Sequence[SdkFetchPage] = (),
        decrypted: Sequence[bytes | Exception] = (),
        media: Sequence[MediaDownloadChunk | Exception] = (),
    ) -> None:
        self._pages = list(pages)
        self._decrypted = list(decrypted)
        self._media = list(media)
        self.fetch_calls: list[tuple[int, int]] = []
        self.random_key_calls = 0
        self.decrypt_calls = 0
        self.media_calls: list[tuple[str, bytes]] = []

    def fetch_chat_data(self, *, seq: int, limit: int) -> SdkFetchPage:
        self.fetch_calls.append((seq, limit))
        return self._pages.pop(0)

    def decrypt_random_key(self, *, encrypt_random_key: bytes) -> bytes:
        del encrypt_random_key
        self.random_key_calls += 1
        return b"decrypted-random-key"

    def decrypt_chat_data(
        self,
        *,
        decrypted_random_key: bytes,
        encrypt_chat_msg: bytes,
    ) -> bytes:
        assert decrypted_random_key == b"decrypted-random-key"
        del encrypt_chat_msg
        self.decrypt_calls += 1
        result = self._decrypted.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def download_media(self, *, sdk_file_id: str, cursor: bytes) -> MediaDownloadChunk:
        self.media_calls.append((sdk_file_id, cursor))
        result = self._media.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _envelope(seq: int) -> EncryptedEnvelope:
    return EncryptedEnvelope(
        seq=seq,
        exact_bytes=f'{{"seq":{seq},"secret":"encrypted"}}'.encode(),
        encrypt_random_key=f"key-{seq}".encode(),
        encrypt_chat_msg=f"cipher-{seq}".encode(),
    )


def _adapter(
    sdk: FakeOfficialSdk,
    *,
    config: WeComArchiveConfig | None = None,
) -> WeComArchiveAdapter:
    return WeComArchiveAdapter(
        config=config or WeComArchiveConfig(instance_id="sales-primary"),
        sdk=sdk,
        clock=lambda: NOW,
    )


def test_fetch_is_bounded_replayable_and_does_not_persist_checkpoint() -> None:
    page = SdkFetchPage(status=SdkFetchStatus.OK, envelopes=(_envelope(12), _envelope(11)))
    sdk = FakeOfficialSdk(pages=(page, page))
    adapter = _adapter(sdk)

    first = adapter.fetch("10", 2)
    replay = adapter.fetch("10", 2)

    assert sdk.fetch_calls == [(10, 2), (10, 2)]
    assert [envelope.seq for envelope in first.envelopes] == [11, 12]
    assert first.expected_checkpoint == "10"
    assert first.next_checkpoint == "12"
    assert replay == first
    assert not hasattr(adapter, "checkpoint")
    assert [delivery.delivery_id for delivery in first.deliveries] == [
        "wecom-archive-sales-primary-seq-11",
        "wecom-archive-sales-primary-seq-12",
    ]
    assert first.deliveries[0].exact_bytes is first.envelopes[0].exact_bytes

    for invalid_limit in (0, -1, 1001):
        with pytest.raises(ValueError, match="bounded"):
            adapter.fetch("10", invalid_limit)


@pytest.mark.parametrize(
    ("envelopes", "code"),
    [
        ((_envelope(11), _envelope(13)), "wecom_archive.seq_gap"),
        ((_envelope(10), _envelope(11)), "wecom_archive.seq_rollback"),
        ((_envelope(11), _envelope(11)), "wecom_archive.seq_rollback"),
    ],
)
def test_fetch_quarantines_sequence_gaps_and_rollbacks(
    envelopes: tuple[EncryptedEnvelope, ...],
    code: str,
) -> None:
    adapter = _adapter(FakeOfficialSdk(pages=(SdkFetchPage.ok(envelopes),)))

    with pytest.raises(WeComArchiveError) as captured:
        adapter.fetch("10", 10)

    assert captured.value.code == code
    assert captured.value.disposition is ArchiveDisposition.QUARANTINE


def test_decrypt_and_normalize_are_separate_and_duplicate_msgids_are_deduplicated() -> None:
    body = b'{"msgid":"message-001","msgtime":1786075800000,"msgtype":"text"}'
    sdk = FakeOfficialSdk(decrypted=(body, body))
    adapter = _adapter(sdk)
    decrypted = (
        adapter.decrypt(_envelope(11)),
        adapter.decrypt(_envelope(12)),
    )

    normalized = adapter.normalize_batch(
        decrypted,
        content_refs=("object://decrypted/11", "object://decrypted/12"),
    )

    assert sdk.decrypt_calls == 2
    assert sdk.random_key_calls == 2
    assert [item.provider_event_id for item in normalized.items] == ["message-001"]
    assert normalized.items[0].source_cursor == "11"
    assert normalized.items[0].payload == {
        "message_type": "text",
        "decrypted_content_ref": "object://decrypted/11",
        "media_pending": False,
    }
    assert normalized.duplicate_msgids == ("message-001",)


def test_decrypt_failure_is_retryable_and_does_not_expose_sdk_details() -> None:
    leaked = "corp-secret private-key-body cipher-11"
    adapter = _adapter(FakeOfficialSdk(decrypted=(RuntimeError(leaked),)))

    with pytest.raises(WeComArchiveError) as captured:
        adapter.decrypt(_envelope(11))

    assert captured.value.code == "wecom_archive.decrypt_failed"
    assert captured.value.disposition is ArchiveDisposition.RETRY
    assert leaked not in str(captured.value)
    assert leaked not in repr(captured.value)


def test_media_download_is_read_only_binary_and_retryable_as_its_own_stage() -> None:
    chunk = MediaDownloadChunk(
        content=b"\x00\xff\x80binary",
        next_cursor=b"cursor-2",
        complete=False,
        media_type="application/octet-stream",
    )
    sdk = FakeOfficialSdk(media=(RuntimeError("sdk secret detail"), chunk))
    adapter = _adapter(sdk)
    request = MediaDownloadRequest(sdk_file_id="opaque-media-id", cursor=b"")

    with pytest.raises(WeComArchiveError) as captured:
        adapter.download_media(request)
    downloaded = adapter.download_media(request)

    assert captured.value.code == "wecom_archive.media_download_failed"
    assert captured.value.disposition is ArchiveDisposition.RETRY
    assert sdk.media_calls == [("opaque-media-id", b""), ("opaque-media-id", b"")]
    assert downloaded.content == b"\x00\xff\x80binary"
    assert isinstance(downloaded.content, bytes)
    assert request.read_only is True


def test_media_description_is_separate_and_only_emits_read_only_requests() -> None:
    message = DecryptedMessage(
        seq=11,
        exact_bytes=(
            b'{"msgid":"media-001","msgtime":1786075800000,"msgtype":"file",'
            b'"file":{"sdkfileid":"opaque-file-id","filename":"do-not-download-yet"}}'
        ),
    )
    adapter = _adapter(FakeOfficialSdk())

    requests = adapter.describe_media(message)

    assert len(requests) == 1
    assert requests[0].sdk_file_id == "opaque-file-id"
    assert requests[0].cursor == b""
    assert requests[0].read_only is True


def test_media_description_deduplicates_ids_and_quarantines_over_limit() -> None:
    duplicate = DecryptedMessage(
        seq=11,
        exact_bytes=(
            b'{"msgid":"media-001","msgtime":1786075800000,"msgtype":"file",'
            b'"file":{"sdkfileid":"same-id"},"attachments":[{"sdkfileid":"same-id"}]}'
        ),
    )
    over_limit = DecryptedMessage(
        seq=12,
        exact_bytes=(
            b'{"msgid":"media-002","msgtime":1786075800000,"msgtype":"file",'
            b'"attachments":[{"sdkfileid":"first-id"},{"sdkfileid":"second-id"}]}'
        ),
    )
    adapter = _adapter(
        FakeOfficialSdk(),
        config=WeComArchiveConfig(instance_id="sales-primary", max_media_requests=1),
    )

    assert [request.sdk_file_id for request in adapter.describe_media(duplicate)] == ["same-id"]
    with pytest.raises(WeComArchiveError) as captured:
        adapter.describe_media(over_limit)

    assert captured.value.code == "wecom_archive.media_request_limit_exceeded"
    assert captured.value.disposition is ArchiveDisposition.QUARANTINE


@pytest.mark.parametrize(
    ("message_type", "code", "disposition"),
    [
        ("revoke", "wecom_archive.message_revoked", ArchiveDisposition.PRESERVE_ONLY),
        ("agree", "wecom_archive.consent_granted", ArchiveDisposition.PRESERVE_ONLY),
        ("disagree", "wecom_archive.consent_declined", ArchiveDisposition.PAUSE),
    ],
)
def test_revoke_and_disagree_are_preserved_as_control_states_not_business_items(
    message_type: str,
    code: str,
    disposition: ArchiveDisposition,
) -> None:
    message = DecryptedMessage(
        seq=11,
        exact_bytes=(
            f'{{"msgid":"control-001","msgtime":1786075800000,"msgtype":"{message_type}"}}'
        ).encode(),
    )
    adapter = _adapter(FakeOfficialSdk())

    normalized = adapter.normalize_batch(
        (message,),
        content_refs=("object://decrypted/control",),
    )

    assert normalized.items == ()
    assert len(normalized.controls) == 1
    assert normalized.controls[0].reason_code == code
    assert normalized.controls[0].disposition is disposition
    assert normalized.controls[0].content_ref == "object://decrypted/control"


def test_recall_action_is_a_control_state_not_a_business_item() -> None:
    message = DecryptedMessage(
        seq=11,
        exact_bytes=(
            b'{"msgid":"recall-001","msgtime":1786075800000,"msgtype":"text","action":"recall"}'
        ),
    )
    adapter = _adapter(FakeOfficialSdk())

    normalized = adapter.normalize_batch(
        (message,),
        content_refs=("object://decrypted/recall",),
    )

    assert normalized.items == ()
    assert normalized.controls[0].reason_code == "wecom_archive.message_recalled"
    assert normalized.controls[0].disposition is ArchiveDisposition.PRESERVE_ONLY


def test_switch_action_is_a_control_state_not_a_business_item() -> None:
    message = DecryptedMessage(
        seq=11,
        exact_bytes=(
            b'{"msgid":"switch-001","msgtime":1786075800000,"msgtype":"text","action":"switch"}'
        ),
    )

    normalized = _adapter(FakeOfficialSdk()).normalize_batch(
        (message,),
        content_refs=("object://decrypted/switch",),
    )

    assert normalized.items == ()
    assert normalized.controls[0].reason_code == "wecom_archive.message_switched"
    assert normalized.controls[0].disposition is ArchiveDisposition.PRESERVE_ONLY


@pytest.mark.parametrize(
    "exact_bytes",
    [
        (b'{"msgid":"unknown-action","msgtime":1786075800000,"msgtype":"text","action":"forward"}'),
        b'{"msgid":"unknown-type","msgtime":1786075800000,"msgtype":"future_control"}',
        (
            b'{"msgid":"unknown-switched","msgtime":1786075800000,'
            b'"msgtype":"future_control","action":"switch"}'
        ),
    ],
)
def test_unknown_action_or_message_type_is_quarantined(exact_bytes: bytes) -> None:
    message = DecryptedMessage(seq=11, exact_bytes=exact_bytes)

    with pytest.raises(WeComArchiveError) as captured:
        _adapter(FakeOfficialSdk()).normalize_batch(
            (message,),
            content_refs=("object://decrypted/unknown",),
        )

    assert captured.value.code == "wecom_archive.unsupported_message_semantics"
    assert captured.value.disposition is ArchiveDisposition.QUARANTINE


def test_duplicate_control_msgid_is_not_emitted_twice() -> None:
    message = DecryptedMessage(
        seq=11,
        exact_bytes=(b'{"msgid":"consent-001","msgtime":1786075800000,"msgtype":"disagree"}'),
    )
    replay = DecryptedMessage(seq=12, exact_bytes=message.exact_bytes)
    adapter = _adapter(FakeOfficialSdk())

    normalized = adapter.normalize_batch(
        (message, replay),
        content_refs=("object://decrypted/11", "object://decrypted/12"),
    )

    assert len(normalized.controls) == 1
    assert normalized.duplicate_msgids == ("consent-001",)


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (SdkFetchStatus.PERMISSION_DENIED, "wecom_archive.permission_denied"),
        (SdkFetchStatus.ARCHIVE_NOT_AUTHORIZED, "wecom_archive.archive_not_authorized"),
        (SdkFetchStatus.MEMBER_ARCHIVE_DISABLED, "wecom_archive.member_archive_disabled"),
    ],
)
def test_fetch_pauses_on_official_sdk_access_states(
    status: SdkFetchStatus,
    code: str,
) -> None:
    adapter = _adapter(FakeOfficialSdk(pages=(SdkFetchPage(status=status),)))

    with pytest.raises(WeComArchiveError) as captured:
        adapter.fetch(None, 100)

    assert captured.value.code == code
    assert captured.value.disposition is ArchiveDisposition.PAUSE


def test_sdk_preflight_prefers_linux_arm64_and_only_plans_digest_pinned_fallback() -> None:
    direct = preflight_official_sdk(
        system="Linux",
        machine="aarch64",
        target_platform="linux/arm64",
        official_linux_arm64_available=True,
    )
    orbstack = preflight_official_sdk(
        system="Darwin",
        machine="arm64",
        target_platform="linux/arm64",
        official_linux_arm64_available=True,
    )
    blocked = preflight_official_sdk(
        system="Darwin",
        machine="arm64",
        target_platform="linux/arm64",
        official_linux_arm64_available=False,
    )

    assert direct.status == "ready"
    assert direct.selected_runtime == "official-linux-arm64"
    assert direct.target_platform == "linux/arm64"
    assert direct.execution == "plan_only"
    assert direct.container_plan is None
    assert orbstack.status == "ready"
    assert orbstack.selected_runtime == "official-linux-arm64-container"
    assert orbstack.target_platform == "linux/arm64"
    assert orbstack.execution == "plan_only"
    assert orbstack.container_plan is None
    assert blocked.status == "blocked"
    assert blocked.error_code == "wecom_archive.sdk_architecture_unavailable"
    assert blocked.target_platform == "linux/amd64"
    assert blocked.execution == "plan_only"
    assert blocked.container_plan is not None
    assert blocked.container_plan.platform == "linux/amd64"
    assert blocked.container_plan.fixed_digest_required is True
    assert blocked.container_plan.isolated is True
    assert blocked.container_plan.execution == "plan_only"


@pytest.mark.parametrize(
    ("config", "exact_bytes", "code"),
    [
        (
            WeComArchiveConfig(instance_id="sales-primary", max_decrypted_bytes=64),
            b'{"msgid":"' + (b"x" * 80) + b'"}',
            "wecom_archive.decrypted_message_too_large",
        ),
        (
            WeComArchiveConfig(instance_id="sales-primary", max_json_depth=3),
            b'{"msgid":"deep","msgtime":1786075800000,"msgtype":"text","a":{"b":{"c":1}}}',
            "wecom_archive.json_depth_exceeded",
        ),
        (
            WeComArchiveConfig(instance_id="sales-primary", max_json_nodes=7),
            b'{"msgid":"nodes","msgtime":1786075800000,"msgtype":"text","a":[1,2,3,4]}',
            "wecom_archive.json_node_limit_exceeded",
        ),
        (
            WeComArchiveConfig(instance_id="sales-primary"),
            (b'{"msgid":"first","msgid":"second","msgtime":1786075800000,"msgtype":"text"}'),
            "wecom_archive.duplicate_json_key",
        ),
        (
            WeComArchiveConfig(instance_id="sales-primary"),
            (b'{"msgid":"nan","msgtime":1786075800000,"msgtype":"text","unsafe":NaN}'),
            "wecom_archive.non_finite_json_number",
        ),
        (
            WeComArchiveConfig(instance_id="sales-primary"),
            (b'{"msgid":"inf","msgtime":1786075800000,"msgtype":"text","unsafe":1e400}'),
            "wecom_archive.non_finite_json_number",
        ),
    ],
)
def test_untrusted_decrypted_json_is_strictly_bounded_and_quarantined(
    config: WeComArchiveConfig,
    exact_bytes: bytes,
    code: str,
) -> None:
    message = DecryptedMessage(seq=11, exact_bytes=exact_bytes)

    with pytest.raises(WeComArchiveError) as captured:
        _adapter(FakeOfficialSdk(), config=config).normalize_batch(
            (message,),
            content_refs=("object://decrypted/bounded",),
        )

    assert captured.value.code == code
    assert captured.value.disposition is ArchiveDisposition.QUARANTINE
    assert exact_bytes.decode(errors="ignore") not in repr(captured.value)


def test_public_boundary_has_no_outbound_or_mutating_methods() -> None:
    names = set(dir(WeComArchiveAdapter))
    forbidden = {"send", "send_message", "post", "update", "delete", "upload_media"}

    assert names.isdisjoint(forbidden)
    assert {"fetch", "decrypt", "normalize_batch", "download_media"} <= names


def test_repr_and_errors_redact_encrypted_content_sdk_and_credentials() -> None:
    corp_id = "ww-corp-id-sensitive"
    secret = "application-secret-sensitive"
    private_key = "-----BEGIN PRIVATE KEY-----sensitive"
    sdk = FakeOfficialSdk()
    adapter = _adapter(sdk)
    envelope = EncryptedEnvelope(
        seq=11,
        exact_bytes=f"{corp_id}:{secret}".encode(),
        encrypt_random_key=private_key.encode(),
        encrypt_chat_msg=secret.encode(),
    )
    decrypted = DecryptedMessage(seq=11, exact_bytes=private_key.encode())
    request = MediaDownloadRequest(sdk_file_id=secret, cursor=private_key.encode())
    page = SdkFetchPage.ok((envelope,))
    adapter = _adapter(FakeOfficialSdk(pages=(page,)))
    batch = adapter.fetch("10", 1)

    rendered = " ".join(map(repr, (adapter, envelope, decrypted, request, page, batch)))

    assert corp_id not in rendered
    assert secret not in rendered
    assert private_key not in rendered
    assert "redacted" in rendered
    assert set(WeComArchiveConfig.__dataclass_fields__) == {
        "instance_id",
        "max_batch_size",
        "max_decrypted_bytes",
        "max_json_depth",
        "max_json_nodes",
        "max_media_requests",
    }
    assert not hasattr(cast(object, adapter), "corp_id")
    assert not hasattr(cast(object, adapter), "secret")
    assert not hasattr(cast(object, adapter), "private_key")
