from __future__ import annotations

import pytest

from services.media_runtime.ffmpeg import (
    FFMPEG_EXECUTABLE_REF,
    FFMPEG_EXECUTABLE_SHA256,
    FFMPEG_VERSION,
    FFmpegAdapter,
    FFmpegRejected,
    FFmpegRequest,
    FFmpegRunResult,
)


class RecordingRunner:
    def __init__(self, result: FFmpegRunResult | None = None) -> None:
        self.result = result or FFmpegRunResult(returncode=0, stdout="", stderr="")
        self.calls: list[tuple[tuple[str, ...], int]] = []

    def run(self, argv: tuple[str, ...], *, timeout_seconds: int) -> FFmpegRunResult:
        self.calls.append((argv, timeout_seconds))
        return self.result


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


def test_ffmpeg_uses_fixed_identity_argv_only_and_safe_audio_output() -> None:
    runner = RecordingRunner()
    adapter = FFmpegAdapter(runner=runner)

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
    assert normalized.media_type == "audio/wav"
    assert normalized.codec == "pcm_s16le"
    assert normalized.channels == 1
    assert normalized.sample_rate == 16_000
    assert normalized.executable_version == FFMPEG_VERSION
    assert normalized.executable_sha256 == FFMPEG_EXECUTABLE_SHA256


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
    runner = RecordingRunner()

    with pytest.raises(FFmpegRejected, match="unsafe_source_path"):
        FFmpegAdapter(runner=runner).normalize(
            _request(source_path=source_path),
            idempotency_key="normalize:01",
        )

    assert runner.calls == []


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
    runner = RecordingRunner()

    with pytest.raises(FFmpegRejected, match="unsafe_output_path"):
        FFmpegAdapter(runner=runner).normalize(
            _request(output_path=output_path),
            idempotency_key="normalize:01",
        )

    assert runner.calls == []


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
    runner = RecordingRunner()

    with pytest.raises(FFmpegRejected, match=code):
        FFmpegAdapter(runner=runner).normalize(
            _request(**changes),
            idempotency_key="normalize:01",
        )

    assert runner.calls == []


def test_ffmpeg_failure_never_echoes_stderr() -> None:
    runner = RecordingRunner(
        FFmpegRunResult(
            returncode=1,
            stdout="",
            stderr="stderr-SENTINEL with source metadata",
        )
    )

    with pytest.raises(FFmpegRejected, match="ffmpeg_failed") as caught:
        FFmpegAdapter(runner=runner).normalize(
            _request(),
            idempotency_key="normalize:01",
        )

    assert "stderr-SENTINEL" not in str(caught.value)
    assert "stderr-SENTINEL" not in repr(caught.value)
    assert "stderr-SENTINEL" not in repr(runner.result)


def test_ffmpeg_runner_exception_is_generic_and_redacted() -> None:
    class FailingRunner:
        def run(self, argv: tuple[str, ...], *, timeout_seconds: int) -> FFmpegRunResult:
            del argv, timeout_seconds
            raise RuntimeError("runner-SENTINEL")

    with pytest.raises(FFmpegRejected, match="ffmpeg_unavailable") as caught:
        FFmpegAdapter(runner=FailingRunner()).normalize(
            _request(),
            idempotency_key="normalize:01",
        )

    assert "runner-SENTINEL" not in repr(caught.value)


def test_empty_ffmpeg_idempotency_key_is_rejected_before_runner() -> None:
    runner = RecordingRunner()

    with pytest.raises(FFmpegRejected, match="idempotency_key_required"):
        FFmpegAdapter(runner=runner).normalize(_request(), idempotency_key="")

    assert runner.calls == []
