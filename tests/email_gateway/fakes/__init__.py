"""Test-only Email Gateway outbound fakes."""

from .provider import FakeEmailProvider, authority_for, closed_command

__all__ = ["FakeEmailProvider", "authority_for", "closed_command"]
