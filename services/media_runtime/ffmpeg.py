from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Protocol

FFMPEG_ARTIFACT_NAME = "ffmpeg"
FFMPEG_EXECUTABLE_REF = "/opt/gbos/bin/ffmpeg"
FFMPEG_VERSION = "ffmpeg-7.1.1-gbos-local-v1"
FFMPEG_DURATION_TOLERANCE_MS = 50

_SAFE_SOURCE_ROOT = PurePosixPath("/media/input")
_SAFE_OUTPUT_ROOT = PurePosixPath("/media/output")
_SAFE_SOURCE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_SAFE_OUTPUT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}\.wav$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_NETWORK_PROTOCOLS = (
    "async,bluray,cache,concat,concatf,crypto,data,ftp,gopher,hls,http,https,"
    "icecast,mmsh,mmst,pipe,rtmp,rtmps,rtp,rtsp,sctp,sftp,srt,tcp,tls,udp,unix"
)


class FFmpegRejected(ValueError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.code!r}, retryable={self.retryable!r})"


@dataclass(frozen=True, slots=True)
class ArtifactIdentity:
    name: str
    version: str
    read_only_path: str = field(repr=False)
    sha256: str
    read_only: bool


@dataclass(frozen=True, slots=True)
class FFmpegRequest:
    source_path: str = field(repr=False)
    output_path: str = field(repr=False)
    duration_ms: int
    channels: int
    sample_rate: int


@dataclass(frozen=True, slots=True)
class FFmpegRunResult:
    returncode: int
    stdout: str = field(repr=False)
    stderr: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class FFmpegOutputProof:
    exists: bool
    byte_size: int
    media_type: str
    codec: str
    channels: int
    sample_rate: int
    duration_ms: int
    content_sha256: str


@dataclass(frozen=True, slots=True)
class NormalizedAudio:
    audio_ref: str = field(repr=False)
    media_type: str
    byte_size: int
    content_sha256: str
    duration_ms: int
    codec: str
    channels: int
    sample_rate: int
    executable_name: str
    executable_version: str
    executable_sha256: str


class ArgvRunner(Protocol):
    def run(self, argv: tuple[str, ...], *, timeout_seconds: int) -> FFmpegRunResult: ...


class FFmpegOutputVerifier(Protocol):
    def verify(self, output_path: str) -> FFmpegOutputProof: ...


class FFmpegAdapter:
    def __init__(
        self,
        *,
        runner: ArgvRunner,
        output_verifier: FFmpegOutputVerifier,
        artifact_identity: ArtifactIdentity | None,
    ) -> None:
        self._runner = runner
        self._output_verifier = output_verifier
        self._artifact_identity = artifact_identity

    def normalize(
        self,
        request: FFmpegRequest,
        *,
        idempotency_key: str,
    ) -> NormalizedAudio:
        if not idempotency_key:
            raise FFmpegRejected("idempotency_key_required")
        self._validate_request(request)
        identity = self._bound_identity()
        argv = self._argv(request, executable_path=identity.read_only_path)
        try:
            result = self._runner.run(argv, timeout_seconds=120)
        except Exception:
            raise FFmpegRejected("ffmpeg_unavailable", retryable=True) from None
        if not isinstance(result, FFmpegRunResult) or type(result.returncode) is not int:
            raise FFmpegRejected("ffmpeg_result_invalid", retryable=True)
        if result.returncode != 0:
            raise FFmpegRejected("ffmpeg_failed", retryable=True)
        try:
            proof = self._output_verifier.verify(request.output_path)
        except Exception:
            raise FFmpegRejected(
                "ffmpeg_output_verification_unavailable",
                retryable=True,
            ) from None
        if not self._valid_output(proof, expected_duration_ms=request.duration_ms):
            raise FFmpegRejected("ffmpeg_output_invalid")
        return NormalizedAudio(
            audio_ref=request.output_path,
            media_type=proof.media_type,
            byte_size=proof.byte_size,
            content_sha256=proof.content_sha256,
            duration_ms=proof.duration_ms,
            codec=proof.codec,
            channels=proof.channels,
            sample_rate=proof.sample_rate,
            executable_name=identity.name,
            executable_version=identity.version,
            executable_sha256=identity.sha256,
        )

    def _bound_identity(self) -> ArtifactIdentity:
        identity = self._artifact_identity
        if identity is None:
            raise FFmpegRejected("artifact_identity_unbound")
        if not valid_artifact_identity(
            identity,
            expected_name=FFMPEG_ARTIFACT_NAME,
            expected_version=FFMPEG_VERSION,
            expected_path=FFMPEG_EXECUTABLE_REF,
        ):
            raise FFmpegRejected("artifact_identity_invalid")
        return identity

    def _validate_request(self, request: FFmpegRequest) -> None:
        if not _safe_path(request.source_path, _SAFE_SOURCE_ROOT, _SAFE_SOURCE_NAME):
            raise FFmpegRejected("unsafe_source_path")
        if not _safe_path(request.output_path, _SAFE_OUTPUT_ROOT, _SAFE_OUTPUT_NAME):
            raise FFmpegRejected("unsafe_output_path")
        if not 1 <= request.duration_ms <= 7_200_000:
            raise FFmpegRejected("duration_out_of_bounds")
        if not 1 <= request.channels <= 8:
            raise FFmpegRejected("channels_out_of_bounds")
        if not 8_000 <= request.sample_rate <= 192_000:
            raise FFmpegRejected("sample_rate_out_of_bounds")

    @staticmethod
    def _valid_output(proof: object, *, expected_duration_ms: int) -> bool:
        if not isinstance(proof, FFmpegOutputProof):
            return False
        return (
            proof.exists is True
            and type(proof.byte_size) is int
            and proof.byte_size > 0
            and proof.media_type == "audio/wav"
            and proof.codec == "pcm_s16le"
            and proof.channels == 1
            and proof.sample_rate == 16_000
            and type(proof.duration_ms) is int
            and abs(proof.duration_ms - expected_duration_ms) <= FFMPEG_DURATION_TOLERANCE_MS
            and valid_sha256(proof.content_sha256)
        )

    @staticmethod
    def _argv(
        request: FFmpegRequest,
        *,
        executable_path: str,
    ) -> tuple[str, ...]:
        return (
            executable_path,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "file",
            "-protocol_blacklist",
            _NETWORK_PROTOCOLS,
            "-safe",
            "1",
            "-i",
            request.source_path,
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-threads",
            "1",
            "-t",
            f"{request.duration_ms / 1000:.3f}",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            "-y",
            request.output_path,
        )


def valid_artifact_identity(
    identity: object,
    *,
    expected_name: str,
    expected_version: str,
    expected_path: str,
) -> bool:
    return (
        isinstance(identity, ArtifactIdentity)
        and identity.name == expected_name
        and identity.version == expected_version
        and identity.read_only_path == expected_path
        and identity.read_only is True
        and valid_sha256(identity.sha256)
    )


def valid_sha256(value: object) -> bool:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        return False
    if value == _EMPTY_SHA256:
        return False
    return all(value != value[:period] * (64 // period) for period in (1, 2, 4, 8, 16, 32))


def _safe_path(value: str, root: PurePosixPath, pattern: re.Pattern[str]) -> bool:
    if "\x00" in value or "\n" in value or "\r" in value:
        return False
    path = PurePosixPath(value)
    return path.parent == root and pattern.fullmatch(path.name) is not None
