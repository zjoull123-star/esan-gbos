"""Read-only IMAP checkpoint initialization with a closed, secret-safe boundary."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import imaplib
import json
import os
import re
import ssl
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

_EMAIL_FIELDS = frozenset(
    {
        "instance_id",
        "team_ref",
        "agent_task_type",
        "account_user_ref",
        "host",
        "port",
        "mailbox",
        "folder",
        "username",
        "password",
        "poll_limit",
        "max_message_bytes",
        "max_attachment_bytes",
        "max_attachments",
        "rescan_max_window_seconds",
        "rescan_max_uids",
        "initial_checkpoint",
    }
)
_CHECKPOINT_FIELDS = frozenset({"mailbox", "uid", "uidvalidity", "version"})
_TASK_TYPES = frozenset({"sales", "purchase", "product_sample", "ceo"})
_MAX_CREDENTIAL_BYTES = 65_536
_BINDING_KEY_BYTES = 32
_MAX_IMAP_VALUE = 4_294_967_295
_TIMEOUT_SECONDS = 10.0
_OUTPUT_NAME = "email-checkpoint.json"
_RECEIPT_NAME = "email-checkpoint-receipt.json"
_RECEIPT_SCHEMA = "gbos.email_checkpoint_receipt"
_RECEIPT_VERSION = 1
_OPERATION = "STATUS_UIDVALIDITY_UIDNEXT"
_MAX_OUTPUT_BYTES = 8_192
_COMMIT = re.compile(r"[0-9a-f]{40}")
_STATUS_VALUE = re.compile(rb"(?:^|[\s(])([A-Z]+)\s+([0-9]+)(?=[\s)])", re.IGNORECASE)


class EmailCheckpointProbeError(RuntimeError):
    """The checkpoint probe failed without exposing provider or mailbox values."""


@dataclass(frozen=True, slots=True, repr=False)
class EmailCheckpoint:
    """The exact closed v1 checkpoint persisted by the probe."""

    mailbox: str
    uid: int
    uidvalidity: int
    version: int = 1

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(mailbox=<redacted>, uid={self.uid}, "
            f"uidvalidity={self.uidvalidity}, version={self.version})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class _EmailCredential:
    instance_id: str
    team_ref: str | None
    agent_task_type: str | None
    account_user_ref: str | None
    host: str
    port: int
    mailbox: str
    folder: str
    username: str
    password: str

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"


class _ImapClient(Protocol):
    def login(self, username: str, password: str) -> tuple[Any, Any]: ...

    def status(self, mailbox: str, names: str) -> tuple[Any, Any]: ...

    def logout(self) -> tuple[Any, Any]: ...


class ImapClientFactory(Protocol):
    def __call__(
        self,
        *,
        host: str,
        port: int,
        ssl_context: ssl.SSLContext,
        timeout: float,
    ) -> _ImapClient: ...


def probe_email_checkpoint(
    credential_file: Path,
    output_dir: Path,
    *,
    binding_key_file: Path,
    repo_root: Path,
    client_factory: ImapClientFactory | None = None,
    activation_time: datetime | str | None = None,
    now: Callable[[], datetime] | None = None,
) -> EmailCheckpoint:
    """Read UID metadata and publish a checkpoint before its closed receipt."""

    try:
        root = _real_directory(Path(repo_root))
        clock = _system_utc_now if now is None else now
        started_at = _utc_now(clock)
        activation = _activation_timestamp(activation_time)
        if activation > started_at:
            raise EmailCheckpointProbeError("email checkpoint activation is invalid")
        credential_path = _external_file(Path(credential_file), root)
        credential = _load_credential(credential_path)
        binding_key_path = _external_binding_key_file(Path(binding_key_file), root)
        binding_key = _read_binding_key(binding_key_path)
        credential_binding = _credential_binding_hmac_sha256(credential, binding_key)
        source_commit = _source_commit(root)
        output = _private_external_output(Path(output_dir), root)
        destination = output / _OUTPUT_NAME
        receipt_destination = output / _RECEIPT_NAME
        checkpoint_exists = _safe_destination_exists(destination)
        receipt_exists = _safe_destination_exists(receipt_destination)
        if receipt_exists and not checkpoint_exists:
            raise EmailCheckpointProbeError("email checkpoint output conflicts")

        existing_checkpoint_bytes: bytes | None = None
        existing_checkpoint: EmailCheckpoint | None = None
        if checkpoint_exists:
            existing_checkpoint_bytes = _read_private_output(destination)
            existing_checkpoint = _checkpoint_from_bytes(existing_checkpoint_bytes)
            if existing_checkpoint.mailbox != credential.mailbox:
                raise EmailCheckpointProbeError("email checkpoint output conflicts")

        if receipt_exists:
            if existing_checkpoint_bytes is None or existing_checkpoint is None:
                raise EmailCheckpointProbeError("email checkpoint output conflicts")
            receipt_bytes = _read_private_output(receipt_destination)
            _validate_receipt(
                receipt_bytes,
                activation=activation,
                checkpoint_bytes=existing_checkpoint_bytes,
                credential_binding=credential_binding,
                source_commit=source_commit,
            )
            return existing_checkpoint

        context = _verified_tls_context()
        factory = _default_client_factory if client_factory is None else client_factory
        checkpoint = _probe_status(credential, context=context, factory=factory)
        checkpoint_bytes = _checkpoint_bytes(checkpoint)
        if existing_checkpoint_bytes is not None and existing_checkpoint_bytes != checkpoint_bytes:
            raise EmailCheckpointProbeError("email checkpoint output conflicts")

        observed_at = _utc_now(clock)
        if observed_at < activation:
            raise EmailCheckpointProbeError("email checkpoint activation is invalid")
        receipt_bytes = _receipt_bytes(
            activation=activation,
            checkpoint_bytes=checkpoint_bytes,
            credential_binding=credential_binding,
            observed_at=observed_at,
            source_commit=source_commit,
        )
        checkpoint_published = False
        receipt_published = False
        try:
            if existing_checkpoint_bytes is None:
                _publish_new_private_bytes(destination, checkpoint_bytes)
                checkpoint_published = True
            if _read_private_output(destination) != checkpoint_bytes:
                raise EmailCheckpointProbeError("email checkpoint output conflicts")
            _publish_new_private_bytes(receipt_destination, receipt_bytes)
            receipt_published = True
            if (
                _read_private_output(destination) != checkpoint_bytes
                or _read_private_output(receipt_destination) != receipt_bytes
            ):
                raise EmailCheckpointProbeError("email checkpoint output conflicts")
        except Exception:
            if receipt_published:
                _remove_publication(receipt_destination)
            if (checkpoint_published or existing_checkpoint_bytes is None) and not _path_lexists(
                receipt_destination
            ):
                _remove_publication(destination)
            raise
        return checkpoint
    except EmailCheckpointProbeError:
        raise
    except Exception:
        raise EmailCheckpointProbeError("email checkpoint probe failed closed") from None


def _probe_status(
    credential: _EmailCredential,
    *,
    context: ssl.SSLContext,
    factory: ImapClientFactory,
) -> EmailCheckpoint:
    try:
        client = factory(
            host=credential.host,
            port=credential.port,
            ssl_context=context,
            timeout=_TIMEOUT_SECONDS,
        )
    except Exception:
        raise EmailCheckpointProbeError("email checkpoint probe failed closed") from None
    try:
        try:
            login_status, _ = client.login(credential.username, credential.password)
            if not _is_ok(login_status):
                raise EmailCheckpointProbeError("email checkpoint probe failed closed")
            status_code, status_data = client.status(
                credential.folder,
                "(UIDVALIDITY UIDNEXT)",
            )
            if not _is_ok(status_code):
                raise EmailCheckpointProbeError("email checkpoint probe failed closed")
            uidvalidity, uid_next = _status_checkpoint_values(status_data)
            return EmailCheckpoint(
                mailbox=credential.mailbox,
                uid=uid_next - 1,
                uidvalidity=uidvalidity,
            )
        except EmailCheckpointProbeError:
            raise
        except Exception:
            raise EmailCheckpointProbeError("email checkpoint probe failed closed") from None
    finally:
        with suppress(Exception):
            client.logout()


def _status_checkpoint_values(data: object) -> tuple[int, int]:
    if not isinstance(data, (list, tuple)) or not data:
        raise EmailCheckpointProbeError("email checkpoint probe failed closed")
    fragments: list[bytes] = []
    for item in data:
        if not isinstance(item, bytes) or len(item) > 4_096:
            raise EmailCheckpointProbeError("email checkpoint probe failed closed")
        fragments.append(item)
    values: dict[bytes, int] = {}
    for name, raw_value in _STATUS_VALUE.findall(b" ".join(fragments)):
        key = name.upper()
        if key in values:
            raise EmailCheckpointProbeError("email checkpoint probe failed closed")
        values[key] = int(raw_value)
    if set(values) & {b"UIDVALIDITY", b"UIDNEXT"} != {b"UIDVALIDITY", b"UIDNEXT"}:
        raise EmailCheckpointProbeError("email checkpoint probe failed closed")
    uidvalidity = values[b"UIDVALIDITY"]
    uid_next = values[b"UIDNEXT"]
    if not 1 <= uidvalidity <= _MAX_IMAP_VALUE or not 1 <= uid_next <= _MAX_IMAP_VALUE:
        raise EmailCheckpointProbeError("email checkpoint probe failed closed")
    return uidvalidity, uid_next


def _verified_tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def _default_client_factory(
    *,
    host: str,
    port: int,
    ssl_context: ssl.SSLContext,
    timeout: float,
) -> _ImapClient:
    return imaplib.IMAP4_SSL(
        host=host,
        port=port,
        ssl_context=ssl_context,
        timeout=timeout,
    )


def _load_credential(path: Path) -> _EmailCredential:
    value = _read_private_json(path)
    if set(value) != _EMAIL_FIELDS:
        raise EmailCheckpointProbeError("email checkpoint credential is invalid")
    instance_id = _text(value, "instance_id", maximum=256)
    team_ref = _optional_text(value.get("team_ref"), maximum=256)
    task = value.get("agent_task_type")
    if task is not None and (task not in _TASK_TYPES or team_ref is None):
        raise EmailCheckpointProbeError("email checkpoint credential is invalid")
    account_user_ref = _optional_text(value.get("account_user_ref"), maximum=256)
    if account_user_ref is not None and any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in account_user_ref
    ):
        raise EmailCheckpointProbeError("email checkpoint credential is invalid")
    host = _text(value, "host", maximum=253)
    port = _integer(value, "port", minimum=1, maximum=65_535)
    mailbox = _text(value, "mailbox", maximum=256)
    folder = _text(value, "folder", maximum=256)
    username = _text(value, "username", maximum=4_096)
    password = _text(value, "password", maximum=4_096)
    _integer(value, "poll_limit", minimum=1, maximum=1_000)
    _integer(value, "max_message_bytes", minimum=1, maximum=100_000_000)
    _integer(value, "max_attachment_bytes", minimum=1, maximum=100_000_000)
    _integer(value, "max_attachments", minimum=1, maximum=1_000)
    _integer(value, "rescan_max_window_seconds", minimum=1, maximum=90 * 86_400)
    _integer(value, "rescan_max_uids", minimum=1, maximum=10_000)
    _optional_checkpoint(value.get("initial_checkpoint"), mailbox=mailbox)
    return _EmailCredential(
        instance_id=instance_id,
        team_ref=team_ref,
        agent_task_type=task,
        account_user_ref=account_user_ref,
        host=host,
        port=port,
        mailbox=mailbox,
        folder=folder,
        username=username,
        password=password,
    )


def _optional_checkpoint(value: object, *, mailbox: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or len(value.encode("utf-8")) > 4_096:
        raise EmailCheckpointProbeError("email checkpoint credential is invalid")
    try:
        decoded = json.loads(value, object_pairs_hook=_unique_object)
    except UnicodeDecodeError, json.JSONDecodeError, ValueError:
        raise EmailCheckpointProbeError("email checkpoint credential is invalid") from None
    if (
        not isinstance(decoded, dict)
        or set(decoded) != _CHECKPOINT_FIELDS
        or decoded.get("mailbox") != mailbox
        or decoded.get("version") != 1
        or type(decoded.get("uid")) is not int
        or decoded["uid"] < 0
        or type(decoded.get("uidvalidity")) is not int
        or decoded["uidvalidity"] < 1
    ):
        raise EmailCheckpointProbeError("email checkpoint credential is invalid")


def _read_private_json(path: Path) -> dict[str, Any]:
    descriptor = -1
    try:
        before = path.lstat()
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        details = os.fstat(descriptor)
        if (
            stat.S_ISLNK(before.st_mode)
            or before.st_dev != details.st_dev
            or before.st_ino != details.st_ino
            or not stat.S_ISREG(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o600
            or not 0 < details.st_size <= _MAX_CREDENTIAL_BYTES
        ):
            raise EmailCheckpointProbeError("email checkpoint credential is invalid")
        chunks: list[bytes] = []
        remaining = details.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, _MAX_CREDENTIAL_BYTES + 1))
            if not chunk:
                raise EmailCheckpointProbeError("email checkpoint credential is invalid")
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(payload) != details.st_size
            or after.st_dev != details.st_dev
            or after.st_ino != details.st_ino
            or after.st_size != details.st_size
        ):
            raise EmailCheckpointProbeError("email checkpoint credential is invalid")
    except EmailCheckpointProbeError:
        raise
    except OSError:
        raise EmailCheckpointProbeError("email checkpoint credential is unavailable") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        decoded = json.loads(payload, object_pairs_hook=_unique_object)
    except UnicodeDecodeError, json.JSONDecodeError, ValueError:
        raise EmailCheckpointProbeError("email checkpoint credential is invalid") from None
    if not isinstance(decoded, dict):
        raise EmailCheckpointProbeError("email checkpoint credential is invalid")
    return decoded


def _read_binding_key(path: Path) -> bytes:
    descriptor = -1
    try:
        before = path.lstat()
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        details = os.fstat(descriptor)
        if (
            stat.S_ISLNK(before.st_mode)
            or before.st_dev != details.st_dev
            or before.st_ino != details.st_ino
            or not stat.S_ISREG(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_size != _BINDING_KEY_BYTES
        ):
            raise EmailCheckpointProbeError("email checkpoint binding key is invalid")
        chunks: list[bytes] = []
        remaining = _BINDING_KEY_BYTES
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                raise EmailCheckpointProbeError("email checkpoint binding key is invalid")
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(payload) != _BINDING_KEY_BYTES
            or after.st_dev != details.st_dev
            or after.st_ino != details.st_ino
            or after.st_size != details.st_size
        ):
            raise EmailCheckpointProbeError("email checkpoint binding key is invalid")
        return payload
    except EmailCheckpointProbeError:
        raise
    except OSError:
        raise EmailCheckpointProbeError("email checkpoint binding key is unavailable") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _text(value: dict[str, Any], key: str, *, maximum: int) -> str:
    item = value.get(key)
    if (
        not isinstance(item, str)
        or not item
        or item != item.strip()
        or len(item.encode("utf-8")) > maximum
        or any(character in item for character in ("\x00", "\r", "\n"))
    ):
        raise EmailCheckpointProbeError("email checkpoint credential is invalid")
    return item


def _optional_text(value: object, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _text({"value": value}, "value", maximum=maximum)


def _integer(value: dict[str, Any], key: str, *, minimum: int, maximum: int) -> int:
    item = value.get(key)
    if type(item) is not int or not minimum <= item <= maximum:
        raise EmailCheckpointProbeError("email checkpoint credential is invalid")
    return item


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _real_directory(path: Path) -> Path:
    try:
        details = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError:
        raise EmailCheckpointProbeError("repository boundary is unavailable") from None
    if path.is_symlink() or not stat.S_ISDIR(details.st_mode):
        raise EmailCheckpointProbeError("repository boundary is invalid")
    return resolved


def _system_utc_now() -> datetime:
    return datetime.now(UTC)


def _utc_now(clock: Callable[[], datetime]) -> datetime:
    try:
        value = clock()
    except Exception:
        raise EmailCheckpointProbeError("email checkpoint clock is invalid") from None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise EmailCheckpointProbeError("email checkpoint clock is invalid")
    return value.astimezone(UTC)


def _activation_timestamp(value: datetime | str | None) -> datetime:
    if isinstance(value, str):
        if not value or value != value.strip() or len(value) > 64:
            raise EmailCheckpointProbeError("email checkpoint activation is invalid")
        candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            value = datetime.fromisoformat(candidate)
        except ValueError:
            raise EmailCheckpointProbeError("email checkpoint activation is invalid") from None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise EmailCheckpointProbeError("email checkpoint activation is invalid")
    return value.astimezone(UTC)


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _source_commit(repo_root: Path) -> str:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "rev-parse",
                "--show-toplevel",
                "--verify",
                "HEAD^{commit}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if len(result.stdout) > 8_192:
            raise ValueError
        lines = result.stdout.splitlines()
        if (
            len(lines) != 2
            or Path(lines[0]).resolve(strict=True) != repo_root
            or _COMMIT.fullmatch(lines[1]) is None
        ):
            raise ValueError
        return lines[1]
    except OSError, subprocess.SubprocessError, ValueError:
        raise EmailCheckpointProbeError("repository source commit is unavailable") from None


def _external_file(path: Path, repo_root: Path) -> Path:
    try:
        details = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError:
        raise EmailCheckpointProbeError("email checkpoint credential is unavailable") from None
    if path.is_symlink() or not stat.S_ISREG(details.st_mode):
        raise EmailCheckpointProbeError("email checkpoint credential is invalid")
    _require_external(resolved, repo_root)
    return path.absolute()


def _external_binding_key_file(path: Path, repo_root: Path) -> Path:
    try:
        details = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError:
        raise EmailCheckpointProbeError("email checkpoint binding key is unavailable") from None
    if path.is_symlink() or not stat.S_ISREG(details.st_mode):
        raise EmailCheckpointProbeError("email checkpoint binding key is invalid")
    _require_external(resolved, repo_root)
    return path.absolute()


def _private_external_output(path: Path, repo_root: Path) -> Path:
    try:
        unresolved = path.resolve(strict=False)
        _require_external(unresolved, repo_root)
        if path.exists() or path.is_symlink():
            details = path.lstat()
            if (
                path.is_symlink()
                or not stat.S_ISDIR(details.st_mode)
                or stat.S_IMODE(details.st_mode) != 0o700
            ):
                raise EmailCheckpointProbeError("email checkpoint output is unsafe")
        else:
            path.mkdir(mode=0o700, parents=True)
            path.chmod(0o700)
        resolved = path.resolve(strict=True)
        details = resolved.lstat()
        _require_external(resolved, repo_root)
        if not stat.S_ISDIR(details.st_mode) or stat.S_IMODE(details.st_mode) != 0o700:
            raise EmailCheckpointProbeError("email checkpoint output is unsafe")
        return resolved
    except EmailCheckpointProbeError:
        raise
    except OSError:
        raise EmailCheckpointProbeError("email checkpoint output is unavailable") from None


def _require_external(path: Path, repo_root: Path) -> None:
    try:
        inside = os.path.commonpath((str(path), str(repo_root))) == str(repo_root)
    except ValueError:
        inside = False
    if inside:
        raise EmailCheckpointProbeError(
            "email checkpoint private material must be repository-external"
        )


def _safe_destination_exists(path: Path) -> bool:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise EmailCheckpointProbeError("email checkpoint output is unavailable") from None
    if (
        path.is_symlink()
        or not stat.S_ISREG(details.st_mode)
        or stat.S_IMODE(details.st_mode) != 0o600
    ):
        raise EmailCheckpointProbeError("email checkpoint output is unsafe")
    return True


def _path_lexists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _read_private_output(path: Path) -> bytes:
    descriptor = -1
    try:
        before = path.lstat()
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        details = os.fstat(descriptor)
        if (
            stat.S_ISLNK(before.st_mode)
            or before.st_dev != details.st_dev
            or before.st_ino != details.st_ino
            or not stat.S_ISREG(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o600
            or not 0 < details.st_size <= _MAX_OUTPUT_BYTES
        ):
            raise EmailCheckpointProbeError("email checkpoint output is unsafe")
        chunks: list[bytes] = []
        remaining = details.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, _MAX_OUTPUT_BYTES + 1))
            if not chunk:
                raise EmailCheckpointProbeError("email checkpoint output is unsafe")
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(payload) != details.st_size
            or after.st_dev != details.st_dev
            or after.st_ino != details.st_ino
            or after.st_size != details.st_size
        ):
            raise EmailCheckpointProbeError("email checkpoint output is unsafe")
        return payload
    except EmailCheckpointProbeError:
        raise
    except OSError:
        raise EmailCheckpointProbeError("email checkpoint output is unavailable") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _json_bytes(value: dict[str, object]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _checkpoint_bytes(checkpoint: EmailCheckpoint) -> bytes:
    return _json_bytes(
        {
            "mailbox": checkpoint.mailbox,
            "uid": checkpoint.uid,
            "uidvalidity": checkpoint.uidvalidity,
            "version": checkpoint.version,
        }
    )


def _checkpoint_from_bytes(payload: bytes) -> EmailCheckpoint:
    try:
        value = json.loads(payload, object_pairs_hook=_unique_object)
    except UnicodeDecodeError, json.JSONDecodeError, ValueError:
        raise EmailCheckpointProbeError("email checkpoint output conflicts") from None
    if (
        not isinstance(value, dict)
        or set(value) != _CHECKPOINT_FIELDS
        or not isinstance(value.get("mailbox"), str)
        or not value["mailbox"]
        or len(value["mailbox"].encode("utf-8")) > 256
        or type(value.get("uid")) is not int
        or not 0 <= value["uid"] < _MAX_IMAP_VALUE
        or type(value.get("uidvalidity")) is not int
        or not 1 <= value["uidvalidity"] <= _MAX_IMAP_VALUE
        or value.get("version") != 1
    ):
        raise EmailCheckpointProbeError("email checkpoint output conflicts")
    checkpoint = EmailCheckpoint(
        mailbox=value["mailbox"],
        uid=value["uid"],
        uidvalidity=value["uidvalidity"],
    )
    if _checkpoint_bytes(checkpoint) != payload:
        raise EmailCheckpointProbeError("email checkpoint output conflicts")
    return checkpoint


def _credential_binding_hmac_sha256(
    credential: _EmailCredential,
    binding_key: bytes,
) -> str:
    canonical_identity = _json_bytes(
        {
            "account_user_ref": credential.account_user_ref,
            "agent_task_type": credential.agent_task_type,
            "folder": credential.folder,
            "host": credential.host,
            "instance_id": credential.instance_id,
            "mailbox": credential.mailbox,
            "port": credential.port,
            "team_ref": credential.team_ref,
            "username": credential.username,
        }
    )
    return hmac.new(binding_key, canonical_identity, hashlib.sha256).hexdigest()


def _receipt_bytes(
    *,
    activation: datetime,
    checkpoint_bytes: bytes,
    credential_binding: str,
    observed_at: datetime,
    source_commit: str,
) -> bytes:
    return _json_bytes(
        {
            "activation_time": _format_utc(activation),
            "checkpoint_sha256": hashlib.sha256(checkpoint_bytes).hexdigest(),
            "credential_binding_hmac_sha256": credential_binding,
            "observed_at": _format_utc(observed_at),
            "operation": _OPERATION,
            "read_only": True,
            "schema": _RECEIPT_SCHEMA,
            "source_commit": source_commit,
            "version": _RECEIPT_VERSION,
        }
    )


def _receipt_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise EmailCheckpointProbeError("email checkpoint output conflicts")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise EmailCheckpointProbeError("email checkpoint output conflicts") from None
    if _format_utc(parsed) != value:
        raise EmailCheckpointProbeError("email checkpoint output conflicts")
    return parsed


def _validate_receipt(
    payload: bytes,
    *,
    activation: datetime,
    checkpoint_bytes: bytes,
    credential_binding: str,
    source_commit: str,
) -> None:
    try:
        value = json.loads(payload, object_pairs_hook=_unique_object)
    except UnicodeDecodeError, json.JSONDecodeError, ValueError:
        raise EmailCheckpointProbeError("email checkpoint output conflicts") from None
    expected = _receipt_bytes(
        activation=activation,
        checkpoint_bytes=checkpoint_bytes,
        credential_binding=credential_binding,
        observed_at=_receipt_timestamp(
            value.get("observed_at") if isinstance(value, dict) else None
        ),
        source_commit=source_commit,
    )
    if not isinstance(value, dict) or not hmac.compare_digest(payload, expected):
        raise EmailCheckpointProbeError("email checkpoint output conflicts")
    observed_at = _receipt_timestamp(value["observed_at"])
    if observed_at < activation:
        raise EmailCheckpointProbeError("email checkpoint output conflicts")


def _publish_new_private_bytes(path: Path, payload: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    published = False
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        published = True
        temporary.unlink()
        _fsync_directory(path.parent)
    except OSError:
        if published:
            _remove_publication(path)
        raise EmailCheckpointProbeError("email checkpoint output failed closed") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_publication(path: Path) -> None:
    with suppress(OSError):
        path.unlink()
        _fsync_directory(path.parent)


def _is_ok(value: object) -> bool:
    if isinstance(value, bytes):
        return value.upper() == b"OK"
    return isinstance(value, str) and value.upper() == "OK"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credential-file", required=True, type=Path)
    parser.add_argument("--binding-key-file", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--activation-time", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parents[2]
    try:
        probe_email_checkpoint(
            args.credential_file,
            args.output_dir,
            binding_key_file=args.binding_key_file,
            repo_root=repo_root,
            activation_time=args.activation_time,
        )
    except EmailCheckpointProbeError:
        print("EMAIL CHECKPOINT PROBE FAILED", file=sys.stderr)
        return 78
    print("Email checkpoint probe completed with a read-only UID metadata receipt.")
    return 0


__all__ = [
    "EmailCheckpoint",
    "EmailCheckpointProbeError",
    "ImapClientFactory",
    "main",
    "probe_email_checkpoint",
]


if __name__ == "__main__":
    raise SystemExit(main())
