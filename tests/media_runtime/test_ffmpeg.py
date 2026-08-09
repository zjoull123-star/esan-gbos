from __future__ import annotations

import hashlib

import pytest

from services.media_runtime.ffmpeg import (
    FFMPEG_ARTIFACT_NAME,
    FFMPEG_DURATION_TOLERANCE_MS,
    FFMPEG_EXECUTABLE_REF,
    FFMPEG_VERSION,
    ArtifactIdentity,
    FFmpegAdapter,
    FFmpegOutputProof,
    FFmpegRejected,
    FFmpegRequest,
    FFmpegRunResult,
)

TEST_FFMPEG_SHA256 = hashlib.sha256(b"bound-test-ffmpeg-artifact").hexdigest()
TEST_OUTPUT_SHA256 = hashlib.sha256(b"verified-test-wave-output").hexdigest()


class RecordingRunner:
    def __init__(self, result: FFmpegRunResult | None = None) -> None:
        self.result = result or FFmpegRunResult(returncode=0, stdout="", stderr="")
        self.calls: list[tuple[tuple[str, ...], int]] = []

    def run(self, argv: tuple[str, ...], *, timeout_seconds: int) -> FFmpegRunResult:
        self.calls.append((argv, timeout_seconds))
        return self.result


class RecordingOutputVerifier:
    def __init__(
        self,
        proof: FFmpegOutputProof | None = None,
        *,
        failure: Exception | None = None,
    ) -> None:
        self.proof = proof or _proof()
        self.failure = failure
        self.calls: list[str] = []

    def verify(self, output_path: str) -> FFmpegOutputProof:
        self.calls.append(output_path)
        if self.failure is not None:
            raise self.failure
        return self.proof


def _identity(**changes: object) -> ArtifactIdentity:
    values: dict[str, object] = {
        "name": FFMPEG_ARTIFACT_NAME,
        "version": FFMPEG_VERSION,
        "read_only_path": FFMPEG_EXECUTABLE_REF,
        "sha256": TEST_FFMPEG_SHA256,
        "read_only": True,
    }
    values.update(changes)
    return ArtifactIdentity(**values)  # type: ignore[arg-type]


def _proof(**changes: object) -> FFmpegOutputProof:
    values: dict[str, object] = {
        "exists": True,
        "byte_size": 192_044,
        "media_type": "audio/wav",
        "codec": "pcm_s16le",
        "channels": 1,
        "sample_rate": 16_000,
        "duration_ms": 60_000,
        "content_sha256": TEST_OUTPUT_SHA256,
    }
    values.update(changes)
    return FFmpegOutputProof(**values)  # type: ignore[arg-type]


def _request(**changes: object) -> FFmpegRequest:
    values: dict[str, object] = {
        "source_path": "/media/input/object-01",
        "output_path": "/media/output/request-01.wav",
        "duration_ms": 60_000,
        "channels": 2,
        "sample_rate": 48_000,
    }
    values.update(changes)
    return FFmpegRequest(**values)  # type: ignore[arg-type]


def _adapter(
    *,
    runner: RecordingRunner | None = None,
    output_verifier: RecordingOutputVerifier | None = None,
    artifact_identity: ArtifactIdentity | None = None,
) -> tuple[FFmpegAdapter, RecordingRunner, RecordingOutputVerifier]:
    actual_runner = runner or RecordingRunner()
    actual_verifier = output_verifier or RecordingOutputVerifier()
    return (
        FFmpegAdapter(
            runner=actual_runner,
            output_verifier=actual_verifier,
            artifact_identity=artifact_identity or _identity(),
        ),
        actual_runner,
        actual_verifier,
    )


def test_ffmpeg_uses_bound_identity_argv_only_and_verified_audio_output() -> None:
    adapter, runner, verifier = _adapter()

    normalized = adapter.normalize(_request(), idempotency_key="normalize:01")

    assert len(runner.calls) == 1
    argv, timeout = runner.calls[0]
    assert isinstance(argv, tuple)
    assert argv[0] == FFMPEG_EXECUTABLE_REF
    assert "-nostdin" in argv
    assert (
        argv[argv.index("-protocol_whitelist")],
        argv[argv.index("-protocol_whitelist") + 1],
    ) == ("-protocol_whitelist", "file")
    assert "-protocol_blacklist" in argv
    blacklist = argv[argv.index("-protocol_blacklist") + 1]
    for protocol in ("http", "https", "tcp", "tls", "udp", "rtmp", "rtsp", "concat"):
        assert protocol in blacklist
    assert "-sn" in argv
    assert "-dn" in argv
    assert (
        argv[argv.index("-map_metadata")],
        argv[argv.index("-map_metadata") + 1],
    ) == ("-map_metadata", "-1")
    assert (argv[argv.index("-ac")], argv[argv.index("-ac") + 1]) == ("-ac", "1")
    assert (argv[argv.index("-ar")], argv[argv.index("-ar") + 1]) == ("-ar", "16000")
    assert (
        argv[argv.index("-c:a")],
        argv[argv.index("-c:a") + 1],
    ) == ("-c:a", "pcm_s16le")
    assert timeout == 120
    assert verifier.calls == ["/media/output/request-01.wav"]
    assert normalized.media_type == "audio/wav"
    assert normalized.byte_size == 192_044
    assert normalized.content_sha256 == TEST_OUTPUT_SHA256
    assert normalized.duration_ms == 60_000
    assert normalized.codec == "pcm_s16le"
    assert normalized.channels == 1
    assert normalized.sample_rate == 16_000
    assert normalized.executable_name == FFMPEG_ARTIFACT_NAME
    assert normalized.executable_version == FFMPEG_VERSION
    assert normalized.executable_sha256 == TEST_FFMPEG_SHA256
    assert "/media/output/request-01.wav" not in repr(normalized)


def test_ffmpeg_without_bound_artifact_fails_before_runner_or_probe() -> None:
    runner = RecordingRunner()
    verifier = RecordingOutputVerifier()
    adapter = FFmpegAdapter(
        runner=runner,
        output_verifier=verifier,
        artifact_identity=None,
    )

    with pytest.raises(FFmpegRejected, match="artifact_identity_unbound"):
        adapter.normalize(_request(), idempotency_key="normalize:01")

    assert runner.calls == []
    assert verifier.calls == []


@pytest.mark.parametrize(
    "identity",
    (
        _identity(name="not-ffmpeg"),
        _identity(version="runtime-selected"),
        _identity(read_only_path="/tmp/ffmpeg-SENTINEL"),
        _identity(read_only=False),
        _identity(sha256="not-a-digest"),
        _identity(sha256="d" * 64),
        _identity(sha256=hashlib.sha256(b"").hexdigest()),
    ),
)
def test_ffmpeg_rejects_wrong_or_placeholder_artifact_identity(
    identity: ArtifactIdentity,
) -> None:
    runner = RecordingRunner()
    verifier = RecordingOutputVerifier()
    adapter = FFmpegAdapter(
        runner=runner,
        output_verifier=verifier,
        artifact_identity=identity,
    )

    with pytest.raises(FFmpegRejected, match="artifact_identity_invalid") as caught:
        adapter.normalize(_request(), idempotency_key="normalize:01")

    assert runner.calls == []
    assert verifier.calls == []
    assert "/tmp/ffmpeg-SENTINEL" not in repr(identity)
    assert "/tmp/ffmpeg-SENTINEL" not in repr(caught.value)


@pytest.mark.parametrize(
    "source_path",
    (
        "https://attacker.invalid/audio",
        "file:///etc/passwd",
        "/media/input/../etc/passwd",
        "/media/input/object-01;touch-pwn",
        "/media/input/object-01\n-f concat",
    ),
)
def test_ffmpeg_rejects_protocol_path_and_argv_injection_before_runner(
    source_path: str,
) -> None:
    adapter, runner, verifier = _adapter()

    with pytest.raises(FFmpegRejected, match="unsafe_source_path"):
        adapter.normalize(
            _request(source_path=source_path),
            idempotency_key="normalize:01",
        )

    assert runner.calls == []
    assert verifier.calls == []


@pytest.mark.parametrize(
    "output_path",
    (
        "/tmp/result.wav",
        "/media/output/../escape.wav",
        "/media/output/result.mp3",
        "/media/output/-result.wav",
        "/media/output/result.wav;touch-pwn",
    ),
)
def test_ffmpeg_rejects_unsafe_output_before_runner(output_path: str) -> None:
    adapter, runner, verifier = _adapter()

    with pytest.raises(FFmpegRejected, match="unsafe_output_path"):
        adapter.normalize(
            _request(output_path=output_path),
            idempotency_key="normalize:01",
        )

    assert runner.calls == []
    assert verifier.calls == []


@pytest.mark.parametrize(
    ("changes", "code"),
    (
        ({"duration_ms": 0}, "duration_out_of_bounds"),
        ({"duration_ms": 7_200_001}, "duration_out_of_bounds"),
        ({"channels": 0}, "channels_out_of_bounds"),
        ({"channels": 9}, "channels_out_of_bounds"),
        ({"sample_rate": 7_999}, "sample_rate_out_of_bounds"),
        ({"sample_rate": 192_001}, "sample_rate_out_of_bounds"),
    ),
)
def test_ffmpeg_enforces_resource_and_media_bounds(
    changes: dict[str, object],
    code: str,
) -> None:
    adapter, runner, verifier = _adapter()

    with pytest.raises(FFmpegRejected, match=code):
        adapter.normalize(
            _request(**changes),
            idempotency_key="normalize:01",
        )

    assert runner.calls == []
    assert verifier.calls == []


def test_ffmpeg_failure_never_probes_or_echoes_stderr() -> None:
    runner = RecordingRunner(
        FFmpegRunResult(
            returncode=1,
            stdout="",
            stderr="stderr-SENTINEL with source metadata",
        )
    )
    adapter, _runner, verifier = _adapter(runner=runner)

    with pytest.raises(FFmpegRejected, match="ffmpeg_failed") as caught:
        adapter.normalize(_request(), idempotency_key="normalize:01")

    assert verifier.calls == []
    assert "stderr-SENTINEL" not in str(caught.value)
    assert "stderr-SENTINEL" not in repr(caught.value)
    assert "stderr-SENTINEL" not in repr(runner.result)


def test_ffmpeg_rejects_non_integer_runner_status_before_output_probe() -> None:
    runner = RecordingRunner(
        FFmpegRunResult(
            returncode=False,
            stdout="",
            stderr="runner-status-SENTINEL",
        )
    )
    adapter, _runner, verifier = _adapter(runner=runner)

    with pytest.raises(FFmpegRejected, match="ffmpeg_result_invalid") as caught:
        adapter.normalize(_request(), idempotency_key="normalize:01")

    assert verifier.calls == []
    assert "runner-status-SENTINEL" not in repr(caught.value)


def test_ffmpeg_runner_exception_is_generic_and_redacted() -> None:
    class FailingRunner:
        def run(self, argv: tuple[str, ...], *, timeout_seconds: int) -> FFmpegRunResult:
            del argv, timeout_seconds
            raise RuntimeError("runner-SENTINEL")

    verifier = RecordingOutputVerifier()
    adapter = FFmpegAdapter(
        runner=FailingRunner(),
        output_verifier=verifier,
        artifact_identity=_identity(),
    )

    with pytest.raises(FFmpegRejected, match="ffmpeg_unavailable") as caught:
        adapter.normalize(_request(), idempotency_key="normalize:01")

    assert verifier.calls == []
    assert "runner-SENTINEL" not in repr(caught.value)


def test_ffmpeg_output_verifier_exception_is_generic_and_redacted() -> None:
    verifier = RecordingOutputVerifier(failure=RuntimeError("/media/output/private-SENTINEL.wav"))
    adapter, _runner, _verifier = _adapter(output_verifier=verifier)

    with pytest.raises(
        FFmpegRejected,
        match="ffmpeg_output_verification_unavailable",
    ) as caught:
        adapter.normalize(_request(), idempotency_key="normalize:01")

    assert "/media/output/private-SENTINEL.wav" not in repr(caught.value)


@pytest.mark.parametrize(
    "proof",
    (
        _proof(exists=False),
        _proof(byte_size=0),
        _proof(media_type="application/octet-stream"),
        _proof(codec="aac"),
        _proof(channels=2),
        _proof(sample_rate=48_000),
        _proof(duration_ms=60_000 + FFMPEG_DURATION_TOLERANCE_MS + 1),
        _proof(content_sha256="not-a-digest"),
        _proof(content_sha256="e" * 64),
    ),
)
def test_ffmpeg_success_without_complete_safe_output_proof_is_quarantined(
    proof: FFmpegOutputProof,
) -> None:
    verifier = RecordingOutputVerifier(proof)
    adapter, runner, _verifier = _adapter(output_verifier=verifier)

    with pytest.raises(FFmpegRejected, match="ffmpeg_output_invalid") as caught:
        adapter.normalize(_request(), idempotency_key="normalize:01")

    assert len(runner.calls) == 1
    assert caught.value.retryable is False
    assert "/media/output/request-01.wav" not in repr(caught.value)


def test_empty_ffmpeg_idempotency_key_is_rejected_before_dependencies() -> None:
    adapter, runner, verifier = _adapter()

    with pytest.raises(FFmpegRejected, match="idempotency_key_required"):
        adapter.normalize(_request(), idempotency_key="")

    assert runner.calls == []
    assert verifier.calls == []
