#!/usr/bin/env python3
# ruff: noqa: E402, I001
"""Create a machine-derived local canary chain attestation."""

from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.local_pilot_runtime.canary_chain_verifier import cli_main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(cli_main(repo_root=REPO_ROOT))
