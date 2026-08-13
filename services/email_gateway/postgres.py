from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Protocol

from .models import TenantScope, ValidationError


class Cursor(Protocol):
    def execute(self, query: str, params: tuple[object, ...] = ()) -> Any: ...

    def fetchone(self) -> tuple[Any, ...] | None: ...

    def fetchall(self) -> list[tuple[Any, ...]]: ...


class Connection(Protocol):
    def cursor(self) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


RestrictedTextEncryptor = Callable[[str], bytes]
RestrictedTextDecryptor = Callable[[bytes], str]


@contextmanager
def site_transaction(connection: Connection, scope: TenantScope) -> Iterator[Cursor]:
    """Open one transaction with exact site/purpose RLS settings."""

    cursor = connection.cursor()
    try:
        cursor.execute("BEGIN")
        cursor.execute("SELECT set_config('gbos.site_id', %s, true)", (scope.site_id,))
        cursor.execute(
            "SELECT set_config('gbos.processing_purpose', %s, true)",
            (scope.processing_purpose,),
        )
        yield cursor
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        close = getattr(cursor, "close", None)
        if callable(close):
            close()


@contextmanager
def redacted_database_errors() -> Iterator[None]:
    """Preserve closed domain errors while hiding driver details and parameters."""

    try:
        yield
    except ValidationError:
        raise
    except Exception:
        raise ValidationError("email gateway persistence operation rejected") from None


def encrypt_restricted_text(encryptor: RestrictedTextEncryptor, value: str) -> bytes:
    try:
        encrypted = encryptor(value)
    except Exception:
        raise ValidationError("restricted text protection failed") from None
    if not isinstance(encrypted, bytes) or not encrypted or len(encrypted) > 2048:
        raise ValidationError("restricted text protection failed")
    return encrypted


def decrypt_restricted_text(decryptor: RestrictedTextDecryptor, value: Any) -> str:
    try:
        cleartext = decryptor(bytes(value))
    except Exception:
        raise ValidationError("restricted text reveal failed") from None
    if not isinstance(cleartext, str) or not cleartext:
        raise ValidationError("restricted text reveal failed")
    return cleartext


def require_database_role(actual_role: str, expected_role: str) -> None:
    if actual_role != expected_role:
        raise ValidationError("database role binding rejected")
