from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock

from .models import TenantScope

_NONCE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


class AuthenticationError(ValueError):
    """The local service request did not authenticate."""


class NonceReplayError(AuthenticationError):
    """A valid local request nonce was already consumed."""


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _body_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _signature_payload(
    *,
    identity: str,
    method: str,
    path: str,
    timestamp: datetime,
    nonce: str,
    scope: TenantScope,
    body_sha256: str,
) -> bytes:
    fields = (
        "gbos-observer-local-hmac-v1",
        identity,
        method.upper(),
        path,
        _timestamp(timestamp),
        nonce,
        scope.site_id,
        scope.processing_purpose,
        body_sha256,
    )
    return "\n".join(fields).encode()


@dataclass(frozen=True, slots=True)
class SignedServiceRequest:
    identity: str
    method: str
    path: str
    timestamp: datetime
    nonce: str
    scope: TenantScope
    body_sha256: str
    signature: str


class NonceStore:
    """Process-local replay protection for deterministic tests."""

    def __init__(self) -> None:
        self._seen: set[tuple[str, str, str]] = set()
        self._lock = Lock()

    def consume(self, identity: str, site_id: str, nonce: str) -> None:
        key = (identity, site_id, nonce)
        with self._lock:
            if key in self._seen:
                raise NonceReplayError("nonce replay rejected")
            self._seen.add(key)


class HMACServiceIdentity:
    def __init__(self, identity: str, secret: bytes) -> None:
        if not identity or not secret:
            raise ValueError("identity and secret are required")
        self._identity = identity
        self._secret = bytes(secret)

    def sign(
        self,
        *,
        method: str,
        path: str,
        timestamp: datetime,
        nonce: str,
        scope: TenantScope,
        body: bytes,
    ) -> SignedServiceRequest:
        if not _NONCE.fullmatch(nonce):
            raise ValueError("invalid nonce")
        digest = _body_sha256(body)
        payload = _signature_payload(
            identity=self._identity,
            method=method,
            path=path,
            timestamp=timestamp,
            nonce=nonce,
            scope=scope,
            body_sha256=digest,
        )
        signature = hmac.new(self._secret, payload, hashlib.sha256).hexdigest()
        return SignedServiceRequest(
            identity=self._identity,
            method=method.upper(),
            path=path,
            timestamp=timestamp,
            nonce=nonce,
            scope=scope,
            body_sha256=digest,
            signature=signature,
        )


class LocalRequestAuthenticator:
    def __init__(
        self,
        *,
        identity: str,
        secret: bytes,
        nonce_store: NonceStore,
        clock: Callable[[], datetime],
        max_clock_skew: timedelta = timedelta(minutes=5),
    ) -> None:
        if not identity or not secret:
            raise ValueError("identity and secret are required")
        if max_clock_skew < timedelta(0):
            raise ValueError("max_clock_skew cannot be negative")
        self._identity = identity
        self._secret = bytes(secret)
        self._nonce_store = nonce_store
        self._clock = clock
        self._max_clock_skew = max_clock_skew

    def authenticate(self, request: SignedServiceRequest, body: bytes) -> TenantScope:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise RuntimeError("authentication clock must be timezone-aware")
        if request.timestamp.tzinfo is None or request.timestamp.utcoffset() is None:
            raise AuthenticationError("invalid request timestamp")
        if abs(now - request.timestamp) > self._max_clock_skew:
            raise AuthenticationError("request timestamp outside allowed window")
        if request.identity != self._identity:
            raise AuthenticationError("unknown local service identity")
        if not _NONCE.fullmatch(request.nonce):
            raise AuthenticationError("invalid request nonce")

        actual_digest = _body_sha256(body)
        if not hmac.compare_digest(actual_digest, request.body_sha256):
            raise AuthenticationError("body digest mismatch")
        payload = _signature_payload(
            identity=request.identity,
            method=request.method,
            path=request.path,
            timestamp=request.timestamp,
            nonce=request.nonce,
            scope=request.scope,
            body_sha256=request.body_sha256,
        )
        expected = hmac.new(self._secret, payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, request.signature):
            raise AuthenticationError("invalid request signature")

        self._nonce_store.consume(request.identity, request.scope.site_id, request.nonce)
        return request.scope
