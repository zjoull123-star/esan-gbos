#!/usr/bin/env python3
# ruff: noqa: UP017
"""Create and verify privacy-safe emergency containment records."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SERVICE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")


def _atomic_private_json(path: Path, payload: dict[str, Any]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _runtime_dir(value: str) -> Path:
    path = Path(value)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("runtime directory must be a real directory")
    path.chmod(0o700)
    return path


def _load_object(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 16_384:
        raise ValueError("containment record is unavailable")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("containment record is invalid")
    return value


def _activate(runtime: Path) -> int:
    latch = {
        "schema_version": "1.0",
        "latch_id": str(uuid.uuid4()),
        "activated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    _atomic_private_json(runtime / "EMERGENCY_STOP", latch)
    return 0


def _running_services(path: Path) -> list[str]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 65_536:
        raise ValueError("running-service result is unavailable")
    values = sorted({line.strip() for line in path.read_text(encoding="utf-8").splitlines()})
    if any(not _SERVICE.fullmatch(value) for value in values):
        raise ValueError("running-service result is invalid")
    return values


def _record(
    runtime: Path,
    *,
    stop_returncode: int,
    ps_returncode: int,
    running_file: Path,
) -> int:
    latch = _load_object(runtime / "EMERGENCY_STOP")
    latch_id = latch.get("latch_id")
    if not isinstance(latch_id, str) or not latch_id:
        raise ValueError("emergency latch is invalid")
    running = _running_services(running_file) if ps_returncode == 0 else []
    verified = ps_returncode == 0 and not running
    receipt = {
        "schema_version": "1.0",
        "latch_id": latch_id,
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "stop_returncode": stop_returncode,
        "inspection_returncode": ps_returncode,
        "running_services": running,
        "verified": verified,
    }
    _atomic_private_json(runtime / "containment-receipt.json", receipt)
    archive = runtime / "containment-receipts"
    archive.mkdir(mode=0o700, exist_ok=True)
    if archive.is_symlink() or not archive.is_dir():
        raise ValueError("containment receipt archive is invalid")
    archive.chmod(0o700)
    _atomic_private_json(archive / f"{latch_id}.json", receipt)
    return 0 if verified else 1


def _clear(runtime: Path) -> int:
    latch_path = runtime / "EMERGENCY_STOP"
    receipt_path = runtime / "containment-receipt.json"
    latch = _load_object(latch_path)
    receipt = _load_object(receipt_path)
    if (
        receipt.get("schema_version") != "1.0"
        or receipt.get("verified") is not True
        or receipt.get("running_services") != []
        or receipt.get("latch_id") != latch.get("latch_id")
    ):
        raise ValueError("matching verified containment receipt is required")
    latch_path.unlink()
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("activate", "clear"):
        item = subparsers.add_parser(command)
        item.add_argument("--runtime-dir", required=True)
    record = subparsers.add_parser("record")
    record.add_argument("--runtime-dir", required=True)
    record.add_argument("--stop-returncode", type=int, required=True)
    record.add_argument("--ps-returncode", type=int, required=True)
    record.add_argument("--running-file", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        runtime = _runtime_dir(args.runtime_dir)
        if args.command == "activate":
            return _activate(runtime)
        if args.command == "record":
            return _record(
                runtime,
                stop_returncode=args.stop_returncode,
                ps_returncode=args.ps_returncode,
                running_file=args.running_file,
            )
        return _clear(runtime)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"CONTAINMENT ERROR: {exc}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
