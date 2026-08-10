from __future__ import annotations

import json
import os
import ssl
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from services.local_pilot_runtime.email_checkpoint_probe import (
    EmailCheckpointProbeError,
    probe_email_checkpoint,
)

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "local-pilot" / "probe-email-checkpoint"
HOST = "imap.host-SENTINEL.invalid"
USERNAME = "username-SENTINEL@example.invalid"
PASSWORD = "password-SENTINEL"
MAILBOX = "mailbox-SENTINEL"


def _credential(path: Path, **changes: object) -> Path:
    value: dict[str, object] = {
        "instance_id": "email-primary",
        "team_ref": "team:sales",
        "agent_task_type": "sales",
        "account_user_ref": "owner@example.invalid",
        "host": HOST,
        "port": 993,
        "mailbox": MAILBOX,
        "folder": "INBOX",
        "username": USERNAME,
        "password": PASSWORD,
        "poll_limit": 25,
        "max_message_bytes": 1_000_000,
        "max_attachment_bytes": 100_000,
        "max_attachments": 5,
        "rescan_max_window_seconds": 86_400,
        "rescan_max_uids": 100,
        "initial_checkpoint": None,
    }
    value.update(changes)
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


class FakeImap:
    def __init__(
        self,
        *,
        status_data: list[bytes] | None = None,
        status_code: str = "OK",
        failure: str | None = None,
    ) -> None:
        self.status_data = status_data or [b'"INBOX" (MESSAGES 8 UIDNEXT 43 UIDVALIDITY 7)']
        self.status_code = status_code
        self.failure = failure
        self.calls: list[tuple[object, ...]] = []

    def login(self, username: str, password: str) -> tuple[str, list[bytes]]:
        self.calls.append(("login", username, password))
        if self.failure == "login":
            raise RuntimeError(f"provider rejected {HOST} {USERNAME} {PASSWORD} {MAILBOX}")
        return "OK", [b"authenticated"]

    def status(self, folder: str, names: str) -> tuple[str, list[bytes]]:
        self.calls.append(("status", folder, names))
        if self.failure == "status":
            raise RuntimeError(f"provider rejected {HOST} {USERNAME} {PASSWORD} {MAILBOX}")
        return self.status_code, self.status_data

    def logout(self) -> tuple[str, list[bytes]]:
        self.calls.append(("logout",))
        return "BYE", [b"closed"]

    def select(self, *_args: object, **_kwargs: object) -> tuple[str, list[bytes]]:
        raise AssertionError("STATUS probe must not select a mailbox")

    def fetch(self, *_args: object, **_kwargs: object) -> tuple[str, list[bytes]]:
        raise AssertionError("checkpoint probe must not fetch messages")

    def uid(self, *_args: object, **_kwargs: object) -> tuple[str, list[bytes]]:
        raise AssertionError("checkpoint probe must not issue UID commands")

    def store(self, *_args: object, **_kwargs: object) -> tuple[str, list[bytes]]:
        raise AssertionError("checkpoint probe must not mutate messages")

    def expunge(self) -> tuple[str, list[bytes]]:
        raise AssertionError("checkpoint probe must not expunge messages")


def _capturing_factory(
    client: FakeImap,
) -> tuple[Callable[..., FakeImap], dict[str, Any]]:
    captured: dict[str, Any] = {}

    def factory(**kwargs: Any) -> FakeImap:
        captured.update(kwargs)
        return client

    return factory, captured


def test_probe_uses_verified_tls_status_only_and_atomically_writes_closed_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = _credential(tmp_path / "email-credential.json")
    output = tmp_path / "private-checkpoint"
    client = FakeImap()
    factory, captured = _capturing_factory(client)
    replacements: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def record_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        replacements.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", record_replace)

    checkpoint = probe_email_checkpoint(
        credential,
        output,
        repo_root=ROOT,
        client_factory=factory,
    )

    assert captured["host"] == HOST
    assert captured["port"] == 993
    assert captured["timeout"] == 10.0
    context = captured["ssl_context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert client.calls == [
        ("login", USERNAME, PASSWORD),
        ("status", "INBOX", "(UIDVALIDITY UIDNEXT)"),
        ("logout",),
    ]
    assert checkpoint.mailbox == MAILBOX
    assert checkpoint.uid == 42
    assert checkpoint.uidvalidity == 7
    assert checkpoint.version == 1
    assert all(secret not in repr(checkpoint) for secret in (HOST, USERNAME, PASSWORD, MAILBOX))

    checkpoint_path = output / "email-checkpoint.json"
    assert json.loads(checkpoint_path.read_text(encoding="utf-8")) == {
        "mailbox": MAILBOX,
        "uid": 42,
        "uidvalidity": 7,
        "version": 1,
    }
    assert checkpoint_path.read_bytes() == (
        b'{"mailbox":"mailbox-SENTINEL","uid":42,"uidvalidity":7,"version":1}\n'
    )
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE(checkpoint_path.stat().st_mode) == 0o600
    assert replacements == [(replacements[0][0], checkpoint_path)]
    assert replacements[0][0].parent == output
    assert replacements[0][0] != checkpoint_path
    assert list(output.iterdir()) == [checkpoint_path]


@pytest.mark.parametrize(
    ("response", "expected_uid", "expected_uidvalidity"),
    [
        (b'"INBOX" (UIDVALIDITY 1 UIDNEXT 1)', 0, 1),
        (b'"INBOX" (UIDNEXT 4294967295 UIDVALIDITY 4294967295)', 4_294_967_294, 4_294_967_295),
    ],
)
def test_probe_accepts_closed_imap_boundaries(
    tmp_path: Path,
    response: bytes,
    expected_uid: int,
    expected_uidvalidity: int,
) -> None:
    factory, _ = _capturing_factory(FakeImap(status_data=[response]))

    checkpoint = probe_email_checkpoint(
        _credential(tmp_path / "email.json"),
        tmp_path / "output",
        repo_root=ROOT,
        client_factory=factory,
    )

    assert checkpoint.uid == expected_uid
    assert checkpoint.uidvalidity == expected_uidvalidity


@pytest.mark.parametrize(
    "mutation",
    [
        lambda path: path.chmod(0o640),
        lambda path: path.write_text("{}", encoding="utf-8"),
        lambda path: path.write_text('{"host":"a","host":"b"}', encoding="utf-8"),
        lambda path: path.write_bytes(b"{" + b"x" * 65_536),
    ],
)
def test_probe_rejects_unsafe_or_nonclosed_credentials_before_network(
    tmp_path: Path,
    mutation: Callable[[Path], object],
) -> None:
    credential = _credential(tmp_path / "email.json")
    mutation(credential)
    calls = 0

    def forbidden_factory(**_kwargs: Any) -> FakeImap:
        nonlocal calls
        calls += 1
        raise AssertionError("unsafe credential must fail before network")

    with pytest.raises(EmailCheckpointProbeError):
        probe_email_checkpoint(
            credential,
            tmp_path / "output",
            repo_root=ROOT,
            client_factory=forbidden_factory,
        )

    assert calls == 0


def test_probe_rejects_credential_symlink_before_network(tmp_path: Path) -> None:
    target = _credential(tmp_path / "target.json")
    credential = tmp_path / "email.json"
    credential.symlink_to(target)

    with pytest.raises(EmailCheckpointProbeError):
        probe_email_checkpoint(
            credential,
            tmp_path / "output",
            repo_root=ROOT,
            client_factory=lambda **_kwargs: pytest.fail("network must remain unused"),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"account_user_ref": "owner with whitespace"},
        {"account_user_ref": "owner\x7f"},
        {"port": True},
        {"initial_checkpoint": '{"mailbox":"wrong","uid":0,"uidvalidity":1,"version":1}'},
    ],
)
def test_probe_rejects_unsafe_closed_credential_values_before_network(
    tmp_path: Path,
    changes: dict[str, object],
) -> None:
    with pytest.raises(EmailCheckpointProbeError):
        probe_email_checkpoint(
            _credential(tmp_path / "email.json", **changes),
            tmp_path / "output",
            repo_root=ROOT,
            client_factory=lambda **_kwargs: pytest.fail("network must remain unused"),
        )


@pytest.mark.parametrize(
    "client",
    [
        FakeImap(failure="login"),
        FakeImap(failure="status"),
        FakeImap(
            status_code="NO", status_data=[f"{HOST} {USERNAME} {PASSWORD} {MAILBOX}".encode()]
        ),
        FakeImap(status_data=[b'"INBOX" (UIDVALIDITY 0 UIDNEXT 1)']),
        FakeImap(status_data=[b'"INBOX" (UIDVALIDITY 7 UIDNEXT 0)']),
        FakeImap(status_data=[b'"INBOX" (UIDVALIDITY 7)']),
    ],
)
def test_probe_errors_and_repr_never_expose_provider_or_mailbox_values(
    tmp_path: Path,
    client: FakeImap,
) -> None:
    factory, _ = _capturing_factory(client)

    with pytest.raises(EmailCheckpointProbeError) as caught:
        probe_email_checkpoint(
            _credential(tmp_path / "email.json"),
            tmp_path / "output",
            repo_root=ROOT,
            client_factory=factory,
        )

    rendered = f"{caught.value!s} {caught.value!r}"
    assert all(secret not in rendered for secret in (HOST, USERNAME, PASSWORD, MAILBOX))
    assert not (tmp_path / "output" / "email-checkpoint.json").exists()


@pytest.mark.parametrize("unsafe", ["inside_repo", "mode", "symlink"])
def test_probe_requires_real_repo_external_mode_0700_output_before_network(
    tmp_path: Path,
    unsafe: str,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    credential = _credential(tmp_path / "email.json")
    output = tmp_path / "output"
    if unsafe == "inside_repo":
        output = repo_root / "output"
    elif unsafe == "mode":
        output.mkdir(mode=0o755)
    else:
        target = tmp_path / "target"
        target.mkdir(mode=0o700)
        output.symlink_to(target, target_is_directory=True)

    with pytest.raises(EmailCheckpointProbeError):
        probe_email_checkpoint(
            credential,
            output,
            repo_root=repo_root,
            client_factory=lambda **_kwargs: pytest.fail("network must remain unused"),
        )


def test_probe_rejects_existing_unsafe_checkpoint_target_before_network(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir(mode=0o700)
    target = tmp_path / "target.json"
    target.write_text("do not replace", encoding="utf-8")
    checkpoint = output / "email-checkpoint.json"
    checkpoint.symlink_to(target)

    with pytest.raises(EmailCheckpointProbeError):
        probe_email_checkpoint(
            _credential(tmp_path / "email.json"),
            output,
            repo_root=ROOT,
            client_factory=lambda **_kwargs: pytest.fail("network must remain unused"),
        )

    assert target.read_text(encoding="utf-8") == "do not replace"


def test_probe_script_help_is_offline_and_executable() -> None:
    result = subprocess.run(
        [str(SCRIPT), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "--credential-file" in result.stdout
    assert "--output-dir" in result.stdout
    assert "--repo-root" in result.stdout
    assert os.access(SCRIPT, os.X_OK)
