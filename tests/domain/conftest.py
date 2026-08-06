from __future__ import annotations

import sys
from pathlib import Path

APP_ROOT = Path(__file__).parents[2] / "apps" / "esan_gbos"
sys.path.insert(0, str(APP_ROOT))
