from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import stat
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
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
ACTIVATION = datetime(2026, 8, 11, 1, 2, 3, tzinfo=UTC)
NOW = ACTIVATION + timedelta(minutes=5)


def _clock() -> datetime:
    return NOW


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

    def move(self, *_args: object, **_kwargs: object) -> tuple[str, list[bytes]]:
        raise AssertionError("checkpoint probe must not move messages")

    def delete(self, *_args: object, **_kwargs: object) -> tuple[str, list[bytes]]:
        raise AssertionError("checkpoint probe must not delete messages")

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


def test_probe_uses_verified_tls_status_only_and_publishes_closed_receipt_last(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = _credential(tmp_path / "email-credential.json")
    output = tmp_path / "private-checkpoint"
    client = FakeImap()
    factory, captured = _capturing_factory(client)
    publications: list[tuple[Path, Path]] = []
    real_link = os.link

    def record_link(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        publications.append((Path(source), Path(target)))
        real_link(source, target)

    monkeypatch.setattr(os, "link", record_link)

    checkpoint = probe_email_checkpoint(
        credential,
        output,
        repo_root=ROOT,
        client_factory=factory,
        activation_time=ACTIVATION,
        now=_clock,
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
    receipt_path = output / "email-checkpoint-receipt.json"
    assert json.loads(checkpoint_path.read_text(encoding="utf-8")) == {
        "mailbox": MAILBOX,
        "uid": 42,
        "uidvalidity": 7,
        "version": 1,
    }
    assert checkpoint_path.read_bytes() == (
        b'{"mailbox":"mailbox-SENTINEL","uid":42,"uidvalidity":7,"version":1}\n'
    )
    checkpoint_bytes = checkpoint_path.read_bytes()
    receipt_bytes = receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes)
    assert receipt == {
        "activation_time": "2026-08-11T01:02:03.000000Z",
        "checkpoint_sha256": hashlib.sha256(checkpoint_bytes).hexdigest(),
        "observed_at": "2026-08-11T01:07:03.000000Z",
        "operation": "STATUS_UIDVALIDITY_UIDNEXT",
        "read_only": True,
        "schema": "gbos.email_checkpoint_receipt",
        "source_commit": subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        "version": 1,
    }
    assert (
        receipt_bytes
        == (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )
    assert re.fullmatch(r"[0-9a-f]{40}", receipt["source_commit"])
    forbidden_values = (
        HOST,
        USERNAME,
        PASSWORD,
        MAILBOX,
        "owner@example.invalid",
        "team:sales",
        "email-primary",
        "INBOX",
        "sales",
    )
    assert all(value.encode() not in receipt_bytes for value in forbidden_values)
    assert all(
        hashlib.sha256(value.encode()).hexdigest().encode() not in receipt_bytes
        for value in forbidden_values
    )
    assert not {
        "host",
        "username",
        "password",
        "mailbox",
        "account",
        "user",
        "team",
        "identity",
        "raw",
    } & set(receipt)
    assert {key for key in receipt if "sha" in key or "digest" in key} == {"checkpoint_sha256"}
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE(checkpoint_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert [target for _, target in publications] == [checkpoint_path, receipt_path]
    assert all(source.parent == output and source != target for source, target in publications)
    assert sorted(path.name for path in output.iterdir()) == [
        "email-checkpoint-receipt.json",
        "email-checkpoint.json",
    ]


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
        activation_time=ACTIVATION,
        now=_clock,
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
            activation_time=ACTIVATION,
            now=_clock,
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
            activation_time=ACTIVATION,
            now=_clock,
        )


def test_probe_bounded_read_loop_accepts_short_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = _credential(tmp_path / "email.json")
    client = FakeImap()
    factory, _ = _capturing_factory(client)
    real_read = os.read
    read_sizes: list[int] = []

    def short_read(descriptor: int, maximum: int) -> bytes:
        read_sizes.append(maximum)
        return real_read(descriptor, min(maximum, 7))

    monkeypatch.setattr(
        "services.local_pilot_runtime.email_checkpoint_probe.os.read",
        short_read,
    )

    checkpoint = probe_email_checkpoint(
        credential,
        tmp_path / "output",
        repo_root=ROOT,
        client_factory=factory,
        activation_time=ACTIVATION,
        now=_clock,
    )

    assert checkpoint.uid == 42
    assert len(read_sizes) > 2
    assert max(read_sizes) <= 65_537


def test_probe_short_read_early_eof_fails_before_output_or_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = _credential(tmp_path / "email.json")
    output = tmp_path / "output"
    real_read = os.read
    read_count = 0
    network_calls = 0

    def truncated_read(descriptor: int, maximum: int) -> bytes:
        nonlocal read_count
        read_count += 1
        if read_count == 1:
            return real_read(descriptor, min(maximum, 7))
        return b""

    def forbidden_factory(**_kwargs: Any) -> FakeImap:
        nonlocal network_calls
        network_calls += 1
        raise AssertionError("truncated credential must fail before network")

    monkeypatch.setattr(
        "services.local_pilot_runtime.email_checkpoint_probe.os.read",
        truncated_read,
    )

    with pytest.raises(EmailCheckpointProbeError):
        probe_email_checkpoint(
            credential,
            output,
            repo_root=ROOT,
            client_factory=forbidden_factory,
            activation_time=ACTIVATION,
            now=_clock,
        )

    assert read_count == 2
    assert network_calls == 0
    assert not output.exists()


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
            activation_time=ACTIVATION,
            now=_clock,
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
            activation_time=ACTIVATION,
            now=_clock,
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
            activation_time=ACTIVATION,
            now=_clock,
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
            activation_time=ACTIVATION,
            now=_clock,
        )

    assert target.read_text(encoding="utf-8") == "do not replace"


@pytest.mark.parametrize(
    ("activation_time", "clock"),
    [
        (None, _clock),
        ("not-a-timestamp", _clock),
        (datetime(2026, 8, 11, 1, 2, 3), _clock),
        (NOW + timedelta(microseconds=1), _clock),
        (ACTIVATION, lambda: datetime(2026, 8, 11, 1, 7, 3)),
    ],
)
def test_probe_rejects_missing_malformed_future_activation_or_clock_before_network(
    tmp_path: Path,
    activation_time: datetime | str | None,
    clock: Callable[[], datetime],
) -> None:
    output = tmp_path / "output"
    calls = 0

    def forbidden_factory(**_kwargs: Any) -> FakeImap:
        nonlocal calls
        calls += 1
        raise AssertionError("invalid activation must fail before network")

    with pytest.raises(EmailCheckpointProbeError) as caught:
        probe_email_checkpoint(
            _credential(tmp_path / "email.json"),
            output,
            repo_root=ROOT,
            client_factory=forbidden_factory,
            activation_time=activation_time,
            now=clock,
        )

    assert calls == 0
    assert not output.exists()
    rendered = f"{caught.value!s} {caught.value!r}"
    assert all(secret not in rendered for secret in (HOST, USERNAME, PASSWORD))


def test_probe_idempotent_replay_returns_identical_files_without_network(tmp_path: Path) -> None:
    credential = _credential(tmp_path / "email.json")
    output = tmp_path / "output"
    factory, _ = _capturing_factory(FakeImap())
    first = probe_email_checkpoint(
        credential,
        output,
        repo_root=ROOT,
        client_factory=factory,
        activation_time=ACTIVATION,
        now=_clock,
    )
    before = {path.name: (path.read_bytes(), path.stat().st_ino) for path in output.iterdir()}

    second = probe_email_checkpoint(
        credential,
        output,
        repo_root=ROOT,
        client_factory=lambda **_kwargs: pytest.fail("replay must remain offline"),
        activation_time=ACTIVATION,
        now=lambda: NOW + timedelta(hours=1),
    )

    after = {path.name: (path.read_bytes(), path.stat().st_ino) for path in output.iterdir()}
    assert second == first
    assert after == before


def test_probe_conflicting_receipt_fails_closed_without_overwrite_or_network(
    tmp_path: Path,
) -> None:
    credential = _credential(tmp_path / "email.json")
    output = tmp_path / "output"
    factory, _ = _capturing_factory(FakeImap())
    probe_email_checkpoint(
        credential,
        output,
        repo_root=ROOT,
        client_factory=factory,
        activation_time=ACTIVATION,
        now=_clock,
    )
    receipt_path = output / "email-checkpoint-receipt.json"
    receipt = json.loads(receipt_path.read_bytes())
    receipt["checkpoint_sha256"] = "0" * 64
    receipt_path.write_text(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    before = {path.name: path.read_bytes() for path in output.iterdir()}

    with pytest.raises(EmailCheckpointProbeError):
        probe_email_checkpoint(
            credential,
            output,
            repo_root=ROOT,
            client_factory=lambda **_kwargs: pytest.fail("conflict must fail before network"),
            activation_time=ACTIVATION,
            now=_clock,
        )

    assert {path.name: path.read_bytes() for path in output.iterdir()} == before


def test_probe_source_commit_drift_replay_fails_closed_without_overwrite_or_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = _credential(tmp_path / "email.json")
    output = tmp_path / "output"
    factory, _ = _capturing_factory(FakeImap())
    probe_email_checkpoint(
        credential,
        output,
        repo_root=ROOT,
        client_factory=factory,
        activation_time=ACTIVATION,
        now=_clock,
    )
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    monkeypatch.setattr(
        "services.local_pilot_runtime.email_checkpoint_probe._source_commit",
        lambda _root: "f" * 40,
    )

    with pytest.raises(EmailCheckpointProbeError):
        probe_email_checkpoint(
            credential,
            output,
            repo_root=ROOT,
            client_factory=lambda **_kwargs: pytest.fail("drifted replay must remain offline"),
            activation_time=ACTIVATION,
            now=_clock,
        )

    assert {path.name: path.read_bytes() for path in output.iterdir()} == before


def test_probe_recovers_matching_checkpoint_only_crash_state_without_overwrite(
    tmp_path: Path,
) -> None:
    credential = _credential(tmp_path / "email.json")
    output = tmp_path / "output"
    factory, _ = _capturing_factory(FakeImap())
    probe_email_checkpoint(
        credential,
        output,
        repo_root=ROOT,
        client_factory=factory,
        activation_time=ACTIVATION,
        now=_clock,
    )
    checkpoint_path = output / "email-checkpoint.json"
    receipt_path = output / "email-checkpoint-receipt.json"
    receipt_path.unlink()
    checkpoint_before = (checkpoint_path.read_bytes(), checkpoint_path.stat().st_ino)
    replay_client = FakeImap()
    replay_factory, _ = _capturing_factory(replay_client)

    checkpoint = probe_email_checkpoint(
        credential,
        output,
        repo_root=ROOT,
        client_factory=replay_factory,
        activation_time=ACTIVATION,
        now=_clock,
    )

    assert checkpoint.uid == 42
    assert checkpoint_before == (checkpoint_path.read_bytes(), checkpoint_path.stat().st_ino)
    assert receipt_path.is_file()
    assert replay_client.calls == [
        ("login", USERNAME, PASSWORD),
        ("status", "INBOX", "(UIDVALIDITY UIDNEXT)"),
        ("logout",),
    ]


def test_probe_checkpoint_only_conflict_fails_without_overwrite(tmp_path: Path) -> None:
    credential = _credential(tmp_path / "email.json")
    output = tmp_path / "output"
    output.mkdir(mode=0o700)
    checkpoint_path = output / "email-checkpoint.json"
    checkpoint_path.write_bytes(
        b'{"mailbox":"mailbox-SENTINEL","uid":41,"uidvalidity":7,"version":1}\n'
    )
    checkpoint_path.chmod(0o600)
    before = checkpoint_path.read_bytes()
    factory, _ = _capturing_factory(FakeImap())

    with pytest.raises(EmailCheckpointProbeError):
        probe_email_checkpoint(
            credential,
            output,
            repo_root=ROOT,
            client_factory=factory,
            activation_time=ACTIVATION,
            now=_clock,
        )

    assert checkpoint_path.read_bytes() == before
    assert not (output / "email-checkpoint-receipt.json").exists()


def test_probe_receipt_publication_failure_removes_new_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "output"
    real_link = os.link

    def fail_receipt_link(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        if Path(target).name == "email-checkpoint-receipt.json":
            raise OSError("simulated receipt publication crash")
        real_link(source, target)

    monkeypatch.setattr(os, "link", fail_receipt_link)
    factory, _ = _capturing_factory(FakeImap())

    with pytest.raises(EmailCheckpointProbeError):
        probe_email_checkpoint(
            _credential(tmp_path / "email.json"),
            output,
            repo_root=ROOT,
            client_factory=factory,
            activation_time=ACTIVATION,
            now=_clock,
        )

    assert list(output.iterdir()) == []


def test_probe_rejects_receipt_symlink_and_receipt_only_before_network(tmp_path: Path) -> None:
    credential = _credential(tmp_path / "email.json")
    for state in ("symlink", "receipt_only"):
        output = tmp_path / state
        output.mkdir(mode=0o700)
        receipt = output / "email-checkpoint-receipt.json"
        if state == "symlink":
            target = tmp_path / "target.json"
            target.write_text("do not replace", encoding="utf-8")
            receipt.symlink_to(target)
        else:
            receipt.write_text("{}\n", encoding="utf-8")
            receipt.chmod(0o600)

        with pytest.raises(EmailCheckpointProbeError):
            probe_email_checkpoint(
                credential,
                output,
                repo_root=ROOT,
                client_factory=lambda **_kwargs: pytest.fail("unsafe state must remain offline"),
                activation_time=ACTIVATION,
                now=_clock,
            )


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
    assert "--activation-time" in result.stdout
    assert "--repo-root" not in result.stdout
    assert os.access(SCRIPT, os.X_OK)
    assert "Path(__file__).resolve().parents[2]" in SCRIPT.read_text(encoding="utf-8")


def test_probe_script_requires_explicit_activation_before_probe(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            str(SCRIPT),
            "--credential-file",
            str(tmp_path / "missing.json"),
            "--output-dir",
            str(tmp_path / "output"),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "--activation-time" in result.stderr
    assert not (tmp_path / "output").exists()


def test_probe_script_rejects_caller_controlled_repo_root(tmp_path: Path) -> None:
    fake_root = tmp_path / "fake-repo"
    fake_root.mkdir()

    result = subprocess.run(
        [
            str(SCRIPT),
            "--credential-file",
            str(tmp_path / "missing.json"),
            "--output-dir",
            str(tmp_path / "output"),
            "--activation-time",
            "2026-08-11T01:02:03Z",
            "--repo-root",
            str(fake_root),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "unrecognized arguments: --repo-root" in result.stderr
    assert not (tmp_path / "output").exists()
