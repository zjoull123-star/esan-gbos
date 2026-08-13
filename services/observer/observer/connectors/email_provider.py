"""Provider-neutral inbound email adapter boundary."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from ..models import RawDelivery

_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")


class EmailProviderError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = True) -> None:
        if _SAFE_CODE.fullmatch(code) is None:
            raise ValueError("invalid email provider error code")
        self.code = code
        self.retryable = retryable
        super().__init__(code)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, retryable={self.retryable!r})"


@dataclass(frozen=True, slots=True)
class EmailProviderPollResult:
    expected_cursor: str | None
    candidate_cursor: str | None
    deliveries: tuple[RawDelivery, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.deliveries, tuple) or not all(
            isinstance(value, RawDelivery) for value in self.deliveries
        ):
            raise TypeError("email provider deliveries must be a tuple")
        if self.deliveries and self.candidate_cursor is None:
            raise ValueError("email provider deliveries require a candidate cursor")


class EmailProvider(Protocol):
    """Future IMAP and WeCom application-mail adapters share this exact seam."""

    @property
    def provider_kind(self) -> str: ...

    def poll(self, checkpoint: str | None, limit: int) -> EmailProviderPollResult: ...


__all__ = ["EmailProvider", "EmailProviderError", "EmailProviderPollResult"]
