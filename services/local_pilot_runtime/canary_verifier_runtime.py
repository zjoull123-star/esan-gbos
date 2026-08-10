"""Run the canary chain verifier inside the private Compose database network."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from .canary_chain_verifier import CanaryChainVerificationError, verify_canary_chain

_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def read_only_postgres_connector(**kwargs: object) -> object:
    """Open one verifier session with database writes disabled by default."""

    import psycopg

    connection_factory: Any = psycopg.connect
    return connection_factory(
        **kwargs,
        options="-c default_transaction_read_only=on",
    )


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CanaryChainVerificationError("observation_window_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CanaryChainVerificationError("observation_window_invalid")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canary-dir", required=True, type=Path)
    parser.add_argument("--projection-config", required=True, type=Path)
    parser.add_argument("--window-start", required=True)
    parser.add_argument("--window-end", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-source-commit", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if _COMMIT.fullmatch(args.expected_source_commit) is None:
            raise CanaryChainVerificationError("source_commit_unavailable")
        verify_canary_chain(
            canary_dir=args.canary_dir,
            projection_config_path=args.projection_config,
            output_path=args.output,
            window_start=_timestamp(args.window_start),
            window_end=_timestamp(args.window_end),
            expected_source_commit=args.expected_source_commit,
            connector=read_only_postgres_connector,
            repo_root=Path(__file__).resolve().parents[2],
        )
    except CanaryChainVerificationError as exc:
        print(f"CANARY CHAIN VERIFICATION FAILED: {exc.code}", file=sys.stderr)
        return 78
    print(f"Created private canary chain attestation at {args.output}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
