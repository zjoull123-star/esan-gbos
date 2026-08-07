from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from services.local_pilot_runtime.server import (
    ServerBindingError,
    run_server,
    validate_unix_socket_path,
)


class _FakeSocket:
    def __init__(self) -> None:
        self.bound: str | None = None
        self.backlog: int | None = None
        self.closed = False

    def bind(self, path: str) -> None:
        self.bound = path

    def listen(self, backlog: int) -> None:
        self.backlog = backlog

    def fileno(self) -> int:
        return 47

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    "path",
    (
        "relative.sock",
        "/tmp/observer.sock",
        "/run/gbos/sockets/../observer.sock",
        "/run/gbos/sockets/nested/observer.sock",
        "/run/gbos/sockets/observer",
    ),
)
def test_validate_unix_socket_path_rejects_noncanonical_or_out_of_scope_paths(
    path: str,
) -> None:
    with pytest.raises(ServerBindingError):
        validate_unix_socket_path(path)


@pytest.mark.parametrize("kind", ("regular", "symlink"))
def test_validate_unix_socket_path_rejects_existing_symlink_or_regular_file(
    kind: str,
    tmp_path: Path,
) -> None:
    regular = tmp_path / "regular"
    regular.write_text("not a socket", encoding="utf-8")
    symlink = tmp_path / "symlink"
    symlink.symlink_to(regular)
    existing = regular if kind == "regular" else symlink

    def existing_lstat(_: object) -> os.stat_result:
        return existing.lstat()

    with pytest.raises(ServerBindingError):
        validate_unix_socket_path(
            "/run/gbos/sockets/observer.sock",
            lstat=existing_lstat,
        )


def test_run_server_prebinds_uds_with_private_permissions_and_no_proxy_options() -> None:
    application = FastAPI()
    fake_socket = _FakeSocket()
    calls: list[tuple[FastAPI, dict[str, Any]]] = []
    modes: list[tuple[Path, int]] = []
    prepared: list[Path] = []
    removed: list[Path] = []

    run_server(
        application,
        host="127.0.0.1",
        port=8003,
        unix_socket="/run/gbos/sockets/observer.sock",
        runner=lambda app, **kwargs: calls.append((app, kwargs)),
        chmod=lambda path, mode: modes.append((Path(path), mode)),
        prepare_directory=lambda path: prepared.append(path),
        socket_factory=lambda: fake_socket,
        unlink=lambda path: removed.append(path),
    )

    assert prepared == [Path("/run/gbos/sockets")]
    assert fake_socket.bound == "/run/gbos/sockets/observer.sock"
    assert fake_socket.backlog == 2048
    assert fake_socket.closed is True
    assert modes == [
        (Path("/run/gbos/sockets"), 0o700),
        (Path("/run/gbos/sockets/observer.sock"), 0o600),
    ]
    assert calls == [
        (
            application,
            {"fd": 47, "access_log": False, "proxy_headers": False},
        )
    ]
    assert removed == [Path("/run/gbos/sockets/observer.sock")]


@pytest.mark.parametrize(
    ("host", "network_mode"),
    (
        ("0.0.0.0", "loopback"),
        ("127.0.0.1", "internal_network"),
        ("192.0.2.10", "loopback"),
        ("192.0.2.10", "internal_network"),
    ),
)
def test_run_server_rejects_tcp_bindings_outside_explicit_policy(
    host: str,
    network_mode: str,
) -> None:
    with pytest.raises(ServerBindingError):
        run_server(
            FastAPI(),
            host=host,
            port=8003,
            network_mode=network_mode,
            runner=lambda *_args, **_kwargs: None,
        )


@pytest.mark.parametrize(
    ("host", "network_mode"),
    (("127.0.0.1", "loopback"), ("::1", "loopback"), ("0.0.0.0", "internal_network")),
)
def test_run_server_allows_only_loopback_or_explicit_internal_tcp(
    host: str,
    network_mode: str,
) -> None:
    calls: list[dict[str, Any]] = []

    run_server(
        FastAPI(),
        host=host,
        port=8003,
        network_mode=network_mode,
        runner=lambda _app, **kwargs: calls.append(kwargs),
    )

    assert calls == [
        {
            "host": host,
            "port": 8003,
            "access_log": False,
            "proxy_headers": False,
        }
    ]
