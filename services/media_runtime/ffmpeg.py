from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Protocol

FFMPEG_EXECUTABLE_REF = "/opt/gbos/bin/ffmpeg"
FFMPEG_VERSION = "ffmpeg-7.1.1-gbos-local-v1"
FFMPEG_EXECUTABLE_SHA256 = "d" * 64

_SAFE_SOURCE_ROOT = PurePosixPath("/media/input")
_SAFE_OUTPUT_ROOT = PurePosixPath("/media/output")
_SAFE_SOURCE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_SAFE_OUTPUT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}\.wav$")
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
class NormalizedAudio:
    audio_ref: str
    media_type: str
    duration_ms: int
    codec: str
    channels: int
    sample_rate: int
    executable_version: str
    executable_sha256: str


class ArgvRunner(Protocol):
    def run(self, argv: tuple[str, ...], *, timeout_seconds: int) -> FFmpegRunResult: ...


class FFmpegAdapter:
    def __init__(self, *, runner: ArgvRunner) -> None:
        self._runner = runner

    def normalize(
        self,
        request: FFmpegRequest,
        *,
        idempotency_key: str,
    ) -> NormalizedAudio:
        if not idempotency_key:
            raise FFmpegRejected("idempotency_key_required")
        self._validate(request)
        argv = self._argv(request)
        try:
            result = self._runner.run(argv, timeout_seconds=120)
        except Exception:
            raise FFmpegRejected("ffmpeg_unavailable", retryable=True) from None
        if result.returncode != 0:
            raise FFmpegRejected("ffmpeg_failed", retryable=True)
        return NormalizedAudio(
            audio_ref=request.output_path,
            media_type="audio/wav",
            duration_ms=request.duration_ms,
            codec="pcm_s16le",
            channels=1,
            sample_rate=16_000,
            executable_version=FFMPEG_VERSION,
            executable_sha256=FFMPEG_EXECUTABLE_SHA256,
        )

    def _validate(self, request: FFmpegRequest) -> None:
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

    def _argv(self, request: FFmpegRequest) -> tuple[str, ...]:
        return (
            FFMPEG_EXECUTABLE_REF,
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


def _safe_path(value: str, root: PurePosixPath, pattern: re.Pattern[str]) -> bool:
    if "\x00" in value or "\n" in value or "\r" in value:
        return False
    path = PurePosixPath(value)
    return path.parent == root and pattern.fullmatch(path.name) is not None
