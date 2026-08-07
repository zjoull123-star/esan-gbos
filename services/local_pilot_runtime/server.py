"""Fail-closed Uvicorn binding for local pilot API processes."""

from __future__ import annotations

import ipaddress
import os
import socket
import stat
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import FastAPI

_SOCKET_DIRECTORY = Path("/run/gbos/sockets")
_SOCKET_BACKLOG = 2048


class ServerBindingError(RuntimeError):
    """The requested API bind is outside the local runtime boundary."""


def validate_unix_socket_path(
    value: str | os.PathLike[str],
    *,
    lstat: Callable[[os.PathLike[str]], os.stat_result] = os.lstat,
) -> Path:
    """Validate a not-yet-created socket directly below the private runtime directory."""

    raw = os.fspath(value)
    path = PurePosixPath(raw)
    if (
        not raw
        or not path.is_absolute()
        or ".." in path.parts
        or path.parent != PurePosixPath(_SOCKET_DIRECTORY)
        or path.suffix != ".sock"
        or path.name in {".sock", "..sock"}
        or str(path) != raw
    ):
        raise ServerBindingError("Unix socket path is outside the private runtime directory")
    target = Path(raw)
    try:
        lstat(target)
    except FileNotFoundError:
        return target
    except OSError as exc:
        raise ServerBindingError("Unix socket target cannot be inspected safely") from exc
    raise ServerBindingError("Unix socket target already exists")


def run_server(
    application: FastAPI,
    *,
    host: str,
    port: int,
    unix_socket: str | os.PathLike[str] | None = None,
    network_mode: str = "loopback",
    runner: Callable[..., None] | None = None,
    chmod: Callable[[os.PathLike[str], int], None] = os.chmod,
    prepare_directory: Callable[[Path], None] | None = None,
    socket_factory: Callable[[], Any] | None = None,
    unlink: Callable[[Path], None] | None = None,
) -> None:
    """Run Uvicorn without proxy trust, using either governed TCP or a pre-bound UDS."""

    active_runner = runner or _uvicorn_run
    socket_path = validate_server_binding(
        host=host,
        port=port,
        unix_socket=unix_socket,
        network_mode=network_mode,
    )
    if socket_path is None:
        try:
            active_runner(
                application,
                host=host,
                port=port,
                access_log=False,
                proxy_headers=False,
            )
        except OSError as exc:
            raise ServerBindingError("TCP binding failed") from exc
        return

    active_prepare = prepare_directory or _prepare_socket_directory
    active_prepare(socket_path.parent)
    try:
        chmod(socket_path.parent, 0o700)
    except OSError as exc:
        raise ServerBindingError("Unix socket directory permissions cannot be secured") from exc

    active_socket_factory = socket_factory or _unix_socket
    listener = active_socket_factory()
    bound = False
    active_unlink = unlink or _unlink_socket
    try:
        listener.bind(str(socket_path))
        bound = True
        chmod(socket_path, 0o600)
        listener.listen(_SOCKET_BACKLOG)
        active_runner(
            application,
            fd=listener.fileno(),
            access_log=False,
            proxy_headers=False,
        )
    except OSError as exc:
        raise ServerBindingError("Unix socket binding failed") from exc
    finally:
        listener.close()
        if bound:
            active_unlink(socket_path)


def validate_server_binding(
    *,
    host: str,
    port: int,
    unix_socket: str | os.PathLike[str] | None = None,
    network_mode: str = "loopback",
) -> Path | None:
    """Validate bind policy without opening a listener or mutating the filesystem."""

    if unix_socket is not None:
        return validate_unix_socket_path(unix_socket)
    _validate_tcp_binding(host=host, port=port, network_mode=network_mode)
    return None


def _validate_tcp_binding(*, host: str, port: int, network_mode: str) -> None:
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65_535:
        raise ServerBindingError("TCP port is invalid")
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ServerBindingError("TCP host must be a literal governed address") from exc
    if network_mode == "loopback" and address.is_loopback:
        return
    if network_mode == "internal_network" and host == "0.0.0.0":
        return
    raise ServerBindingError("TCP binding is not loopback or an explicit internal network")


def _prepare_socket_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        details = path.lstat()
    except OSError as exc:
        raise ServerBindingError("Unix socket directory cannot be prepared safely") from exc
    if not stat.S_ISDIR(details.st_mode) or path.is_symlink():
        raise ServerBindingError("Unix socket directory must be a real directory")


def _unix_socket() -> socket.socket:
    return socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)


def _unlink_socket(path: Path) -> None:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISSOCK(details.st_mode):
        path.unlink()


def _uvicorn_run(application: FastAPI, **kwargs: Any) -> None:
    import uvicorn

    uvicorn.run(application, **kwargs)


__all__ = [
    "ServerBindingError",
    "run_server",
    "validate_server_binding",
    "validate_unix_socket_path",
]
